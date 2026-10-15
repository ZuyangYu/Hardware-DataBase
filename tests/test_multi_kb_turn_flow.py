"""R3 T3: authorized API scopes survive durable execution and reject replay drift."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.settings as settings
from src.agents.tools.retrieval_executor import initialize_retrieval_executor, reset_retrieval_executor
from src.api.app import create_app
from src.api.deps import current_user, get_auth_service, get_pipeline
from src.core.conversation import ConversationService
from tests import test_knowledge_scope as scope_fixtures
from tests._api_stub import StubPipeline

auth_scope = scope_fixtures.auth_scope


@pytest.fixture
def api(auth_scope, monkeypatch):
    auth, _, actor, owner, ids = auth_scope
    monkeypatch.setattr(settings, "AUTH_DB_PATH", auth.db_path)
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True)
    monkeypatch.setattr(settings, "RESOURCE_GOVERNOR_ENABLED", False)
    for flag in ("HDB_API_SPAWN_WORKER", "HDB_API_SPAWN_EXPORT_WORKER", "HDB_API_SPAWN_MEMORY_WORKER"):
        monkeypatch.setenv(flag, "0")
    reset_retrieval_executor()
    initialize_retrieval_executor()
    app = create_app()
    pipeline = StubPipeline()
    app.dependency_overrides[current_user] = lambda: actor
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, auth, actor, owner, ids, ConversationService(), pipeline
    reset_retrieval_executor(deadline_seconds=1)


def create(client, ids, **overrides):
    body = {"kb_name": "A", "knowledge_scope": scope_fixtures.selection_payload(ids["A"], ids["B"])}
    return client.post("/api/v1/conversations", json={**body, **overrides})


def test_create_session_and_turn_return_server_scope_and_persist_it(api):
    client, _, actor, _, ids, conv, _ = api
    response = create(client, ids)
    assert response.status_code == 200, response.text
    session = response.json()
    assert {s["kb_name"] for s in session["knowledge_scope"]["sources"]} == {"A", "B"}
    turn_response = client.post(f"/api/v1/conversations/{session['id']}/turns", json={"query": "compare supplies", "client_request_id": "first"})
    assert turn_response.status_code == 201, turn_response.text
    turn = turn_response.json()["turn"]
    stored = conv.get_turn(actor.id, turn["id"])
    assert stored.knowledge_selection_hash == session["knowledge_selection_hash"]
    assert len(stored.knowledge_scope["sources"]) == 2
    assert turn["knowledge_scope"]["selection_hash"] == stored.knowledge_selection_hash


def test_disabled_feature_does_not_silently_create_single_kb_session(api, monkeypatch):
    client, _, _, _, ids, _, _ = api
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", False)
    response = create(client, ids)
    assert response.status_code == 409
    assert "feature_unavailable" in response.text


@pytest.mark.parametrize("source", ["private", "other_A", "unknown"])
def test_any_denied_source_rejects_entire_api_request(api, source):
    client, _, _, _, ids, _, pipeline = api
    response = create(client, ids, knowledge_scope=scope_fixtures.selection_payload(ids["A"], ids.get(source, 999999)))
    assert response.status_code == 403
    assert "knowledge_scope_denied" in response.text
    assert not hasattr(pipeline, "last_thread_id")


def test_general_chat_cannot_attach_kb_selection(api):
    client, _, _, _, ids, _, _ = api
    assert create(client, ids, kb_name="__general__").status_code == 422


def test_new_protocol_replay_checks_request_and_range_fingerprints(api):
    client, _, _, _, ids, _, _ = api
    session_id = create(client, ids).json()["id"]
    url = f"/api/v1/conversations/{session_id}/turns"
    original = client.post(url, json={"query": "voltage", "client_request_id": "retry"})
    assert original.status_code == 201
    replay = client.post(url, json={"query": "voltage", "client_request_id": "retry"})
    assert replay.json()["turn"]["id"] == original.json()["turn"]["id"]
    conflict = client.post(url, json={"query": "current", "client_request_id": "retry"})
    assert conflict.status_code == 409
    assert "idempotency_conflict" in conflict.text


def test_changed_purpose_or_role_requires_new_session(api):
    client, _, _, _, ids, _, _ = api
    session_id = create(client, ids).json()["id"]
    url = f"/api/v1/conversations/{session_id}/turns"
    assert client.post(url, json={"query": "first"}).status_code == 201
    for selection in [scope_fixtures.selection_payload(ids["A"], ids["B"], purpose="compare"),
                      scope_fixtures.selection_payload(ids["A"], ids["B"], roles=["primary", "standard"])]:
        response = client.post(url, json={"query": "new purpose", "knowledge_scope": selection})
        assert response.status_code == 409
        assert "knowledge_scope_changed" in response.text


def test_revoked_reference_blocks_new_turn_before_pipeline_execution(api):
    client, auth, _, owner, ids, _, pipeline = api
    session_id = create(client, ids).json()["id"]
    auth.set_knowledge_base_sharing("B", owner, False)
    response = client.post(f"/api/v1/conversations/{session_id}/turns", json={"query": "must fail"})
    assert response.status_code == 403
    assert "knowledge_scope_denied" in response.text
    assert not hasattr(pipeline, "last_thread_id")


def test_old_worker_pending_sql_cannot_claim_multi_kb_protocol_turn(api):
    client, _, _, _, ids, conv, _ = api
    session_id = create(client, ids).json()["id"]
    turn_id = client.post(f"/api/v1/conversations/{session_id}/turns", json={"query": "new protocol"}).json()["turn"]["id"]
    from contextlib import closing
    with closing(conv._connect()) as conn:
        legacy_candidates = conn.execute("SELECT id FROM chat_turns WHERE status='pending'").fetchall()
    assert turn_id not in {row["id"] for row in legacy_candidates}
    assert conv.get_turn_unscoped(turn_id).status == "pending"
    assert turn_id in {turn.id for turn, _user_id in conv.list_pending_turn_work()}


def test_modern_worker_claims_multi_kb_turn_without_exposing_protocol_status(api):
    client, _, actor, _, ids, conv, _ = api
    session_id = create(client, ids).json()["id"]
    turn_id = client.post(
        f"/api/v1/conversations/{session_id}/turns", json={"query": "claim me"}
    ).json()["turn"]["id"]

    result = conv.claim_turn_result(actor.id, turn_id, worker_id="modern-worker")

    assert result.status == "claimed"
    assert result.turn is not None
    assert result.turn.status == "streaming"
    with conv._connect() as conn:
        stored = conn.execute("SELECT status FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
    assert stored["status"] == "multi_kb_streaming"
