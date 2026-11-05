"""Task-level tools: the agent maintains its own plan (no fixed workflow)."""

from __future__ import annotations

import json
from typing import Any

_ALLOWED_STATUS = {"pending", "running", "done", "blocked"}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_task_tools(task_id: str, store: Any, rt: Any = None) -> list[Any]:
    def task_plan_update(plan_json: str) -> str:
        """更新当前任务的执行计划（由你自己规划，不是固定流程）。

        plan_json 是 JSON 数组，每项形如
        {"title": "定位候选器件", "status": "running"}；
        status 只能是 pending / running / done / blocked。
        """
        if rt is not None:
            try:
                rt.check_cancel()
            except Exception:
                raise
        try:
            data = json.loads(plan_json or "[]")
            if isinstance(data, dict):
                data = data.get("steps") or data.get("plan") or []
            if not isinstance(data, list):
                raise ValueError("plan_json must be a JSON array")
            plan: list[dict[str, Any]] = []
            for index, item in enumerate(data[:20]):
                if isinstance(item, str):
                    item = {"title": item}
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                status = str(item.get("status") or "pending").lower()
                if status not in _ALLOWED_STATUS:
                    status = "pending"
                plan.append(
                    {
                        "id": str(item.get("id") or f"step-{index + 1}"),
                        "title": title[:200],
                        "status": status,
                    }
                )
            if not plan:
                raise ValueError("plan is empty")
            store.set_plan(task_id, plan)
            return _json({"updated": True, "plan": plan})
        except Exception as exc:
            return _json({"error": str(exc)})

    task_plan_update.__name__ = "task_plan_update"
    return [task_plan_update]


__all__ = ["build_task_tools"]
