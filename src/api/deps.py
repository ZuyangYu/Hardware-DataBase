"""FastAPI dependencies: AppPipeline singleton, auth, RequestContext."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from src.core.app_pipeline import AppPipeline
from src.core.auth import ROLE_DEPT_ADMIN, AuthService, AuthUser
from src.pipelines.document_rag.schemas import RequestContext

from src.api.context import build_context_for_user

_pipeline: AppPipeline | None = None


def get_pipeline() -> AppPipeline:
    """Process-wide AppPipeline singleton (mirrors Streamlit's @st.cache_resource)."""
    global _pipeline
    if _pipeline is None:
        _pipeline = AppPipeline()
    return _pipeline


def reset_pipeline() -> None:
    """Test hook: clear the cached singleton so a stub can be installed."""
    global _pipeline
    _pipeline = None


def get_auth_service() -> AuthService:
    return AuthService()


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


def build_ctx(user: AuthUser, kb_name: str | None = None, auth: AuthService | None = None) -> RequestContext:
    """Convenience wrapper kept for symmetry with the deps module."""
    return build_context_for_user(user, kb_name=kb_name, auth=auth)
