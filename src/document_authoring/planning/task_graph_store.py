"""Immutable SQLite persistence for compiled document task graphs."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from src.document_authoring.harness.idempotency import canonical_json

from .task_graph import CompiledTaskGraph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskGraphStore:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS compiled_task_graphs (
                    graph_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    task_id TEXT,
                    plan_id TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    source_snapshot_id TEXT NOT NULL,
                    source_snapshot_hash TEXT NOT NULL,
                    graph_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(graph_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_compiled_task_graphs_owner
                    ON compiled_task_graphs(tenant_id, user_id, task_id, version DESC);
                """
            )

    @staticmethod
    def _owner(tenant_id: str, user_id: str) -> tuple[str, str]:
        tenant = str(tenant_id or "").strip()
        user = str(user_id or "").strip()
        if not tenant or not user:
            raise ValueError("tenant_id and user_id are required")
        return tenant, user

    def put(
        self,
        graph: CompiledTaskGraph,
        *,
        tenant_id: str = "default",
        user_id: str = "system",
        task_id: str | None = None,
    ) -> CompiledTaskGraph:
        tenant, user = self._owner(tenant_id, user_id)
        if not isinstance(graph, CompiledTaskGraph):
            graph = CompiledTaskGraph.model_validate(graph)
        payload_json = canonical_json(graph.model_dump(mode="json"))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM compiled_task_graphs WHERE graph_id = ? AND version = ?",
                    (graph.graph_id, graph.version),
                ).fetchone()
                if existing is not None:
                    if existing["tenant_id"] != tenant or existing["user_id"] != user:
                        raise PermissionError("compiled graph belongs to a different owner")
                    persisted = self._validate_row(existing)
                    if existing["payload_json"] != payload_json or persisted != graph:
                        raise ValueError("immutable compiled graph already contains different content")
                    connection.execute("COMMIT")
                    return persisted
                connection.execute(
                    """INSERT INTO compiled_task_graphs
                       (graph_id, version, tenant_id, user_id, task_id, plan_id,
                        plan_hash, source_snapshot_id, source_snapshot_hash,
                        graph_hash, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        graph.graph_id,
                        graph.version,
                        tenant,
                        user,
                        str(task_id).strip() if task_id is not None else None,
                        graph.plan_id,
                        graph.plan_hash,
                        graph.source_snapshot_id,
                        graph.source_snapshot_hash,
                        graph.graph_hash,
                        _now(),
                        payload_json,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return graph

    def get(
        self,
        graph_id: str,
        version: int,
        graph_hash: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        task_id: str | None = None,
    ) -> CompiledTaskGraph | None:
        sql = "SELECT * FROM compiled_task_graphs WHERE graph_id = ? AND version = ?"
        params: list[Any] = [graph_id, version]
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        with closing(self._connect()) as connection:
            row = connection.execute(sql, params).fetchone()
        if row is None:
            return None
        graph = self._validate_row(row)
        if graph.graph_hash != graph_hash:
            raise ValueError("compiled graph hash does not match requested hash")
        return graph

    @staticmethod
    def _validate_row(row: sqlite3.Row) -> CompiledTaskGraph:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("compiled graph payload is not valid JSON") from exc
        graph = CompiledTaskGraph.model_validate(payload)
        indexed = {
            "graph_id": row["graph_id"],
            "version": int(row["version"]),
            "plan_id": row["plan_id"],
            "plan_hash": row["plan_hash"],
            "source_snapshot_id": row["source_snapshot_id"],
            "source_snapshot_hash": row["source_snapshot_hash"],
            "graph_hash": row["graph_hash"],
        }
        for field, value in indexed.items():
            if getattr(graph, field) != value:
                raise ValueError(f"compiled graph indexed {field} does not match payload")
        return graph
