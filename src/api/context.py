"""Shared ``RequestContext`` construction for the API layer.

``build_context_for_user`` is the long-lived helper used by the HTTP API (and,
later, any separated frontend) to turn an already-authenticated ``AuthUser``
into a ``RequestContext``. It deliberately reuses ``build_request_context``
by feeding it a synthetic session_state, so the permission / role /
deactivation logic lives in exactly one place -- the Streamlit path and the
API path cannot drift apart.
"""
from __future__ import annotations

from src.core.auth import AuthUser, AuthService, build_request_context
from src.pipelines.document_rag.schemas import RequestContext


class _SessionState(dict):
    """Dict with attribute access, mimicking ``st.session_state``.

    ``build_request_context`` was written for Streamlit's session_state, which
    is a dict that also exposes keys as attributes (``ensure_session_id``
    writes ``session_state.session_id = ...``). A plain dict raises
    AttributeError on that assignment; this subclass lets the API reuse
    ``build_request_context`` verbatim without duplicating its logic.
    """

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
    """Build a ``RequestContext`` for an already-authenticated user.

    KB scope (``department_id`` / ``kb_id``) is resolved from the auth DB.
    Permissions in :class:`AuthService` are always department-scoped against
    the user's own department, so the resource department is the user's
    department and ``kb_id`` is looked up under it -- cross-department access
    is not reachable here, matching the Streamlit behaviour.
    """
    auth = auth or AuthService()
    session_state = _SessionState(
        {
            "username": user.username,
            "department_id": user.department_id,
            # Resource scope defaults to the user's own department; AuthService
            # only ever grants permissions on KBs in that department.
            "current_kb_department_id": user.department_id,
            "current_kb_id": None,
        }
    )
    if kb_name:
        session_state["current_kb_id"] = auth.get_knowledge_base_id(
            kb_name, department_id=user.department_id
        )
    # Reuse the Streamlit path verbatim: it re-fetches the user from the DB
    # (picking up live role / active state) and assembles allowed_kbs /
    # kb_permissions / metadata identically. A deactivated user collapses to
    # an anonymous context, which is the desired fail-closed behaviour.
    return build_request_context(session_state)
