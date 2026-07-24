from __future__ import annotations

import json
import sqlite3

import pytest

from src.document_authoring.models import DocumentWorkOrder, KnowledgeBaseSourceSnapshot
from src.document_authoring.work_order_store import DocumentAuthoringStore


def make_work_order(**updates) -> DocumentWorkOrder:
    values = {
        "work_order_id": "work-order-a",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "baseline_id": "baseline-a",
        "baseline_content_hash": "baseline-hash",
        "source_set_snapshot_id": "snapshot-a",
        "template_version_id": "template-a",
        "document_schema_id": "schema-a",
        "document_schema_version": "1",
        "template_schema_id": "template-schema-a",
        "template_schema_version": "1",
        "retrieval_policy_version": "1",
        "renderer_policy_version": "1",
        "target_format": "xlsx",
        "execution_mode": "deterministic_only",
        "created_by": "alice",
    }
    values.update(updates)
    return DocumentWorkOrder(**values)


def test_store_round_trips_knowledge_base_work_order(tmp_path):
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "authoring-files"),
    )
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="tenant-a",
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        created_by="alice",
    )
    store.create_knowledge_base_source_snapshot(snapshot)
    assert store.get_knowledge_base_source_snapshot(snapshot.source_set_snapshot_id) == snapshot
    order = make_work_order(
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id=snapshot.source_set_snapshot_id,
    )

    store.create_work_order(order)

    assert store.get_work_order(order.work_order_id).knowledge_base_name == "hardware"
    assert store.list_work_orders_for_knowledge_base("tenant-a", "hardware") == [order]


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"project_id": None}, "project-scoped work orders require project_id and baseline_id"),
        (
            {"knowledge_base_name": "hardware"},
            "project-scoped work orders cannot name a knowledge base",
        ),
        (
            {
                "scope_type": "knowledge_base",
                "knowledge_base_name": None,
                "project_id": None,
                "baseline_id": None,
                "baseline_content_hash": "",
            },
            "knowledge-base work orders require knowledge_base_name",
        ),
        (
            {
                "scope_type": "knowledge_base",
                "knowledge_base_name": "hardware",
            },
            "knowledge-base work orders cannot name a project or baseline",
        ),
    ],
)
def test_work_order_scope_fields_are_mutually_exclusive(updates, message):
    with pytest.raises(ValueError, match=message):
        make_work_order(**updates)


def test_store_migrates_legacy_project_work_orders_without_losing_scope_or_children(tmp_path):
    db_path = tmp_path / "authoring.db"
    legacy_order = make_work_order(idempotency_key="request-a")
    legacy_payload = legacy_order.model_dump(mode="json", exclude={"scope_type", "knowledge_base_name"})
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE document_work_orders (
                work_order_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL, status TEXT NOT NULL,
                idempotency_key TEXT, payload_json TEXT NOT NULL,
                UNIQUE(tenant_id, project_id, idempotency_key)
            );
            CREATE TABLE authoring_run_manifests (
                run_manifest_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id)
            );
            """
        )
        conn.execute(
            """INSERT INTO document_work_orders
               (work_order_id, tenant_id, project_id, status, idempotency_key, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                legacy_order.work_order_id,
                legacy_order.tenant_id,
                legacy_order.project_id,
                legacy_order.status,
                legacy_order.idempotency_key,
                json.dumps(legacy_payload),
            ),
        )
        conn.execute(
            """INSERT INTO authoring_run_manifests
               (run_manifest_id, work_order_id, payload_json) VALUES (?, ?, ?)""",
            ("manifest-a", legacy_order.work_order_id, "{}"),
        )

    store = DocumentAuthoringStore(str(db_path), str(tmp_path / "authoring-files"))

    assert store.get_work_order(legacy_order.work_order_id) == legacy_order
    assert store.find_work_order_by_idempotency("tenant-a", "project-a", "request-a") == legacy_order
    assert store.list_work_orders("tenant-a", "project-a") == [legacy_order]
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(document_work_orders)")}
        migrated_scope = conn.execute(
            "SELECT scope_type, scope_key FROM document_work_orders WHERE work_order_id = ?",
            (legacy_order.work_order_id,),
        ).fetchone()
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert {"scope_type", "scope_key", "knowledge_base_name"} <= columns
    assert migrated_scope == ("project", "project:project-a")
    assert foreign_key_errors == []

    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="tenant-a",
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        created_by="alice",
    )
    store.create_knowledge_base_source_snapshot(snapshot)
    knowledge_base_order = make_work_order(
        work_order_id="work-order-kb",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id=snapshot.source_set_snapshot_id,
        idempotency_key="request-a",
    )
    store.create_work_order(knowledge_base_order)
    assert store.list_work_orders_for_knowledge_base("tenant-a", "hardware") == [
        knowledge_base_order
    ]
    with pytest.raises(sqlite3.IntegrityError):
        store.create_work_order(
            knowledge_base_order.model_copy(update={"work_order_id": "work-order-kb-duplicate"})
        )
