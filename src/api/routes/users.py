"""User management endpoints (system_admin / dept_admin).

Scoping is enforced by :class:`AuthService` methods, which read the caller's
role and department off ``actor``. `system_admin` sees all users;
`dept_admin` is confined to plain users in its own department.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.core.auth import AuthService, AuthUser

from src.api.deps import get_auth_service, require_any_admin
from src.api.schemas import (
    AuthUserView,
    CreateUserRequest,
    OkResponse,
    ResetPasswordRequest,
    SetUserActiveRequest,
)

router = APIRouter(tags=["users"])


def _user_view(u: AuthUser) -> AuthUserView:
    return AuthUserView(
        id=u.id,
        username=u.username,
        role=u.role,
        is_active=u.is_active,
        department_id=u.department_id,
        department_name=u.department_name,
    )


@router.get("/users", response_model=list[AuthUserView])
def list_users(
    actor: AuthUser = Depends(require_any_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """List users. system_admin sees all; dept_admin sees own department only."""
    users = auth.list_users_as(actor)
    return [_user_view(u) for u in users]


@router.post("/users", response_model=AuthUserView)
def create_user(
    body: CreateUserRequest,
    actor: AuthUser = Depends(require_any_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Create a user. Role/department scoping is enforced by AuthService."""
    user = auth.create_user_as(
        actor,
        body.username,
        body.password,
        role=body.role,
        department_id=body.department_id,
    )
    return _user_view(user)


@router.put("/users/{user_id}/active", response_model=OkResponse)
def set_user_active(
    user_id: int,
    body: SetUserActiveRequest,
    actor: AuthUser = Depends(require_any_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Enable or disable a user account. Cannot target self."""
    auth.set_user_active_as(actor, user_id, body.is_active)
    return OkResponse(ok=True, message="user active state updated")


@router.put("/users/{user_id}/password", response_model=OkResponse)
def reset_user_password(
    user_id: int,
    body: ResetPasswordRequest,
    actor: AuthUser = Depends(require_any_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Reset a user's password. Cannot target self."""
    auth.reset_user_password_as(actor, user_id, body.new_password)
    return OkResponse(ok=True, message="password reset")