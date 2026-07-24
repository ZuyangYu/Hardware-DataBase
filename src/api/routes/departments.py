"""Department management endpoints (system_admin only for CUD)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.core.auth import AuthService, AuthUser

from src.api.deps import current_user, get_auth_service, require_system_admin
from src.api.schemas import CreateDepartmentRequest, DepartmentView, OkResponse

router = APIRouter(tags=["departments"])


@router.get("/departments", response_model=list[DepartmentView])
def list_departments(
    _user: AuthUser = Depends(current_user),  # any authenticated user may list
    auth: AuthService = Depends(get_auth_service),
):
    """List all departments. Available to any authenticated user."""
    return [DepartmentView(id=d.id, name=d.name) for d in auth.list_departments()]


@router.post("/departments", response_model=DepartmentView)
def create_department(
    body: CreateDepartmentRequest,
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Create a department. system_admin only."""
    d = auth.create_department_as(actor, body.name)
    return DepartmentView(id=d.id, name=d.name)


@router.delete("/departments/{department_id}", response_model=OkResponse)
def delete_department(
    department_id: int,
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """Delete a department. Refuses if it still has users / KBs or is system."""
    auth.delete_department_as(actor, department_id)
    return OkResponse(ok=True, message="department deleted")