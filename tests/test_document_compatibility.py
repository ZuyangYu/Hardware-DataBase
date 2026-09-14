from __future__ import annotations

import sqlite3

import pytest

import src.settings
from src.document_authoring.compatibility import (
    CompatibilityClosureError,
    DocumentAuthoringCompatibilityService,
    DocumentAuthoringCompatibilityStore,
    create_sqlite_backup,
    restore_sqlite_backup,
)
from src.document_authoring.generation_sessions import GenerationSessionStore
from src.document_authoring.models import DocumentSchema
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore


def test_compatibility_telemetry_is_durable_idempotent_and_low_content(tmp_path):
    store = DocumentAuthoringCompatibilityStore(tmp_path / "compatibility.db")
    service = DocumentAuthoringCompatibilityService(store=store)

    service.record_legacy_direct_execution(
        operation="legacy_generation_session", tenant_id="tenant-a", entity_id="session-1",
    )
    service.record_plan_backed_execution(
        operation="plan_submission", tenant_id="tenant-a", entity_id="plan-1",
        plan_id="plan-1", plan_version=2, plan_hash="sha256:plan-1",
    )
    service.record_auto_confirmation_attempt(
        operation="confirm", tenant_id="tenant-a", entity_id="session-1", outcome="blocked",
    )
    service.record_new_write(
        operation="plan_submission", tenant_id="tenant-a", entity_id="plan-1", route="plan_backed",
    )
    service.record_legacy_write(
        operation="legacy_generation_session", tenant_id="tenant-a", entity_id="session-1",
    )

    counters = service.counter_snapshot()
    assert counters["legacy_direct_execution"] == 1
    assert counters["plan_backed_execution"] == 1
    assert counters["auto_confirmation_attempt"] == 1
    assert counters["new_write"] == 1
    assert counters["legacy_write"] == 1
    assert all("raw_content" not in event.payload for event in store.list_events())

    replay = service.record_legacy_direct_execution(
        operation="legacy_generation_session", tenant_id="tenant-a", entity_id="session-1",
        idempotency_key="legacy-event-1",
    )
    replay_again = service.record_legacy_direct_execution(
        operation="legacy_generation_session", tenant_id="tenant-a", entity_id="session-1",
        idempotency_key="legacy-event-1",
    )
    assert replay.event_id == replay_again.event_id
    assert service.counter_snapshot()["legacy_direct_execution"] == 2

    with pytest.raises(ValueError, match="forbidden"):
        service.record_event(
            event_type="legacy_direct_execution", operation="bad", entity_id="x",
            payload={"raw_content": "must not persist"}, idempotency_key="unsafe",
        )


def test_historical_backfill_markers_never_fabricate_plan_hash(tmp_path):
    store = DocumentAuthoringCompatibilityStore(tmp_path / "compatibility.db")
    service = DocumentAuthoringCompatibilityService(store=store)

    marker = service.mark_legacy(
        entity_type="generation_session", entity_id="legacy-session-1",
        reason="created_before_plan_v2", source_fingerprint="sha256:legacy-session",
    )
    assert marker.legacy_status == "legacy"
    assert marker.plan_id is None
    assert marker.plan_hash is None
    assert marker.source_fingerprint == "sha256:legacy-session"
    assert service.backfill_legacy_records(
        "work_order", [{"work_order_id": "legacy-order-1", "input_fingerprint": "sha256:wo"}],
    )[0].plan_hash is None

    with pytest.raises(ValueError, match="fabricate"):
        service.mark_legacy(
            entity_type="artifact", entity_id="legacy-artifact",
            reason="old", plan_hash="sha256:not-real",
        )


def test_closure_requires_accepted_output_spec_and_plan_references(tmp_path, monkeypatch):
    store = DocumentAuthoringCompatibilityStore(tmp_path / "compatibility.db")
    service = DocumentAuthoringCompatibilityService(store=store)
    monkeypatch.setattr(src.settings, "DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED", True, raising=False)

    with pytest.raises(CompatibilityClosureError, match="accepted"):
        service.validate_new_document_write(
            operation="create_work_order",
            output_spec={"status": "proposed", "output_spec_id": "spec-1", "version": 1, "content_hash": "sha256:spec"},
            document_plan={"status": "accepted", "document_plan_id": "plan-1", "version": 1, "plan_hash": "sha256:plan", "output_spec_id": "spec-1", "output_spec_version": 1, "output_spec_hash": "sha256:spec"},
        )

    refs = service.validate_new_document_write(
        operation="create_work_order",
        output_spec={"status": "accepted", "output_spec_id": "spec-1", "version": 1, "content_hash": "sha256:spec"},
        document_plan={"status": "accepted", "document_plan_id": "plan-1", "version": 1, "plan_hash": "sha256:plan", "output_spec_id": "spec-1", "output_spec_version": 1, "output_spec_hash": "sha256:spec"},
    )
    assert refs.document_plan_id == "plan-1"
    assert refs.output_spec_hash == "sha256:spec"

    with pytest.raises(CompatibilityClosureError, match="direct"):
        service.validate_new_document_write(operation="create_work_order")


def test_backup_restore_and_two_release_no_write_readiness(tmp_path):
    database = tmp_path / "compatibility.db"
    store = DocumentAuthoringCompatibilityStore(database)
    service = DocumentAuthoringCompatibilityService(store=store)
    service.record_release_observation(
        release_label="release-1", backup_verified=True, restore_verified=True,
        old_fields_read_only=False, legacy_write_count=0,
    )
    service.record_release_observation(
        release_label="release-2", backup_verified=True, restore_verified=True,
        old_fields_read_only=True, legacy_write_count=0,
    )
    readiness = service.evaluate_closure_readiness()
    assert readiness.ready is True
    assert readiness.release_count == 2

    backup = tmp_path / "compatibility.backup.db"
    report = create_sqlite_backup(database, backup)
    assert report.verified is True
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('changed')")
    restored = restore_sqlite_backup(database, backup, safety_backup_path=tmp_path / "safety.db")
    assert restored.verified is True
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='sentinel'").fetchone() is None


def test_readiness_rejects_a_release_with_legacy_writes(tmp_path):
    service = DocumentAuthoringCompatibilityService(
        store=DocumentAuthoringCompatibilityStore(tmp_path / "compatibility.db")
    )
    service.record_release_observation(
        release_label="release-1", backup_verified=True, restore_verified=True,
        old_fields_read_only=True, legacy_write_count=1,
    )
    service.record_release_observation(
        release_label="release-2", backup_verified=True, restore_verified=True,
        old_fields_read_only=True, legacy_write_count=0,
    )
    readiness = service.evaluate_closure_readiness()
    assert readiness.ready is False
    assert "legacy_writes_observed" in readiness.reasons


def test_closure_blocks_new_messages_on_legacy_generation_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        src.settings,
        "DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED",
        False,
        raising=False,
    )
    sessions = GenerationSessionStore(str(tmp_path / "authoring.db"))
    session = sessions.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="kb-a",
        template_version_id="template-v1",
    )
    monkeypatch.setattr(
        src.settings,
        "DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED",
        True,
        raising=False,
    )

    with pytest.raises(CompatibilityClosureError, match="direct GenerationBrief"):
        sessions.append_message(
            session.session_id,
            role="user",
            content="should be rejected",
        )

    assert sessions.get_session(session.session_id).messages == []


def test_plan_backed_schema_bypass_requires_accepted_plan_references(tmp_path, monkeypatch):
    monkeypatch.setattr(
        src.settings,
        "DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED",
        True,
        raising=False,
    )
    store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=store)
    schema = DocumentSchema(
        document_schema_id="schema-1",
        version="1",
        document_type="generic",
        status="approved",
    )

    with pytest.raises(CompatibilityClosureError, match="accepted OutputSpec"):
        service.register_document_schema(schema, plan_backed=True)

    assert store.get_document_schema("schema-1", "1") is None
