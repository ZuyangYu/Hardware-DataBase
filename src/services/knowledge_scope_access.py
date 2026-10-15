"""Shared execution/history gates; citations alone never prove dependencies."""

from __future__ import annotations

from pydantic import ValidationError

from src.agents.tools.retrieval_executor import MultiKnowledgeRetrievalUnavailable, get_multi_kb_retrieval_executor
from src.services.knowledge_scope import KnowledgeScopeDenied, KnowledgeScopeInvalid, KnowledgeScopeResolver
from src.services.knowledge_scope_models import AuthorizedKnowledgeSource, KnowledgeSelectionInput


def multi_kb_thread_key(session_id: int, selection_hash: str) -> str:
    return f"session:{session_id}:knowledge:{selection_hash}"


def resolve_session_scope(auth, actor, session, requested=None, *, execute: bool = True):
    stored = getattr(session, "knowledge_selection", None)
    if requested is None and not stored:
        return None
    if execute:
        get_multi_kb_retrieval_executor()
    try:
        selection = requested if requested is not None else KnowledgeSelectionInput.model_validate(stored)
    except ValidationError as exc:
        raise KnowledgeScopeInvalid("knowledge_scope_invalid") from exc
    snapshot = KnowledgeScopeResolver(auth).resolve(actor, session.kb_name, selection)
    if execute and snapshot.purpose == "compose":
        import src.settings as settings
        if not bool(getattr(settings, "MULTI_KB_AUTHORING_ENABLED", False)):
            raise MultiKnowledgeRetrievalUnavailable("feature_unavailable")
    return snapshot


def source_dependencies(turn) -> dict:
    summary = getattr(turn, "summary", None) or {}
    dependencies = summary.get("source_dependencies")
    if isinstance(dependencies, dict) and dependencies.get("version") == 1 and dependencies.get("complete") is True:
        ids = dependencies.get("kb_ids")
        if ids and isinstance(ids, list) and all(type(kb_id) is int and kb_id > 0 for kb_id in ids):
            return {"version": 1, "kb_ids": sorted(set(ids)), "inherited_turn_ids": list(dependencies.get("inherited_turn_ids") or []), "complete": True}
    scope = getattr(turn, "knowledge_scope", None) or {}
    return {"version": 1, "kb_ids": sorted({s["kb_id"] for s in scope.get("sources", [])}), "inherited_turn_ids": [], "complete": False}


def can_read_turn(auth, actor, turn) -> bool:
    if not getattr(turn, "knowledge_selection_hash", ""):
        return True
    resolver = KnowledgeScopeResolver(auth)
    try:
        dependencies = source_dependencies(turn)
        # An incomplete scope cannot be interpreted as an empty dependency set.
        if not dependencies["complete"] and not dependencies["kb_ids"]:
            return False
        for kb_id in dependencies["kb_ids"]:
            identity = auth.get_knowledge_base_identity(kb_id)
            if identity is None or identity.department_id is None:
                return False
            resolver.context_for_source(actor.id, AuthorizedKnowledgeSource(kb_id=kb_id, kb_name=identity.kb_name, department_id=str(identity.department_id), role="reference"))
        return True
    except (KnowledgeScopeDenied, ValueError):
        return False
