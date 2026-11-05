"""Agent-maintained task plans."""

from __future__ import annotations

import json

import pytest

from src.agents.tools.task_tools import build_task_tools
from src.core.task_store import TaskStore


@pytest.fixture()
def store(tmp_path):
    return TaskStore(
        db_path=str(tmp_path / "tasks.db"),
        workspace_root=str(tmp_path / "workspaces"),
    )


def _task(store):
    return store.create_task(
        user_id=1,
        session_id=2,
        kb_name="AAA",
        title="plan",
        definition_id="",
        query="q",
    )


def test_plan_update_persists(store):
    task = _task(store)
    tool = build_task_tools(task.id, store)[0]
    result = json.loads(
        tool(
            '[{"title":"定位候选","status":"running"},'
            '{"title":"填充表格","status":"pending"}]'
        )
    )
    assert result["updated"] is True
    plan = store.get_task(task.id).plan
    assert [item["title"] for item in plan] == ["定位候选", "填充表格"]
    assert plan[0]["status"] == "running"


def test_plan_update_rejects_invalid_input(store):
    task = _task(store)
    tool = build_task_tools(task.id, store)[0]
    assert "error" in json.loads(tool("not json"))
    assert "error" in json.loads(tool("[]"))
    assert store.get_task(task.id).plan == []


def test_publish_artifact_writes_and_registers(store, tmp_path):
    task = _task(store)
    tools = {
        item.__name__: item
        for item in build_task_tools(
            task.id, store, workspace_dir=task.workspace_dir, turn_id="turn-1"
        )
    }
    result = json.loads(
        tools["task_publish_artifact"]("report.md", "# report\n", "document")
    )
    assert result["name"] == "report.md"
    assert result["download_url"].endswith("/download")
    artifacts = store.list_artifacts(task.id)
    assert len(artifacts) == 1
    assert artifacts[0].name == "report.md"
    assert artifacts[0].turn_id == "turn-1"
    # Finalize scan must not create a duplicate version of the same content.
    assert store.scan_workspace_artifacts(task.id) == []
