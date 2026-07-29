from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import current_user
from src.core.auth import ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN, AuthUser
from src.core.conversation import ConversationService

router = APIRouter(tags=["metrics"])


@router.get("/task-metrics")
def task_metrics(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    user: AuthUser = Depends(current_user),
):
    # Operational counters expose no prompt, answer, filename, or evidence.
    # Department admins only receive their own department's aggregate.
    if user.role not in {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN}:
        raise HTTPException(status_code=403, detail="admin role required")
    department_id = None if user.role == ROLE_SYSTEM_ADMIN else user.department_id
    return ConversationService().task_metrics_summary(department_id=department_id, hours=hours)
