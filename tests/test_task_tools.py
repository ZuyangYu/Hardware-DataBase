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
