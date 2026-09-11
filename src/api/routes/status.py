from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import require_dashboard_access
from src.core.auth import ROLE_SYSTEM_ADMIN, AuthUser
from src.core.conversation import ConversationService
from src.core.llm_governor import get_llm_governor
from src.core.model_gateway import get_usage_ledger
from src.observability.health import check_dependencies, check_ready


router = APIRouter(tags=["status"])


@router.get("/system/status")
def system_status(
    viewer: AuthUser = Depends(require_dashboard_access),
):
    """Return operational dependencies and durable task counters for admins.

    员工只看本部门的任务计数(与 /task-metrics 一致); 系统管理员看全局。
    """
    department_id = None if viewer.role == ROLE_SYSTEM_ADMIN else viewer.department_id
    return {
        "ready": check_ready(),
        "dependencies": check_dependencies(),
        "tasks": ConversationService().task_metrics_summary(department_id=department_id, hours=24),
        "llm": {
            **get_llm_governor().snapshot(),
            "usage": get_usage_ledger().snapshot(),
        },
    }
