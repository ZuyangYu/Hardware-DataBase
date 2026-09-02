"""Phase 0 store contract tests: AuthoringExecutionEvent append semantics,
EvidenceRegistry store API and NodeExecutionReceipt action_key/attempt."""

from __future__ import annotations

import uuid

import pytest

from src.document_authoring.models import (
    AuthoringExecutionEvent,
    EvidenceRegistryEntry,
    HarnessRun,
    NodeExecutionReceipt,
)
from src.document_authoring.work_order_store import DocumentAuthoringStore


def _store(tmp_path) -> DocumentAuthoringStore:
    return DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "files"))


def _seed_work_order_and_run(store: DocumentAuthoringStore, work_order_id: str = "wo-1",
                             harness_run_id: str = "run-1", tenant_id: str = "t1") -> None:
    import json
    with store._connect() as conn:
        conn.execute(
            """INSERT INTO document_work_orders
               (work_order_id, tenant_id, scope_type, scope_key, project_id, knowledge_base_name,
                status, idempotency_key, payload_json)
               VALUES (?, ?, 'project', ?, 'p-1', NULL, 'planned', NULL, ?)""",
            (work_order_id, tenant_id, "project:p-1", json.dumps({"work_order_id": work_order_id})),
        )
    store.create_harness_run(HarnessRun(
        harness_run_id=harness_run_id, work_order_id=work_order_id,
        run_manifest_id="rm-1", tenant_id=tenant_id,
    ))


def _event(harness_run_id: str = "run-1", work_order_id: str = "wo-1",
           idempotency_key: str | None = None, event_type: str = "tool_called",
           **overrides) -> AuthoringExecutionEvent:
    payload = dict(
        event_id=f"evt-{uuid.uuid4().hex}", event_type=event_type, tenant_id="t1",
        work_order_id=work_order_id, harness_run_id=harness_run_id,
        idempotency_key=idempotency_key or f"key-{uuid.uuid4().hex}",
        executor="agent_field_harness", tool_name="retrieve_evidence", field_id="field-0",
        sanitized_payload={"evidence_ids": ["ev-1"], "issue_codes": []},
    )
    payload.update(overrides)
    return AuthoringExecutionEvent.model_validate(payload)


# ── AuthoringExecutionEvent ──────────────────────────────────────────────────


def test_event_model_rejects_unknown_types_and_unsanitized_payloads():
    with pytest.raises(Exception):
        _event(event_type="something_happened")
    with pytest.raises(Exception):
        _event(sanitized_payload={"prompt": "raw prompt"})
    with pytest.raises(Exception):
        AuthoringExecutionEvent.model_validate({
            "event_id": "evt-x", "event_type": "tool_called", "work_order_id": "wo-1",
            "harness_run_id": "run-1", "idempotency_key": "",
        })


def test_append_assigns_monotonic_sequence_inside_store_transaction(tmp_path):
    store = _store(tmp_path)
    _seed_work_order_and_run(store)
    first = store.append_execution_event(_event(idempotency_key="k1"))
    second = store.append_execution_event(_event(idempotency_key="k2"))
    assert (first.sequence, second.sequence) == (1, 2)
    events = store.list_execution_events("run-1")
    assert [e.sequence for e in events] == [1, 2]
    assert all(e.harness_run_id == "run-1" for e in events)


def test_duplicate_idempotency_key_replays_stored_event(tmp_path):
    store = _store(tmp_path)
    _seed_work_order_and_run(store)
    original = store.append_execution_event(_event(idempotency_key="same-key"))
    replay = store.append_execution_event(_event(idempotency_key="same-key"))
    assert replay.event_id == original.event_id
    assert replay.sequence == original.sequence
    assert len(store.list_execution_events("run-1")) == 1


def test_events_are_scoped_by_tenant_and_work_order(tmp_path):
    store = _store(tmp_path)
    _seed_work_order_and_run(store)
    store.append_execution_event(_event(idempotency_key="k1"))
    assert len(store.list_execution_events("run-1", tenant_id="t1")) == 1
    assert store.list_execution_events("run-1", tenant_id="t2") == []
    assert len(store.list_execution_events("run-1", work_order_id="wo-1")) == 1
    assert store.list_execution_events("run-1", work_order_id="wo-other") == []
    assert store.get_execution_event("run-1", "k1") is not None
    assert store.get_execution_event("run-1", "missing") is None


# ── EvidenceRegistry store API ───────────────────────────────────────────────


def _entry(harness_run_id: str = "run-1", evidence_id: str = "ev-1",
           content_hash: str = "ch-1") -> EvidenceRegistryEntry:
    return EvidenceRegistryEntry.model_validate(dict(
        evidence_id=evidence_id, tenant_id="t1", harness_run_id=harness_run_id,
        work_order_id="wo-1", knowledge_base_id="kb-1", source_set_snapshot_id="snap-1",
        snapshot_content_hash="sh-1", content_hash=content_hash,
        source_identity="ragflow://kb-1/doc-1", redacted_summary="MCU",
        reload_handle="handle-1",
    ))


def test_register_evidence_is_idempotent_and_immutable(tmp_path):
    store = _store(tmp_path)
    _seed_work_order_and_run(store)
    store.register_evidence(_entry())
    assert store.register_evidence(_entry()) is not None
    assert store.get_evidence_entry("run-1", "ev-1").content_hash == "ch-1"
    with pytest.raises(ValueError):
        store.register_evidence(_entry(content_hash="ch-2"))
    assert [e.evidence_id for e in store.list_evidence_entries("run-1")] == ["ev-1"]


def test_evidence_entries_are_scoped_per_run(tmp_path):
    store = _store(tmp_path)
    _seed_work_order_and_run(store)
    _seed_work_order_and_run(store, work_order_id="wo-2", harness_run_id="run-2")
    store.register_evidence(_entry(harness_run_id="run-1"))
    assert store.get_evidence_entry("run-1", "ev-1") is not None
    assert store.get_evidence_entry("run-2", "ev-1") is None


# ── NodeExecutionReceipt action_key/attempt ──────────────────────────────────


def _receipt(harness_run_id: str = "run-1", node_name: str = "retrieve_evidence",
             unit_id: str = "field-0", input_fingerprint: str = "fp-1",
             action_key: str = "ak-1", attempt: int = 1,
             receipt_id: str | None = None) -> NodeExecutionReceipt:
    return NodeExecutionReceipt.model_validate(dict(
        receipt_id=receipt_id or f"rc-{uuid.uuid4().hex}", harness_run_id=harness_run_id,
        node_name=node_name, unit_id=unit_id, input_fingerprint=input_fingerprint,
        action_key=action_key, attempt=attempt, fencing_token=1,
    ))


def test_receipt_persists_action_key_and_attempt(tmp_path):
    store = _store(tmp_path)
    _seed_work_order_and_run(store)
    receipt = _receipt()
    with store._connect() as conn:
        store._put(conn, "node_execution_receipts", {
            "receipt_id": receipt.receipt_id, "harness_run_id": receipt.harness_run_id,
            "node_name": receipt.node_name, "unit_id": receipt.unit_id,
            "input_fingerprint": receipt.input_fingerprint, "status": receipt.status,
            "action_key": receipt.action_key, "attempt": receipt.attempt,
        }, receipt)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT action_key, attempt FROM node_execution_receipts WHERE receipt_id = ?",
            (receipt.receipt_id,),
        ).fetchone()
    assert row["action_key"] == "ak-1"
    assert row["attempt"] == 1


def test_action_key_unique_index_blocks_duplicate_committed_actions(tmp_path):
    store = _store(tmp_path)
    _seed_work_order_and_run(store)
    import sqlite3
    with store._connect() as conn:
        store._put(conn, "node_execution_receipts", {
            "receipt_id": "rc-a", "harness_run_id": "run-1", "node_name": "n",
            "unit_id": "u", "input_fingerprint": "fp-a", "status": "committed",
            "action_key": "ak-dup", "attempt": 1,
        }, _receipt(receipt_id="rc-a"))
        with pytest.raises(sqlite3.IntegrityError):
            store._put(conn, "node_execution_receipts", {
                "receipt_id": "rc-b", "harness_run_id": "run-1", "node_name": "n2",
                "unit_id": "u2", "input_fingerprint": "fp-b", "status": "started",
                "action_key": "ak-dup", "attempt": 2,
            }, _receipt(receipt_id="rc-b", input_fingerprint="fp-b", action_key="ak-dup", attempt=2))


def test_legacy_receipts_without_action_key_remain_writable(tmp_path):
    store = _store(tmp_path)
    _seed_work_order_and_run(store)
    with store._connect() as conn:
        store._put(conn, "node_execution_receipts", {
            "receipt_id": "rc-old-1", "harness_run_id": "run-1", "node_name": "n",
            "unit_id": "u1", "input_fingerprint": "fp-1", "status": "committed",
            "action_key": "", "attempt": 1,
        }, _receipt(receipt_id="rc-old-1", unit_id="u1"))
        store._put(conn, "node_execution_receipts", {
            "receipt_id": "rc-old-2", "harness_run_id": "run-1", "node_name": "n",
            "unit_id": "u2", "input_fingerprint": "fp-2", "status": "committed",
            "action_key": "", "attempt": 1,
        }, _receipt(receipt_id="rc-old-2", unit_id="u2", input_fingerprint="fp-2"))
        rows = conn.execute(
            "SELECT COUNT(*) FROM node_execution_receipts WHERE action_key = ''"
        ).fetchone()[0]
    assert rows == 2
