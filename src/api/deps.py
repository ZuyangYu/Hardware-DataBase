"""FastAPI dependencies: AppPipeline singleton, auth, RequestContext."""
from __future__ import annotations

import threading

from fastapi import Depends, Header, HTTPException

from src.core.app_pipeline import AppPipeline
from src.core.auth import ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN, AuthService, AuthUser
from src.pipelines.document_rag.schemas import RequestContext

from src.api.context import build_context_for_user

_pipeline: AppPipeline | None = None
_pipeline_lock = threading.Lock()

_auth_service: AuthService | None = None
_auth_service_lock = threading.Lock()


def get_pipeline() -> AppPipeline:
    """Process-wide AppPipeline singleton (mirrors Streamlit's @st.cache_resource).

    Lock-guarded: sync routes run in Starlette's threadpool, so after
    reset_pipeline() (e.g. PUT /config) two concurrent requests could each see
    _pipeline is None and build duplicate pipelines -- each spawning its own
    parse-worker daemon competing for the same SQLite. The lock serialises
    construction; reads of the cached reference are lock-free after the first
    build.
    """
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                _pipeline = AppPipeline()
    return _pipeline


def reset_pipeline() -> None:
    """Clear the cached singleton so a stub can be installed / settings rebuilt.

    Also signals the old pipeline's parse worker to stop, so a reset (e.g. after
    PUT /config) doesn't leave an orphan worker competing with the next pipeline's
    worker for the same SQLite queue.
    """
    global _pipeline
    with _pipeline_lock:
        old = _pipeline
        _pipeline = None
    if old is not None:
        runtime = getattr(getattr(old, "backend", None), "runtime", None)
        stop = getattr(runtime, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        close = getattr(getattr(old, "backend", None), "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def get_auth_service() -> AuthService:
    """Process-wide AuthService singleton (mirrors get_pipeline).

    FastAPI caches dependency results per-request, but every request still
    built a fresh AuthService before this singleton existed -- redoing schema
    bootstrap work each time. Lock-guarded like get_pipeline because sync
    routes run in Starlette's threadpool.
    """
    global _auth_service
    if _auth_service is None:
        with _auth_service_lock:
            if _auth_service is None:
                _auth_service = AuthService()
    return _auth_service


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def current_user(
    authorization: str | None = Header(default=None),
    auth: AuthService = Depends(get_auth_service),
) -> AuthUser:
    token = bearer_token(authorization)
    user = auth.get_user_by_token(token) if token else None
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated; run `hardware-database login`")
    return user


def require_dept_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role != ROLE_DEPT_ADMIN:
        raise HTTPException(status_code=403, detail="department admin role required")
    return user


def require_system_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="system admin role required")
    return user


def require_any_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role not in (ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN):
        raise HTTPException(status_code=403, detail="admin role required")
    return user


# Standard error used by every KB-content endpoint when a system_admin tries
# to reach it. system_admin is a governance role by design (manage departments,
# users, KB mounting, config, logs, evaluation) and does NOT get access to KB
# contents -- that would let the platform admin silently read every department's
# private data. See CLAUDE.md > "角色权力分离". Streamlit enforces the same
# split at the tab level (system_admin sees governance/logs/eval tabs only).
SYSTEM_ADMIN_KB_CONTENT_FORBIDDEN = (
    "system_admin 是治理角色,不能访问知识库内容;请用 dept_admin 或 user 账号"
)


def reject_system_admin_kb_access(ctx: RequestContext) -> None:
    """Raise 403 with a specific message if a system_admin is trying to touch
    KB contents. Call this at the top of any route whose semantics require KB
    content access (retrieval / files / upload / parse tasks / permissions)."""
    if ctx.is_system_admin():
        raise HTTPException(status_code=403, detail=SYSTEM_ADMIN_KB_CONTENT_FORBIDDEN)


def build_ctx(user: AuthUser, kb_name: str | None = None, auth: AuthService | None = None) -> RequestContext:
    """Convenience wrapper kept for symmetry with the deps module."""
    return build_context_for_user(user, kb_name=kb_name, auth=auth)
