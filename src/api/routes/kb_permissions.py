"""Knowledge-base permission endpoints.

- ``GET /kbs/{kb_name}/permissions`` — list permissions on a KB
- ``POST /kbs/{kb_name}/permissions`` — grant a permission (dept_admin)
- ``PUT /kbs/{kb_name}/assign`` — reassign KB to a different dept/owner (system_admin)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.core.auth import AuthService, AuthUser
from src.services.kb_scope import kb_scope_from_context

from src.api.context import build_context_for_user
from src.api.deps import (
    current_user,
    get_auth_service,
    require_dept_admin,
    require_system_admin,
)
from src.api.schemas import (
    AssignKbRequest,
    GrantKbPermissionRequest,
    KbPermissionView,
    OkResponse,
)

router = APIRouter(tags=["kb-permissions"])


@router.get("/kbs/{kb_name}/permissions", response_model=list[KbPermissionView])
def list_kb_permissions(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    """List all users with permissions on a KB (department-scoped by caller).

    system_admin sees the grant list as governance data. Other roles need
    read on the KB so a plain user can't enumerate another KB's grants;
    dept_admins (who manage grants in the Streamlit KB panel) have read
    implicitly via their department scope.
    """
    ctx = build_context_for_user(user, kb_name, auth=auth)
    if not ctx.is_system_admin() and not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    scope = kb_scope_from_context(kb_name, ctx)
    perms = auth.list_knowledge_base_permissions(
        scope.kb_name,
        department_id=scope.department_id or None,
        kb_id=scope.kb_id,
    )
    return [
        KbPermissionView(
            username=p.username,
            role=p.role,
            permission=p.permission,
            department_name=p.department_name,
        )
        for p in perms
    ]


@router.post("/kbs/{kb_name}/permissions", response_model=OkResponse)
def grant_kb_permission(
    kb_name: str,
    body: GrantKbPermissionRequest,
    actor: AuthUser = Depends(require_dept_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Grant a permission (read/write/admin) to a user on a KB.

    Dept-admin only; scoping to the caller's department is enforced inside
    :meth:`AuthService.grant_kb_permission_as`.
    """
    auth.grant_kb_permission_as(
        actor,
        kb_name,
        body.user_id,
        permission=body.permission,
    )
    return OkResponse(ok=True, message="permission granted")


@router.put("/kbs/{kb_name}/assign", response_model=OkResponse)
def assign_kb(
    kb_name: str,
    body: AssignKbRequest,
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Reassign a KB to a different department / owner. system_admin only."""
    auth.assign_knowledge_base_as(
        actor,
        kb_name,
        body.department_id,
        owner_user_id=body.owner_user_id,
    )
    return OkResponse(ok=True, message="knowledge base reassigned")


@router.delete("/kbs/{kb_name}/permissions/{user_id}", response_model=OkResponse)
def revoke_kb_permission(
    kb_name: str,
    user_id: int,
    actor: AuthUser = Depends(require_dept_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Revoke a user's permission on a KB. Dept-admin only; scoping is
    enforced inside :meth:`AuthService.revoke_kb_permission_as`."""
    auth.revoke_kb_permission_as(actor, kb_name, user_id)
    return OkResponse(ok=True, message="permission revoked")
