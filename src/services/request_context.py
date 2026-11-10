"""Authenticated request-context construction shared outside the API layer."""

from __future__ import annotations

from src.core.auth import AuthService, AuthUser, build_request_context
from src.pipelines.document_rag.schemas import RequestContext
from src.services.knowledge_scope_models import AuthorizedKnowledgeSource


def build_context_for_knowledge_source(
    user: AuthUser,
    source: AuthorizedKnowledgeSource,
    auth: AuthService | None = None,
    *,
    tenant_key: str = "default",
) -> RequestContext:
    from src.services.knowledge_scope import KnowledgeScopeResolver

    return KnowledgeScopeResolver(auth or AuthService(), tenant_key=tenant_key).context_for_source(
        user.id, source
    )


class _SessionState(dict):
    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value) -> None:
        self[name] = value


def build_context_for_user(
    user: AuthUser,
    kb_name: str | None = None,
    auth: AuthService | None = None,
) -> RequestContext:
    auth = auth or AuthService()
    session_state = _SessionState(
        {
            "username": user.username,
            "department_id": user.department_id,
            "current_kb_department_id": user.department_id,
            "current_kb_id": None,
        }
    )
    if kb_name:
        session_state["current_kb_id"] = auth.get_knowledge_base_id(
            kb_name, department_id=user.department_id
        )
    return build_request_context(session_state)


__all__ = ["build_context_for_knowledge_source", "build_context_for_user"]
