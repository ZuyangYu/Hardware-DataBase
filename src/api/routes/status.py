from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import require_any_admin
from src.core.auth import AuthUser
from src.core.conversation import ConversationService
from src.observability.health import check_dependencies, check_ready


router = APIRouter(tags=["status"])


@router.get("/system/status")
def system_status(
    _viewer: AuthUser = Depends(require_any_admin),
):
    """Return operational dependencies and durable task counters for admins."""

    return {
        "ready": check_ready(),
        "dependencies": check_dependencies(),
        "tasks": ConversationService().task_metrics_summary(hours=24),
    }
