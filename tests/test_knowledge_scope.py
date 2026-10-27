"""R3 T1: stable selection identity, complete authorization, isolated contexts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import src.settings as settings
from src.core.auth import AuthService, ROLE_EMPLOYEE


def selection_payload(*ids, purpose="answer", roles=None):
    roles = roles or (["peer"] * len(ids) if purpose == "compare" else ["primary"] + ["reference"] * (len(ids) - 1))
    return {"version": 1, "purpose": purpose, "sources": [
        {"kb_id": kb_id, "role": role} for kb_id, role in zip(ids, roles)
    ]}


@pytest.mark.parametrize("payload", [
    selection_payload(),
    selection_payload(1, 1),
    selection_payload(1, 2, 3, 4, 5),
    selection_payload(0),
    selection_payload(-1),
    selection_payload(True),
    selection_payload("1"),
    selection_payload(1.5),
    selection_payload(1, purpose="compare"),
    selection_payload(1, 2, purpose="compare", roles=["primary", "reference"]),
    selection_payload(1, 2, roles=["primary", "primary"]),
    selection_payload(1, 2, roles=["reference", "standard"]),
    selection_payload(1, 2, roles=["primary", "peer"]),
    selection_payload(1, roles=["owner"]),
    {**selection_payload(1), "version": 2},
    {**selection_payload(1), "tenant_key": "forged"},
    {**selection_payload(1), "sources": [{"kb_id": 1, "role": "primary", "kb_name": "forged"}]},
])
def test_selection_rejects_invalid_or_client_supplied_identity(payload):
    from src.services.knowledge_scope_models import KnowledgeSelectionInput

    with pytest.raises(ValidationError):
        KnowledgeSelectionInput.model_validate(payload)


def test_selection_limit_can_be_lowered_for_demo(monkeypatch):
    from src.services.knowledge_scope_models import KnowledgeSelectionInput

    monkeypatch.setattr(settings, "MULTI_KB_MAX_SOURCES", 2, raising=False)
    with pytest.raises(ValidationError):
        KnowledgeSelectionInput.model_validate(selection_payload(1, 2, 3))
    assert len(KnowledgeSelectionInput.model_validate(selection_payload(1, 2)).sources) == 2


def test_selection_is_deeply_frozen_and_serializes_sources_as_array():
    from src.services.knowledge_scope_models import KnowledgeSelectionInput

    selection = KnowledgeSelectionInput.model_validate(selection_payload(1, 2))
    assert isinstance(selection.sources, tuple)
    assert isinstance(selection.model_dump(mode="json")["sources"], list)
    with pytest.raises(ValidationError):
        selection.sources[0].role = "standard"
    with pytest.raises(ValidationError):
        selection.purpose = "compose"


def test_selection_hash_is_order_independent_and_binds_security_and_roles():
    from src.services.knowledge_scope_models import KnowledgeSelectionInput, selection_hash

    left = KnowledgeSelectionInput.model_validate(selection_payload(101, 205))
    right = KnowledgeSelectionInput.model_validate(selection_payload(205, 101, roles=["reference", "primary"]))
    scope = {"tenant_key": "default", "actor_user_id": 7, "anchor_kb_id": 101}
    original = selection_hash(left, **scope)
    assert len(original) == 64
    assert original == selection_hash(right, **scope)
    for key, value in [("tenant_key", "other"), ("actor_user_id", 8), ("anchor_kb_id", 205)]:
        assert original != selection_hash(left, **{**scope, key: value})
    for payload in [selection_payload(101, 205, purpose="compose"), selection_payload(101, 205, roles=["primary", "standard"])]:
        assert original != selection_hash(KnowledgeSelectionInput.model_validate(payload), **scope)


@pytest.fixture
def auth_scope(tmp_path):
    auth = AuthService(db_path=str(tmp_path / "scope-auth.db"))
    admin = auth.get_user_by_username(settings.AUTH_DEFAULT_ADMIN_USERNAME)
    dept = auth.create_department("engineering")
    other_dept = auth.create_department("other")
    actor = auth.create_user_as(admin, "reader", "password123", ROLE_EMPLOYEE, dept.id)
    owner = auth.create_user_as(admin, "owner", "password123", ROLE_EMPLOYEE, dept.id)
    outsider = auth.create_user_as(admin, "outsider", "password123", ROLE_EMPLOYEE, other_dept.id)
    auth.register_knowledge_base("A", owner=actor)
    auth.register_knowledge_base("B", owner=owner, is_shared=True)
    auth.register_knowledge_base("private", owner=owner)
    auth.register_knowledge_base("A", owner=outsider)
    ids = {name: auth.get_knowledge_base_id(name, department_id=dept.id) for name in ("A", "B", "private")}
    ids["other_A"] = auth.get_knowledge_base_id("A", department_id=other_dept.id)
    return auth, admin, actor, owner, ids


def resolve_scope(auth_scope, *, ids=None, purpose="answer", roles=None):
    from src.services.knowledge_scope import KnowledgeScopeResolver
    from src.services.knowledge_scope_models import KnowledgeSelectionInput

    auth, _, actor, _, kb_ids = auth_scope
    selection = KnowledgeSelectionInput.model_validate(selection_payload(*(ids or [kb_ids["A"], kb_ids["B"]]), purpose=purpose, roles=roles))
    resolver = KnowledgeScopeResolver(auth)
    return resolver, resolver.resolve(actor, "A", selection)


def test_public_identity_lookup_uses_id_even_for_duplicate_names(auth_scope):
    auth, _, actor, _, ids = auth_scope
    own = auth.get_knowledge_base_identity(ids["A"])
    other = auth.get_knowledge_base_identity(ids["other_A"])
    assert own.kb_id == ids["A"]
    assert own.kb_name == other.kb_name == "A"
    assert own.department_id == actor.department_id
    assert own.department_id != other.department_id
    assert auth.get_knowledge_base_identity(999999) is None


def test_resolver_authorizes_complete_set_and_returns_server_identity(auth_scope):
    _, _, actor, _, ids = auth_scope
    resolver, snapshot = resolve_scope(auth_scope)
    assert snapshot.anchor_kb_id == ids["A"]
    assert snapshot.actor_user_id == actor.id
    assert [(s.kb_id, s.kb_name, s.department_id, s.role) for s in snapshot.sources] == [
        (ids["A"], "A", str(actor.department_id), "primary"),
        (ids["B"], "B", str(actor.department_id), "reference"),
    ]
    assert resolver.revalidate(actor.id, snapshot) == snapshot.sources
    _, second = resolve_scope(auth_scope)
    assert snapshot.snapshot_id != second.snapshot_id
    assert snapshot.selection_hash == second.selection_hash


@pytest.mark.parametrize("denied_key", ["other_A", "private", "unknown"])
def test_any_denied_member_rejects_entire_selection_without_names(auth_scope, denied_key):
    from src.services.knowledge_scope import KnowledgeScopeDenied

    *_, ids = auth_scope
    with pytest.raises(KnowledgeScopeDenied) as error:
        resolve_scope(auth_scope, ids=[ids["A"], ids.get(denied_key, 999999)])
    assert str(error.value) == "knowledge_scope_denied"


def test_scope_rechecks_deactivation_and_system_admin_role(auth_scope):
    from src.services.knowledge_scope import KnowledgeScopeDenied, KnowledgeScopeResolver
    from src.services.knowledge_scope_models import KnowledgeSelectionInput

    auth, admin, actor, _, ids = auth_scope
    resolver, snapshot = resolve_scope(auth_scope)
    auth.set_user_active(actor.id, False)
    with pytest.raises(KnowledgeScopeDenied):
        resolver.revalidate(actor.id, snapshot)
    with pytest.raises(KnowledgeScopeDenied):
        resolver.resolve(actor, "A", KnowledgeSelectionInput.model_validate(selection_payload(ids["A"])))
    with pytest.raises(KnowledgeScopeDenied):
        KnowledgeScopeResolver(auth).resolve(admin, "A", KnowledgeSelectionInput.model_validate(selection_payload(ids["A"])))


def test_revalidation_refuses_revoked_shared_source_and_wrong_actor(auth_scope):
    from src.services.knowledge_scope import KnowledgeScopeDenied

    auth, _, actor, owner, _ = auth_scope
    resolver, snapshot = resolve_scope(auth_scope)
    with pytest.raises(KnowledgeScopeDenied):
        resolver.revalidate(owner.id, snapshot)
    auth.set_knowledge_base_sharing("B", owner, False)
    with pytest.raises(KnowledgeScopeDenied):
        resolver.revalidate(actor.id, snapshot)
    with pytest.raises(KnowledgeScopeDenied):
        resolver.context_for_source(actor.id, snapshot.sources[1])


def test_revalidation_refuses_tenant_or_selection_hash_tampering(auth_scope):
    from src.services.knowledge_scope import KnowledgeScopeDenied, KnowledgeScopeResolver

    auth, _, actor, _, _ = auth_scope
    resolver, snapshot = resolve_scope(auth_scope)
    with pytest.raises(KnowledgeScopeDenied):
        KnowledgeScopeResolver(auth, tenant_key="other").revalidate(actor.id, snapshot)
    with pytest.raises(KnowledgeScopeDenied):
        resolver.revalidate(actor.id, snapshot.model_copy(update={"selection_hash": "forged"}))


@pytest.mark.parametrize("anchor,source_keys,roles", [
    ("A", ["B"], ["primary"]),
    ("A", ["A", "B"], ["reference", "primary"]),
    ("__general__", ["A"], ["primary"]),
    ("", ["A"], ["primary"]),
])
def test_anchor_must_be_selected_and_primary(auth_scope, anchor, source_keys, roles):
    from src.services.knowledge_scope import KnowledgeScopeInvalid, KnowledgeScopeResolver
    from src.services.knowledge_scope_models import KnowledgeSelectionInput

    auth, _, actor, _, ids = auth_scope
    selection = KnowledgeSelectionInput.model_validate(selection_payload(*(ids[k] for k in source_keys), roles=roles))
    with pytest.raises(KnowledgeScopeInvalid):
        KnowledgeScopeResolver(auth).resolve(actor, anchor, selection)


def test_contexts_have_independent_metadata_and_only_own_source_permissions(auth_scope):
    from src.api.context import build_context_for_knowledge_source

    auth, _, actor, _, ids = auth_scope
    resolver, snapshot = resolve_scope(auth_scope)
    left = resolver.context_for_source(actor.id, snapshot.sources[0])
    right = build_context_for_knowledge_source(actor, snapshot.sources[1], auth=auth)
    assert left.metadata["kb_id"] == ids["A"]
    assert right.metadata["kb_id"] == ids["B"]
    assert left.has_kb_permission("A", "read")
    assert not left.has_kb_permission("B", "read")
    assert right.has_kb_permission("B", "read")
    assert not right.has_kb_permission("A", "read")
    left.metadata["kb_id"] = -1
    left.kb_permissions.clear()
    assert right.metadata["kb_id"] == ids["B"]
    assert right.has_kb_permission("B", "read")
    assert right.tenant_id == "default"


def test_legacy_resolution_never_synthesizes_ids_and_same_selection_guard(auth_scope):
    from src.services.knowledge_scope import KnowledgeScopeChanged, KnowledgeScopeDenied, KnowledgeScopeResolver, require_same_selection

    auth, _, actor, _, ids = auth_scope
    snapshot = KnowledgeScopeResolver(auth).resolve(actor, "A", None)
    assert [source.kb_id for source in snapshot.sources] == [ids["A"]]
    with pytest.raises(KnowledgeScopeDenied):
        KnowledgeScopeResolver(auth).resolve(actor, "unregistered", None)
    require_same_selection("", "new")
    require_same_selection("same", "same")
    with pytest.raises(KnowledgeScopeChanged, match="knowledge_scope_changed"):
        require_same_selection("old", "new")


@pytest.mark.parametrize("purpose", ["answer", "compare", "compose"])
def test_all_purposes_support_four_authorized_sources(auth_scope, purpose):
    from src.services.knowledge_scope_models import KnowledgeScopeSnapshot

    auth, _, actor, _, ids = auth_scope
    for name in ("D", "E"):
        auth.register_knowledge_base(name, owner=actor)
        ids[name] = auth.get_knowledge_base_id(name, department_id=actor.department_id)
    resolver, snapshot = resolve_scope(auth_scope, ids=[ids[k] for k in ("A", "B", "D", "E")], purpose=purpose)
    restored = KnowledgeScopeSnapshot.model_validate_json(snapshot.model_dump_json())
    assert len(resolver.revalidate(actor.id, restored)) == 4
    assert [s.role for s in restored.sources] == (["peer"] * 4 if purpose == "compare" else ["primary"] + ["reference"] * 3)


def test_standard_role_does_not_authorize_private_knowledge_base(auth_scope):
    from src.services.knowledge_scope import KnowledgeScopeDenied

    *_, ids = auth_scope
    with pytest.raises(KnowledgeScopeDenied):
        resolve_scope(auth_scope, ids=[ids["A"], ids["private"]], roles=["primary", "standard"])


def test_role_change_in_auth_database_invalidates_stale_actor(auth_scope):
    from contextlib import closing
    from src.services.knowledge_scope import KnowledgeScopeDenied

    auth, _, actor, _, _ = auth_scope
    resolver, snapshot = resolve_scope(auth_scope)
    with closing(auth._connect()) as conn:
        conn.execute("UPDATE users SET role = 'system_admin' WHERE id = ?", (actor.id,))
        conn.commit()
    with pytest.raises(KnowledgeScopeDenied):
        resolver.resolve(actor, "A", snapshot.selection())
    with pytest.raises(KnowledgeScopeDenied):
        resolver.revalidate(actor.id, snapshot)


@pytest.mark.parametrize("limit", ["0", "-1", "invalid"])
def test_settings_reject_invalid_source_limit(limit):
    with pytest.raises(ValueError):
        settings.validate_settings_values({"MULTI_KB_MAX_SOURCES": limit})
