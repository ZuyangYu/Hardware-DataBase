from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.document_authoring.planning.models import DocumentPlan, OutputSpec
from src.document_authoring.planning.store import DocumentPlanningStore
from src.document_authoring.work_order_store import DocumentAuthoringStore

from tests.test_document_planning_contracts import _output_payload, _plan_payload


def _spec() -> OutputSpec:
    return OutputSpec(**_output_payload())


def _plan() -> DocumentPlan:
    return DocumentPlan(**_plan_payload())


def test_output_spec_versions_are_immutable_and_owner_scoped(tmp_path: Path):
    store = DocumentPlanningStore(str(tmp_path / "planning.db"))
    spec = _spec()

    persisted = store.create_output_spec(
        spec,
        tenant_id="tenant-a",
        user_id="user-a",
        task_id="task-a",
    )
    replay = store.create_output_spec(
        spec,
        tenant_id="tenant-a",
        user_id="user-a",
        task_id="task-a",
    )

    assert persisted == spec == replay
    assert store.get_output_spec("spec-001", 1, tenant_id="tenant-a", user_id="user-a") == spec
    assert store.get_output_spec("spec-001", 1, tenant_id="tenant-b", user_id="user-a") is None
    assert store.get_latest_output_spec("task-a", tenant_id="tenant-a", user_id="user-a") == spec

    changed = OutputSpec(**{**_output_payload(), "purpose": "不同的接口评审"})
    with pytest.raises(ValueError, match="immutable|different|existing"):
        store.create_output_spec(changed, tenant_id="tenant-a", user_id="user-a", task_id="task-a")


def test_plan_versions_validate_indexed_hashes_and_cas_stale_transition(tmp_path: Path):
    store = DocumentPlanningStore(str(tmp_path / "planning.db"))
    plan = _plan()
    store.create_plan(
        plan,
        tenant_id="tenant-a",
        user_id="user-a",
        task_id="task-a",
    )

    stale = store.mark_plan_stale(
        "plan-001",
        1,
        expected_plan_hash=plan.plan_hash,
        reason_code="source_changed",
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert stale.status == "stale"
    assert store.get_plan("plan-001", 1, tenant_id="tenant-a", user_id="user-a").status == "stale"
    assert store.mark_plan_stale(
        "plan-001",
        1,
        expected_plan_hash=plan.plan_hash,
        reason_code="source_changed",
        tenant_id="tenant-a",
        user_id="user-a",
    ).status == "stale"

    with pytest.raises(ValueError, match="hash|stale"):
        store.mark_plan_stale(
            "plan-001",
            1,
            expected_plan_hash="sha256:wrong",
            reason_code="source_changed",
            tenant_id="tenant-a",
            user_id="user-a",
        )

    with store._connect() as connection:
        connection.execute(
            "UPDATE document_plans SET plan_hash = ? WHERE document_plan_id = ? AND version = ?",
            ("sha256:corrupted", "plan-001", 1),
        )
    with pytest.raises(ValueError, match="hash"):
        store.get_plan("plan-001", 1, tenant_id="tenant-a", user_id="user-a")


def test_planning_events_are_append_only_and_idempotent(tmp_path: Path):
    store = DocumentPlanningStore(str(tmp_path / "planning.db"))
    first = store.append_event(
        task_id="task-a",
        event_type="output_spec_created",
        idempotency_key="event-key-1",
        payload={"output_spec_id": "spec-001", "version": 1},
    )
    replay = store.append_event(
        task_id="task-a",
        event_type="output_spec_created",
        idempotency_key="event-key-1",
        payload={"output_spec_id": "spec-001", "version": 1},
    )
    assert replay["event_id"] == first["event_id"]
    assert store.list_events("task-a")[0]["event_type"] == "output_spec_created"

    with pytest.raises(ValueError, match="idempotency|payload"):
        store.append_event(
            task_id="task-a",
            event_type="output_spec_changed",
            idempotency_key="event-key-1",
            payload={"output_spec_id": "spec-001", "version": 2},
        )


def test_read_rejects_indexed_identity_or_payload_disagreement(tmp_path: Path):
    store = DocumentPlanningStore(str(tmp_path / "planning.db"))
    spec = _spec()
    store.create_output_spec(spec, tenant_id="tenant-a", user_id="user-a", task_id="task-a")
    with store._connect() as connection:
        connection.execute(
            "UPDATE document_output_specs SET content_hash = ? WHERE output_spec_id = ? AND version = ?",
            ("sha256:corrupted", "spec-001", 1),
        )
    with pytest.raises(ValueError, match="hash"):
        store.get_output_spec("spec-001", 1, tenant_id="tenant-a", user_id="user-a")


def test_document_authoring_store_composes_planning_store_on_same_database(tmp_path: Path):
    database = tmp_path / "document_authoring.db"
    authoring = DocumentAuthoringStore(str(database), artifact_root=str(tmp_path / "artifacts"))
    assert authoring.planning.db_path == str(database)
    with sqlite3.connect(database) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "document_output_specs",
        "document_plans",
        "document_planning_events",
    } <= table_names


def test_planning_tables_are_added_to_a_pre_planning_database_without_touching_legacy_payload(tmp_path: Path):
    database = tmp_path / "legacy.db"
    legacy_payload = json.dumps({"work_order_id": "legacy-1", "status": "completed"})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE document_work_orders (work_order_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO document_work_orders VALUES (?, ?)", ("legacy-1", legacy_payload)
        )
        connection.commit()

    DocumentPlanningStore(str(database))

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT payload_json FROM document_work_orders WHERE work_order_id = ?", ("legacy-1",)
        ).fetchone()[0] == legacy_payload
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'document_plans'"
        ).fetchone() is not None
