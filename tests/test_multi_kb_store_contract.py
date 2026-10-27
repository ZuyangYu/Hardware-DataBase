"""R3 T2: the same durable scope contract on SQLite and real PostgreSQL."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

import src.settings as settings
from src.core.auth import AuthService
from src.core.conversation import ConversationService
from src.services.knowledge_scope_models import (
    AuthorizedKnowledgeSource, KnowledgeScopeSnapshot, KnowledgeSelectionInput, selection_hash,
)


def snapshot(user_id, anchor=101, reference=205):
    selection = KnowledgeSelectionInput(purpose="answer", sources=[
        {"kb_id": anchor, "role": "primary"}, {"kb_id": reference, "role": "reference"},
    ])
    return KnowledgeScopeSnapshot(
        snapshot_id=f"snapshot-{reference}", tenant_key="default", actor_user_id=user_id,
        anchor_kb_id=anchor, selection_hash=selection_hash(selection, tenant_key="default", actor_user_id=user_id, anchor_kb_id=anchor),
        purpose="answer", sources=tuple(AuthorizedKnowledgeSource(kb_id=s.kb_id, kb_name=f"KB{s.kb_id}", department_id="7", role=s.role) for s in selection.sources),
        authorized_at="2026-10-15T00:00:00+00:00",
    )


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        auth = AuthService(db_path=str(tmp_path / "auth.db"))
        actor = auth.get_user_by_username(settings.AUTH_DEFAULT_ADMIN_USERNAME)
        yield ConversationService(str(tmp_path / "auth.db")), actor.id
        return
    dsn = os.getenv("HDB_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("HDB_TEST_POSTGRES_DSN is not configured")
    import uuid
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from src.storage.control_plane import apply_schema
    from src.storage.control_plane.conversation_service import PostgresConversationService

    name = "multi_kb_" + uuid.uuid4().hex[:12]
    params = conninfo_to_dict(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(make_conninfo(**{**params, "dbname": name}), autocommit=True) as conn:
            apply_schema(conn, include_indexes=True)
            from src.quality.store import initialize_schema
            from src.storage.control_plane.sql_adapter import PgSqlAdapterConnection
            from psycopg.rows import dict_row
            conn.row_factory = dict_row
            initialize_schema(PgSqlAdapterConnection(conn))
        service = PostgresConversationService(make_conninfo(**{**params, "dbname": name}))
        try:
            yield service, 1
        finally:
            service.close()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def create(service, user, scope=None):
    return service.create_session(user, "KB101", department_id=7, kb_id=101, knowledge_selection=scope.selection() if scope else None)


def test_scope_roundtrip_survives_list_claim_and_new_service(store):
    service, user = store
    scope = snapshot(user)
    session = create(service, user, scope)
    assert session.knowledge_selection == scope.selection().model_dump(mode="json")
    assert session.knowledge_selection_hash == scope.selection_hash
    turn = service.create_turn(user, session.id, "compare supplies", knowledge_scope=scope)
    assert turn.knowledge_scope == scope.model_dump(mode="json")
    assert turn.knowledge_selection_hash == scope.selection_hash
    assert turn.summary["source_dependencies"] == {
        "version": 1, "kb_ids": [101, 205], "inherited_turn_ids": [], "complete": True,
    }
    assert len(turn.knowledge_request_fingerprint) == 64
    assert service.list_sessions(user)[0].knowledge_selection_hash == scope.selection_hash
    claimed = service.claim_turn(user, turn.id, "worker-new")
    assert claimed.knowledge_scope == scope.model_dump(mode="json")
    assert service.get_turn(user, turn.id).knowledge_request_fingerprint == turn.knowledge_request_fingerprint
    if hasattr(service, "db_path"):
        fresh = ConversationService(service.db_path)
    else:
        from src.storage.control_plane.conversation_service import PostgresConversationService
        fresh = PostgresConversationService(service.store.dsn)
    try:
        assert fresh.get_turn(user, turn.id).knowledge_scope == scope.model_dump(mode="json")
        assert fresh.get_session(user, session.id).knowledge_selection_hash == scope.selection_hash
    finally:
        if hasattr(fresh, "close"):
            fresh.close()


def test_legacy_defaults_and_replay_remain_compatible(store):
    service, user = store
    session = create(service, user)
    turn = service.create_turn(user, session.id, "old", client_request_id="old-key")
    replay = service.create_turn(user, session.id, "different legacy text", client_request_id="old-key")
    assert turn.id == replay.id
    assert session.knowledge_selection is None
    assert turn.knowledge_scope is None
    assert turn.knowledge_selection_hash == turn.knowledge_request_fingerprint == ""


def test_history_citations_preserve_source_readback_identity(store):
    service, user = store
    scope = snapshot(user)
    session = create(service, user, scope)
    turn = service.create_turn(user, session.id, "voltage", knowledge_scope=scope)
    metadata = {
        "citation_number": 1, "kb_id": 205, "kb_name": "KB205", "local_record_id": 7,
        "source_content_hash": "a" * 64, "source_version_id": "version-2",
        "revision": "B", "approval_status": "pending", "index_revision": 3,
        "processing_artifact_id": "", "processor_version": "", "evidence_text_hash": "b" * 64,
        "knowledge_source": {"kb_id": 205, "kb_name": "KB205", "role": "reference"},
    }
    summary = {**turn.summary, "evidence": [{
        "id": "document:205:claim", "source_name": "spec.pdf", "content_kind": "document_text",
        "content": "raw evidence " * 100, "locator": {"chunk_id": "chunk-2"},
        "metadata": {**metadata, "internal_diagnostics": "not part of citation readback"},
    }]}
    service.complete_turn(user, turn.id, "The candidate is 12 V [1].", summary)

    message = next(row for row in service.list_messages(user, session.id) if row.id == turn.assistant_message_id)

    assert message.citations[0]["metadata"] == metadata
    assert message.citations[0]["locator"] == {"chunk_id": "chunk-2"}
    assert len(message.citations[0]["content"]) == 240


def test_new_protocol_replay_ignores_snapshot_nonce_and_authorization_time(store):
    service, user = store
    scope = snapshot(user)
    session = create(service, user, scope)
    first = service.create_turn(user, session.id, "question", client_request_id="retry", knowledge_scope=scope)
    replay = service.create_turn(user, session.id, "question", client_request_id="retry", knowledge_scope=scope.model_copy(update={"snapshot_id": "fresh", "authorized_at": "2026-10-16T00:00:00Z"}))
    assert replay.id == first.id
    assert len(service.list_messages(user, session.id)) == 2


@pytest.mark.parametrize("change", ["query", "scope", "document_context", "wiki_assist"])
def test_same_key_with_different_new_request_is_conflict(store, change):
    service, user = store
    scope = snapshot(user)
    session = create(service, user, scope)
    service.create_turn(user, session.id, "question", client_request_id="same", knowledge_scope=scope)
    kwargs = {"query": "question", "client_request_id": "same", "knowledge_scope": scope}
    kwargs.update({
        "query": {"query": "different"}, "scope": {"knowledge_scope": snapshot(user, reference=306)},
        "document_context": {"document_context": {"template_id": "new"}},
        "wiki_assist": {"wiki_assist": True},
    }[change])
    with pytest.raises(ValueError, match="idempotency_conflict"):
        service.create_turn(user, session.id, **kwargs)
    assert len(service.list_messages(user, session.id)) == 2


def test_selection_can_change_between_turns_and_must_have_fresh_snapshot(store):
    service, user = store
    scope = snapshot(user)
    session = create(service, user, scope)
    with pytest.raises(ValueError, match="knowledge_scope_required"):
        service.create_turn(user, session.id, "missing scope")
    first = service.create_turn(user, session.id, "first", knowledge_scope=scope)
    second_scope = snapshot(user, reference=306)
    second = service.create_turn(user, session.id, "second", knowledge_scope=second_scope)
    assert first.knowledge_scope["selection_hash"] == scope.selection_hash
    assert second.knowledge_scope["selection_hash"] == second_scope.selection_hash
    assert service.get_session(user, session.id).knowledge_selection_hash == second_scope.selection_hash


def test_concurrent_turns_keep_their_own_selection_snapshots(store):
    service, user = store
    session = create(service, user)

    def submit(reference):
        scope = snapshot(user, reference=reference)
        return service.create_turn(user, session.id, f"first-{reference}", knowledge_scope=scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        turns = list(pool.map(submit, [205, 306]))
    assert len(turns) == 2
    assert {turn.knowledge_scope["selection_hash"] for turn in turns} == {
        snapshot(user, reference=205).selection_hash,
        snapshot(user, reference=306).selection_hash,
    }
    assert service.get_session(user, session.id).knowledge_selection_hash in {
        turn.knowledge_scope["selection_hash"] for turn in turns
    }
    assert len(service.list_messages(user, session.id)) == 4


def test_can_attach_multiscope_to_existing_legacy_history(store):
    service, user = store
    session = create(service, user)
    service.create_turn(user, session.id, "old")
    scope = snapshot(user)
    turn = service.create_turn(user, session.id, "new", knowledge_scope=scope)
    assert turn.knowledge_scope["selection_hash"] == scope.selection_hash
    assert service.get_session(user, session.id).knowledge_selection_hash == scope.selection_hash
    assert len(service.list_messages(user, session.id)) == 4


def test_snapshot_actor_anchor_and_hash_are_checked_before_any_writes(store):
    service, user = store
    scope = snapshot(user)
    session = create(service, user)
    for changes in [{"actor_user_id": user + 1}, {"anchor_kb_id": 999}, {"selection_hash": "0" * 64}]:
        with pytest.raises(ValueError):
            service.create_turn(user, session.id, "bad", knowledge_scope=scope.model_copy(update=changes))
    assert service.list_messages(user, session.id) == []


def test_fingerprint_binds_attachment_ids_and_scope_without_volatile_snapshot_fields():
    from src.services.knowledge_scope_persistence import request_fingerprint

    inputs = {"scope": snapshot(1).model_dump(mode="json"), "query": "question", "source_scope": "attachment_only",
              "attachment_ids": ["attachment-a", "attachment-b"], "document_context": {}, "query_mode": "deep", "wiki_assist": False}
    original = request_fingerprint(**inputs)
    assert original != request_fingerprint(**{**inputs, "source_scope": "attachment_and_knowledge_base"})
    assert original != request_fingerprint(**{**inputs, "attachment_ids": ["attachment-c"]})
    assert original == request_fingerprint(**{**inputs, "attachment_ids": ["attachment-b", "attachment-a"]})


def test_migration_check_does_not_write_and_apply_is_idempotent(tmp_path, capsys):
    import sqlite3
    from scripts.production_hardening.migrate_multi_kb_scope import main

    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE chat_turns (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO chat_sessions VALUES (7)")
        conn.commit()
    before = path.read_bytes()
    assert main(["--backend", "sqlite", "--db", str(path)]) == 1
    assert path.read_bytes() == before
    assert main(["--backend", "sqlite", "--db", str(path), "--check"]) == 1
    assert path.read_bytes() == before
    assert main(["--backend", "sqlite", "--db", str(path), "--apply"]) == 0
    migrated = path.read_bytes()
    assert main(["--backend", "sqlite", "--db", str(path), "--apply"]) == 0
    assert path.read_bytes() == migrated
    assert main(["--backend", "sqlite", "--db", str(path), "--verify"]) == 0
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT knowledge_selection_json, knowledge_selection_hash FROM chat_sessions WHERE id=7").fetchone() == ("{}", "")


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_postgres_migration_preserves_legacy_rows_and_rejects_future_version(store, capsys):
    service, user = store
    import psycopg
    from scripts.production_hardening.migrate_multi_kb_scope import main
    from src.storage.control_plane.knowledge_scope_migration import SQLITE_COLUMNS
    from src.storage.control_plane import record_schema_version

    session = service.create_session(user, "KB101", department_id=7, kb_id=101)
    dsn = service.store.dsn
    with psycopg.connect(dsn) as conn:
        record_schema_version(conn)
        for table, columns in SQLITE_COLUMNS.items():
            for column in columns:
                conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        conn.execute("UPDATE control_plane_schema SET value='1' WHERE key='schema_version'")
    assert main(["--backend", "postgres", "--dsn", dsn, "--check"]) == 1
    assert main(["--backend", "postgres", "--dsn", dsn, "--apply"]) == 0
    assert service.get_session(user, session.id).knowledge_selection is None
    with psycopg.connect(dsn) as conn:
        version_before = conn.execute("SELECT * FROM control_plane_schema ORDER BY key").fetchall()
    assert main(["--backend", "postgres", "--dsn", dsn, "--apply"]) == 0
    assert main(["--backend", "postgres", "--dsn", dsn, "--verify"]) == 0
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT * FROM control_plane_schema ORDER BY key").fetchall() == version_before
        conn.execute("UPDATE control_plane_schema SET value='99' WHERE key='schema_version'")
    assert main(["--backend", "postgres", "--dsn", dsn, "--apply"]) == 2
    assert "postgresql://" not in capsys.readouterr().out


def test_failed_sqlite_migration_rolls_back_all_ddl(tmp_path):
    import sqlite3
    from scripts.production_hardening.migrate_multi_kb_scope import main

    path = tmp_path / "incomplete.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY)")
    assert main(["--backend", "sqlite", "--db", str(path), "--apply"]) == 2
    with closing(sqlite3.connect(path)) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(chat_sessions)")] == ["id"]
