"""Knowledge-base mounting endpoints (system_admin only).

Mounting a KB to a department / owner is governance: it decides which
department's employees see the KB. There are no per-user grants in the
two-role model — every employee of the owning department has implicit
admin access.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.core.auth import AuthService, AuthUser

from src.api.deps import get_auth_service, require_system_admin
from src.api.schemas import AssignKbRequest, OkResponse

router = APIRouter(tags=["kb-assign"])


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
        source_kb_id=body.source_kb_id,
    )
    return OkResponse(ok=True, message="knowledge base reassigned")
