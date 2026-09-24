from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.document_authoring.planning.task_graph import TaskGraphCompiler
from src.document_authoring.planning.task_graph_store import TaskGraphStore

from tests.test_document_planning_contracts import _plan_payload
from src.document_authoring.planning.models import DocumentPlan


def _graph():
    return TaskGraphCompiler().compile(DocumentPlan.model_validate(_plan_payload()))


def test_graph_store_is_immutable_owner_scoped_and_idempotent(tmp_path: Path) -> None:
    store = TaskGraphStore(str(tmp_path / "authoring.db"))
    graph = _graph()

    assert store.put(graph, tenant_id="tenant-a", user_id="user-a", task_id="task-a") == graph
    assert store.put(graph, tenant_id="tenant-a", user_id="user-a", task_id="task-a") == graph
    assert store.get(
        graph.graph_id,
        graph.version,
        graph.graph_hash,
        tenant_id="tenant-a",
        user_id="user-a",
    ) == graph
    assert store.get(
        graph.graph_id,
        graph.version,
        graph.graph_hash,
        tenant_id="tenant-b",
        user_id="user-a",
    ) is None

    with pytest.raises(PermissionError):
        store.put(graph, tenant_id="tenant-b", user_id="user-a", task_id="task-a")

    changed_payload = graph.model_dump(mode="json")
    changed_payload["source_snapshot_hash"] = "sha256:changed-source"
    with pytest.raises(ValueError, match="graph_hash|immutable|different"):
        store.put(type(graph).model_validate(changed_payload), tenant_id="tenant-a", user_id="user-a")


def test_graph_store_validates_persisted_identity_and_creates_schema(tmp_path: Path) -> None:
    database = tmp_path / "authoring.db"
    store = TaskGraphStore(str(database))
    graph = _graph()
    store.put(graph, tenant_id="tenant-a", user_id="user-a")

    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'compiled_task_graphs'"
        ).fetchone()
        assert table is not None
        connection.execute(
            "UPDATE compiled_task_graphs SET graph_hash = ? WHERE graph_id = ? AND version = ?",
            ("sha256:corrupted", graph.graph_id, graph.version),
        )

    with pytest.raises(ValueError, match="hash"):
        store.get(graph.graph_id, graph.version, graph.graph_hash, tenant_id="tenant-a", user_id="user-a")
