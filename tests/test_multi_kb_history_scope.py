"""R3 T3: source dependencies protect derived history, even without citations."""

from __future__ import annotations

from tests.test_multi_kb_turn_flow import create
from tests import test_knowledge_scope as scope_fixtures

pytest_plugins = ("tests.test_multi_kb_turn_flow",)

auth_scope = scope_fixtures.auth_scope


def test_revoke_hides_mixed_body_and_leaves_proven_single_source_message_visible(api):
    client, auth, actor, owner, ids, conv, _ = api
    session_id = create(client, ids).json()["id"]
    url = f"/api/v1/conversations/{session_id}/turns"
    first = client.post(url, json={"query": "only A"}).json()["turn"]
    conv.complete_turn(actor.id, first["id"], "A_ONLY_RESULT", {"source_dependencies": {
        "version": 1, "kb_ids": [ids["A"]], "inherited_turn_ids": [], "complete": True,
    }})
    mixed = client.post(url, json={"query": "uses B without final citation"}).json()["turn"]
    conv.complete_turn(actor.id, mixed["id"], "B_SECRET_VALUE", {"evidence": [], "source_dependencies": {
        "version": 1, "kb_ids": [ids["A"], ids["B"]], "inherited_turn_ids": [first["id"]], "complete": True,
    }})
    auth.set_knowledge_base_sharing("B", owner, False)
    response = client.get(f"/api/v1/conversations/{session_id}/messages")
    assert response.status_code == 200
    assert "A_ONLY_RESULT" in response.text
    assert "B_SECRET_VALUE" not in response.text
    hidden = next(message for message in response.json() if message["id"] == mixed["assistant_message_id"])
    assert hidden["source_access_restricted"] is True
    assert hidden["citations"] == hidden["cards"] == hidden["trace_steps"] == []
    assert "B_SECRET_VALUE" not in client.get(f"/api/v1/turns/{mixed['id']}").text


def test_incomplete_dependencies_conservatively_check_full_selection(api):
    client, auth, actor, owner, ids, conv, _ = api
    session_id = create(client, ids).json()["id"]
    turn = client.post(f"/api/v1/conversations/{session_id}/turns", json={"query": "old partial"}).json()["turn"]
    conv.complete_turn(actor.id, turn["id"], "UNTRACKED_PRIVATE_RESULT", {})
    auth.set_knowledge_base_sharing("B", owner, False)
    assert "UNTRACKED_PRIVATE_RESULT" not in client.get(f"/api/v1/conversations/{session_id}/messages").text


def test_revoked_inherited_dependency_hides_turn_even_when_direct_ids_omit_it(api):
    client, auth, actor, owner, ids, conv, _ = api
    session_id = create(client, ids).json()["id"]
    url = f"/api/v1/conversations/{session_id}/turns"
    parent = client.post(url, json={"query": "reference only"}).json()["turn"]
    conv.complete_turn(actor.id, parent["id"], "REFERENCE_SECRET", {"source_dependencies": {
        "version": 1, "kb_ids": [ids["B"]], "inherited_turn_ids": [], "complete": True,
    }})
    child = client.post(url, json={"query": "derived from reference"}).json()["turn"]
    conv.complete_turn(actor.id, child["id"], "DERIVED_SECRET", {"source_dependencies": {
        "version": 1, "kb_ids": [ids["A"]], "inherited_turn_ids": [parent["id"]], "complete": True,
    }})
    auth.set_knowledge_base_sharing("B", owner, False)
    assert "DERIVED_SECRET" not in client.get(f"/api/v1/conversations/{session_id}/messages").text


def test_sse_rejects_revoked_scope_before_replaying_events(api):
    client, auth, actor, owner, ids, conv, _ = api
    session_id = create(client, ids).json()["id"]
    turn = client.post(
        f"/api/v1/conversations/{session_id}/turns", json={"query": "streamed secret"}
    ).json()["turn"]
    conv.complete_turn(actor.id, turn["id"], "PRIVATE_EVENT", {})
    auth.set_knowledge_base_sharing("B", owner, False)

    response = client.get(f"/api/v1/turns/{turn['id']}/events")

    assert response.status_code == 403
    assert "knowledge_scope_denied" in response.text
    assert "PRIVATE_EVENT" not in response.text


def test_start_and_active_turn_views_redact_revoked_scope(api):
    client, auth, _, owner, ids, _, _ = api
    session_id = create(client, ids).json()["id"]
    turn = client.post(
        f"/api/v1/conversations/{session_id}/turns", json={"query": "revoked"}
    ).json()["turn"]
    auth.set_knowledge_base_sharing("B", owner, False)

    start = client.post(f"/api/v1/turns/{turn['id']}/start")
    active = client.get(f"/api/v1/conversations/{session_id}/turns")

    assert start.status_code == 403
    assert "knowledge_scope_denied" in start.text
    assert active.status_code == 200
    visible = next(item for item in active.json() if item["id"] == turn["id"])
    assert visible["source_access_restricted"] is True
    assert visible["knowledge_scope"] is None
