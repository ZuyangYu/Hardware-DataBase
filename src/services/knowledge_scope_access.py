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


def source_dependencies(turn, dependency_loader=None, *, _seen=None) -> dict:
    """Return the complete transitive source set for a derived turn.

    A citation summary may name earlier turns whose sources were inherited.  A
    caller that can load those turns must expand the chain before authorizing
    history; without a loader the dependency is intentionally incomplete and
    callers fail closed.
    """
    summary = getattr(turn, "summary", None) or {}
    dependencies = summary.get("source_dependencies")
    if isinstance(dependencies, dict) and dependencies.get("version") == 1 and dependencies.get("complete") is True:
        ids = dependencies.get("kb_ids")
        if ids and isinstance(ids, list) and all(type(kb_id) is int and kb_id > 0 for kb_id in ids):
            inherited = [str(turn_id) for turn_id in (dependencies.get("inherited_turn_ids") or []) if str(turn_id)]
            if inherited and dependency_loader is None:
                return {"version": 1, "kb_ids": sorted(set(ids)), "inherited_turn_ids": inherited, "complete": False}
            seen = set(_seen or ())
            if str(getattr(turn, "id", "")) in seen:
                return {"version": 1, "kb_ids": sorted(set(ids)), "inherited_turn_ids": inherited, "complete": False}
            seen.add(str(getattr(turn, "id", "")))
            all_ids = set(ids)
            for inherited_id in inherited:
                parent = dependency_loader(inherited_id)
                if parent is None:
                    return {"version": 1, "kb_ids": sorted(all_ids), "inherited_turn_ids": inherited, "complete": False}
                parent_dependencies = source_dependencies(parent, dependency_loader, _seen=seen)
                if not parent_dependencies["complete"]:
                    return {"version": 1, "kb_ids": sorted(all_ids | set(parent_dependencies["kb_ids"])), "inherited_turn_ids": inherited, "complete": False}
                all_ids.update(parent_dependencies["kb_ids"])
            return {"version": 1, "kb_ids": sorted(all_ids), "inherited_turn_ids": inherited, "complete": True}
    scope = getattr(turn, "knowledge_scope", None) or {}
    return {"version": 1, "kb_ids": sorted({s["kb_id"] for s in scope.get("sources", [])}), "inherited_turn_ids": [], "complete": False}


def can_read_turn(auth, actor, turn, *, dependency_loader=None) -> bool:
    if not getattr(turn, "knowledge_selection_hash", ""):
        return True
    resolver = KnowledgeScopeResolver(auth)
    try:
        dependencies = source_dependencies(turn, dependency_loader)
        # An incomplete scope cannot be interpreted as an empty dependency set.
        if not dependencies["complete"]:
            return False
        for kb_id in dependencies["kb_ids"]:
            identity = auth.get_knowledge_base_identity(kb_id)
            if identity is None or identity.department_id is None:
                return False
            resolver.context_for_source(actor.id, AuthorizedKnowledgeSource(kb_id=kb_id, kb_name=identity.kb_name, department_id=str(identity.department_id), role="reference"))
        return True
    except (KnowledgeScopeDenied, ValueError):
        return False
