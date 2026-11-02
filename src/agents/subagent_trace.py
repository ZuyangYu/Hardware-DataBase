"""Subagent lifecycle telemetry for the agent stream loop."""

from __future__ import annotations

import time
from typing import Any, Callable


def record_subagent_start(
    *,
    args: dict[str, Any],
    call_id: str,
    fingerprint: str,
    seen_tool_calls: set[str],
    subagents: list[dict[str, Any]],
    subagent_by_call: dict[str, dict[str, Any]],
    emit_event: Callable[[dict[str, Any]], None],
) -> None:
    description = str(
        args.get("description") or args.get("prompt") or args.get("goal") or ""
    ).strip()[:300]
    if not description:
        return
    seen_tool_calls.add(fingerprint)
    subagent = {
        "name": str(args.get("subagent_type") or "research"),
        "description": description,
        "status": "running",
        "tool_call_id": call_id,
        "_started": time.monotonic(),
    }
    subagents.append(subagent)
    if call_id:
        subagent_by_call[call_id] = subagent
    emit_event(
        {
            "type": "subagent_started",
            "payload": {
                key: value
                for key, value in subagent.items()
                if not key.startswith("_")
            },
        }
    )


def record_subagent_end(
    *,
    subagent: dict[str, Any],
    result_text: str,
    failed: bool,
    emit_event: Callable[[dict[str, Any]], None],
) -> None:
    subagent["status"] = "failed" if failed else "completed"
    subagent["result_chars"] = len(result_text)
    started = float(subagent.pop("_started", time.monotonic()))
    subagent["duration_ms"] = max(0, int((time.monotonic() - started) * 1000))
    emit_event(
        {
            "type": "subagent_completed",
            "payload": {
                key: value
                for key, value in subagent.items()
                if not key.startswith("_")
            },
        }
    )


__all__ = ["record_subagent_end", "record_subagent_start"]
