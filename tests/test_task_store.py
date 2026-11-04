"""Task store: durable plan/workspace/artifact bookkeeping."""

from __future__ import annotations

import os

import pytest

from src.core.task_store import TaskStore


@pytest.fixture()
def store(tmp_path):
    return TaskStore(
        db_path=str(tmp_path / "tasks.db"),
        workspace_root=str(tmp_path / "workspaces"),
    )


def test_task_lifecycle_and_workspace(store):
    task = store.create_task(
        user_id=1,
        session_id=2,
        kb_name="AAA",
        title="整理清单",
        definition_id="",
        query="整理一份清单",
    )
    assert task.id
    assert task.status == "queued"
    assert os.path.isdir(os.path.join(task.workspace_dir, "out"))

    store.set_current_turn(task.id, "turn-1")
    store.add_turn(task.id, "turn-1", "initial")
    store.set_plan(task.id, [{"id": "p1", "title": "自拟计划", "status": "pending"}])
    store.set_status(task.id, "running")

    fetched = store.get_task(task.id, 1)
    assert fetched is not None
    assert fetched.current_turn_id == "turn-1"
    assert fetched.plan[0]["title"] == "自拟计划"
    assert [item["turn_id"] for item in store.list_turns(task.id)] == ["turn-1"]
    assert store.get_task(task.id, 999) is None


def test_artifact_scan_is_idempotent(store):
    task = store.create_task(
        user_id=1,
        session_id=2,
        kb_name="AAA",
        title="artifact",
        definition_id="",
        query="q",
    )
    out = os.path.join(task.workspace_dir, "out")
    with open(os.path.join(out, "component_list.md"), "w", encoding="utf-8") as handle:
        handle.write("| 序号 | 器件 |\n|---|---|\n| 1 | MCU |\n")

    first = store.scan_workspace_artifacts(task.id)
    assert len(first) == 1
    assert first[0].name == "component_list.md"
    assert first[0].kind == "document"
    assert first[0].sha256

    second = store.scan_workspace_artifacts(task.id)
    assert second == []
    assert len(store.list_artifacts(task.id)) == 1


def test_artifact_version_and_path_guard(store, tmp_path):
    task = store.create_task(
        user_id=1,
        session_id=2,
        kb_name="AAA",
        title="version",
        definition_id="",
        query="q",
    )
    path = os.path.join(task.workspace_dir, "out", "report.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("v1")
    first = store.register_artifact(task.id, path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("v2 changed")
    second = store.register_artifact(task.id, path)
    assert first.version == 1
    assert second.version == 2

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError):
        store.register_artifact(task.id, str(outside))
