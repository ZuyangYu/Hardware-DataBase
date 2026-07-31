"""Department management endpoints (system_admin only for CUD)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.core.auth import ROLE_SYSTEM_ADMIN, AuthService, AuthUser

from src.api.deps import get_auth_service, require_any_admin, require_system_admin
from src.api.schemas import CreateDepartmentRequest, DepartmentView, OkResponse

router = APIRouter(tags=["departments"])


@router.get("/departments", response_model=list[DepartmentView])
def list_departments(
    actor: AuthUser = Depends(require_any_admin),
    auth: AuthService = Depends(get_auth_service),
):
    """List departments. system_admin sees all; dept_admin sees only their own
    department. Mirrors the Streamlit department-management tab visibility."""
    deps = auth.list_departments()
    if actor.role == ROLE_SYSTEM_ADMIN:
        return [DepartmentView(id=d.id, name=d.name) for d in deps]
    return [DepartmentView(id=d.id, name=d.name) for d in deps if d.id == actor.department_id]


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
    """Delete a department. Refuses if it still has users or knowledge bases."""
    auth.delete_department_as(actor, department_id)
    return OkResponse(ok=True, message="department deleted")
