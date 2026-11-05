"""Task API: create, continue, artifacts, stop (functional)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import tasks as tasks_route
from src.core.task_store import TaskStore


class FakeConv:
    def __init__(self) -> None:
        self.sessions = {1: SimpleNamespace(id=1, kb_name="AAA", user_id=1)}
        self.turns: dict[str, object] = {}
        self.created: list[tuple[str, dict]] = []
        self.cancelled: list[str] = []

    def create_session(self, user_id: int, kb_name: str, title: str = "", **kwargs):
        session = SimpleNamespace(id=max(self.sessions) + 1, kb_name=kb_name, user_id=user_id)
        self.sessions[session.id] = session
        return session

    def get_session(self, user_id: int, session_id: int):
        return self.sessions.get(session_id)

    def create_turn(self, user_id: int, session_id: int, query: str, **kwargs):
        turn_id = f"turn-{len(self.turns) + 1}"
        turn = SimpleNamespace(
            id=turn_id,
            status="pending",
            query=query,
            answer="",
            summary={},
            error_message="",
            created_at="2026-01-01T00:00:00+00:00",
            finished_at=None,
        )
        self.turns[turn_id] = turn
        self.created.append((turn_id, dict(kwargs)))
        return turn

    def get_turn(self, user_id: int, turn_id: str):
        return self.turns.get(turn_id)

    def request_turn_cancel(self, user_id: int, turn_id: str):
        self.cancelled.append(turn_id)
        return self.turns.get(turn_id)

    def list_turn_events(self, session_id: int, turn_id: str):
        return [
            SimpleNamespace(seq=1, event_type="turn_start", payload={}),
            SimpleNamespace(
                seq=2,
                event_type="tool_started",
                payload={"tool_name": "workspace_write"},
            ),
        ]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    conv = FakeConv()
    store = TaskStore(
        db_path=str(tmp_path / "tasks.db"),
        workspace_root=str(tmp_path / "workspaces"),
    )
    app = FastAPI()
    app.include_router(tasks_route.router, prefix="/api/v1")
    app.dependency_overrides[tasks_route.current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[tasks_route.get_auth_service] = lambda: SimpleNamespace()
    app.dependency_overrides[tasks_route._conv_service] = lambda: conv
    app.dependency_overrides[tasks_route._task_store] = lambda: store
    monkeypatch.setattr(
        tasks_route,
        "build_context_for_user",
        lambda user, kb_name, auth: SimpleNamespace(
            has_kb_permission=lambda kb, permission: True
        ),
    )
    monkeypatch.setattr(
        tasks_route, "reject_system_admin_kb_access", lambda ctx: None
    )
    return TestClient(app), conv, store


def test_create_continue_and_get_task(client):
    http, conv, store = client
    response = http.post(
        "/api/v1/tasks", json={"query": "帮我整理一份清单", "kb_name": "AAA"}
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    task = payload["task"]
    assert task["status"] == "queued"
    assert task["definition_id"] == ""
    assert payload["turn"]["id"] == "turn-1"
    # The turn carries the task id so the worker can rebuild task context.
    assert conv.created[0][1]["trace_context"] == {"task_id": task["id"]}
    assert conv.created[0][1]["query_mode"] == "deep"

    fetched = http.get(f"/api/v1/tasks/{task['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["current_turn"]["id"] == "turn-1"

    continued = http.post(
        f"/api/v1/tasks/{task['id']}/messages",
        json={"query": "继续补充单机用量"},
    )
    assert continued.status_code == 201, continued.text
    assert continued.json()["turn"]["id"] == "turn-2"
    assert conv.created[1][1]["trace_context"] == {"task_id": task["id"]}
    assert len(store.list_turns(task["id"])) == 2


def test_artifact_download_and_stop(client):
    http, conv, store = client
    created = http.post(
        "/api/v1/tasks", json={"query": "输出一份表格", "kb_name": "AAA"}
    ).json()
    task_id = created["task"]["id"]
    task = store.get_task(task_id)
    out = os.path.join(task.workspace_dir, "out")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.md"), "w", encoding="utf-8") as handle:
        handle.write("# result\n")
    store.scan_workspace_artifacts(task_id)

    listed = http.get(f"/api/v1/tasks/{task_id}/artifacts")
    assert listed.status_code == 200
    artifacts = listed.json()["items"]
    assert len(artifacts) == 1
    assert artifacts[0]["name"] == "result.md"

    download = http.get(artifacts[0]["download_url"])
    assert download.status_code == 200
    assert download.text == "# result\n"

    stopped = http.post(f"/api/v1/tasks/{task_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["task"]["status"] == "cancelled"
    assert conv.cancelled == ["turn-1"]


def test_unknown_definition_is_rejected(client):
    http, _conv, _store = client
    response = http.post(
        "/api/v1/tasks",
        json={"query": "随便做点什么", "definition_id": "does-not-exist"},
    )
    assert response.status_code == 400
    assert "unknown task definition" in response.text


def test_task_status_follows_current_turn(client):
    http, conv, _store = client
    created = http.post(
        "/api/v1/tasks", json={"query": "做点什么", "kb_name": "AAA"}
    ).json()
    task_id = created["task"]["id"]
    conv.turns["turn-1"].status = "failed"
    conv.turns["turn-1"].error_message = "boom"
    fetched = http.get(f"/api/v1/tasks/{task_id}").json()
    assert fetched["status"] == "failed"


def test_external_task_definitions_endpoint(client, monkeypatch, tmp_path):
    from src import settings

    root = tmp_path / "defs"
    root.mkdir()
    (root / "review.yaml").write_text(
        "id: review\ntitle: 审阅\ngoal: 审阅并给出结论\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "TASK_DEFINITION_DIR", str(root))
    http, _conv, _store = client
    response = http.get("/api/v1/task-definitions")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items[0]["id"] == "review"
    assert items[0]["title"] == "审阅"


def test_task_projection_includes_verification(client):
    http, _conv, store = client
    created = http.post(
        "/api/v1/tasks", json={"query": "做点什么", "kb_name": "AAA"}
    ).json()
    task_id = created["task"]["id"]
    store.set_verification(task_id, {"status": "pass", "checks": []})
    fetched = http.get(f"/api/v1/tasks/{task_id}").json()
    assert fetched["verification"]["status"] == "pass"


def test_task_events_endpoint(client):
    http, _conv, _store = client
    created = http.post(
        "/api/v1/tasks", json={"query": "做点什么", "kb_name": "AAA"}
    ).json()
    task_id = created["task"]["id"]
    response = http.get(f"/api/v1/tasks/{task_id}/events")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["type"] for item in items] == ["turn_start", "tool_started"]
    assert items[1]["payload"]["tool_name"] == "workspace_write"


def test_stop_completed_task_is_noop(client):
    http, conv, _store = client
    created = http.post(
        "/api/v1/tasks", json={"query": "做点什么", "kb_name": "AAA"}
    ).json()
    task_id = created["task"]["id"]
    conv.turns["turn-1"].status = "completed"
    stopped = http.post(f"/api/v1/tasks/{task_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["task"]["status"] == "completed"


def test_stop_cancelled_task_still_cancels_streaming_turn(client):
    http, conv, store = client
    created = http.post(
        "/api/v1/tasks", json={"query": "长任务", "kb_name": "AAA"}
    ).json()
    task_id = created["task"]["id"]
    store.set_status(task_id, "cancelled", finished=True)
    conv.turns["turn-1"].status = "streaming"
    stopped = http.post(f"/api/v1/tasks/{task_id}/stop")
    assert stopped.status_code == 200
    assert conv.cancelled == ["turn-1"]
