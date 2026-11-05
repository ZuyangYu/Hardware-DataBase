"""Conversation-scoped artifact API: no task id / disk path in the UI."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import artifacts as artifacts_route
from src.core.task_store import TaskStore


class FakeConv:
    def get_session(self, user_id: int, session_id: int):
        if session_id == 7:
            return SimpleNamespace(id=7, kb_name="AAA")
        return None


@pytest.fixture()
def client(tmp_path):
    store = TaskStore(
        db_path=str(tmp_path / "tasks.db"),
        workspace_root=str(tmp_path / "workspaces"),
    )
    task = store.create_task(
        user_id=1, session_id=7, kb_name="AAA", title="t",
        definition_id="", query="q",
    )
    out = os.path.join(task.workspace_dir, "out")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "result.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# result\n")
    store.register_artifact(task.id, path)
    app = FastAPI()
    app.include_router(artifacts_route.router, prefix="/api/v1")
    app.dependency_overrides[artifacts_route.current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[artifacts_route._conv_service] = lambda: FakeConv()
    app.dependency_overrides[artifacts_route._task_store] = lambda: store
    return TestClient(app), store, task


def test_list_conversation_artifacts(client):
    http, _store, task = client
    response = http.get("/api/v1/conversations/7/artifacts")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "result.md"
    assert items[0]["task_id"] == task.id
    assert items[0]["download_url"].startswith("/api/v1/artifacts/")


def test_download_artifact_by_id(client):
    http, _store, _task = client
    item = http.get("/api/v1/conversations/7/artifacts").json()["items"][0]
    response = http.get(item["download_url"])
    assert response.status_code == 200
    assert response.text == "# result\n"


def test_unknown_session_returns_404(client):
    http, _store, _task = client
    assert http.get("/api/v1/conversations/999/artifacts").status_code == 404
