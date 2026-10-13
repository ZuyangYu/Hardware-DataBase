from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import require_system_admin
from src.core.auth import AuthUser
from src.core.conversation import ConversationService
from src.core.llm_governor import get_llm_governor
from src.core.model_gateway import get_persisted_usage, get_usage_ledger
from src.costs.sink import sink_snapshots
from src.costs.store import CostStore
from src.observability.health import check_dependencies, check_ready
import os


router = APIRouter(tags=["status"])


@router.get("/system/status")
def system_status(
    viewer: AuthUser = Depends(require_system_admin),
):
    """Return operational dependencies and durable task counters.

    运维明细(依赖错误原文 / LLM 队列与用量 / 任务统计)只给系统管理员;
    员工没有这个入口。
    """
    slo = {"enabled": bool(os.getenv("HDB_CHAT_SLO_POLICY")), "workloads": ["general_chat", "kb_chat"]}
    if slo["enabled"]:
        from src.observability.slo import ChatSloStore

        slo["windows"] = ChatSloStore().windows()
    return {
        "business_slo": slo,
        "cost_accounting": {"ledger": CostStore().summary(viewer), "local_sinks": sink_snapshots()},
        "ready": check_ready(),
        "dependencies": check_dependencies(),
        "tasks": ConversationService().task_metrics_summary(department_id=None, hours=24),
        "llm": {
            **get_llm_governor().snapshot(),
            "usage": get_usage_ledger().snapshot(),
            "persisted_usage": get_persisted_usage(),
        },
    }
