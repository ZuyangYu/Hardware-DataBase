"""Account management endpoints.

Registration, activation and password resets are governance: only the system
administrator manages other accounts. Every signed-in user may change their own
password via ``PUT /users/me/password`` (current password required).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from src.core.auth import AuthService, AuthUser

from src.api.deps import current_user, get_auth_service, require_system_admin
from src.api.schemas import (
    AuthUserView,
    ChangeOwnPasswordRequest,
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
        tenant_id=u.tenant_id,
    )


@router.get("/users", response_model=list[AuthUserView])
def list_users(
    department_id: int | None = Query(default=None),
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """List all accounts; optional ``department_id`` filters employees by dept."""
    users = auth.list_users_as(actor)
    if department_id is not None:
        users = [u for u in users if u.department_id == department_id]
    return [_user_view(u) for u in users]


@router.post("/users", response_model=AuthUserView)
def create_user(
    body: CreateUserRequest,
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Register an employee (bound to a department) or another system admin."""
    user = auth.create_user_as(
        actor,
        body.username,
        body.password,
        role=body.role,
        department_id=body.department_id,
        tenant_id=body.tenant_id,
    )
    return _user_view(user)


@router.put("/users/{user_id}/active", response_model=OkResponse)
def set_user_active(
    user_id: int,
    body: SetUserActiveRequest,
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Enable or disable an account. Cannot target self."""
    auth.set_user_active_as(actor, user_id, body.is_active)
    return OkResponse(ok=True, message="user active state updated")


@router.put("/users/me/password", response_model=OkResponse)
def change_own_password(
    body: ChangeOwnPasswordRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    """Change the signed-in account's own password (requires the current password).

    All sessions of the account are revoked, so the client must sign in again.
    """
    auth.change_own_password(user, body.current_password, body.new_password)
    return OkResponse(ok=True, message="password changed")


@router.put("/users/{user_id}/password", response_model=OkResponse)
def reset_user_password(
    user_id: int,
    body: ResetPasswordRequest,
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Reset an account password. Cannot target self."""
    auth.reset_user_password_as(actor, user_id, body.new_password)
    return OkResponse(ok=True, message="password reset")
