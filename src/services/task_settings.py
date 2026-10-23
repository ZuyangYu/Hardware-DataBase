"""Configuration for the task-planning/completion-gate layer.

These flags are intentionally kept out of ``src/settings.py``: that module is
already at its architecture line-budget allowance, and task orchestration is
not a retrieval setting.  Read values through this module so a later config
surface can refresh them without importing the runner.
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


TASK_PLANNING_ENABLED = _env_bool("TASK_PLANNING_ENABLED", False)
TASK_COMPLETION_GATE_ENABLED = _env_bool("TASK_COMPLETION_GATE_ENABLED", False)
TASK_COMPARISON_SCOPE_ENABLED = _env_bool("TASK_COMPARISON_SCOPE_ENABLED", False)
TASK_COMPARISON_PREFLIGHT_ENABLED = _env_bool("TASK_COMPARISON_PREFLIGHT_ENABLED", False)
TASK_ANSWER_BUFFERING_ENABLED = _env_bool("TASK_ANSWER_BUFFERING_ENABLED", False)
TASK_PLAN_MODEL_ENABLED = _env_bool("TASK_PLAN_MODEL_ENABLED", False)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


TASK_MAX_RECOVERY_STEPS = max(0, _env_int("TASK_MAX_RECOVERY_STEPS", 3))
TASK_TURN_DEADLINE_SECONDS = max(1.0, _env_float("TASK_TURN_DEADLINE_SECONDS", 120.0))
TASK_MAX_MODEL_CALLS = max(1, _env_int("TASK_MAX_MODEL_CALLS", 8))
TASK_MAX_TOOL_CALLS = max(1, _env_int("TASK_MAX_TOOL_CALLS", 24))


def snapshot() -> dict[str, object]:
    return {
        "TASK_PLANNING_ENABLED": TASK_PLANNING_ENABLED,
        "TASK_COMPLETION_GATE_ENABLED": TASK_COMPLETION_GATE_ENABLED,
        "TASK_COMPARISON_SCOPE_ENABLED": TASK_COMPARISON_SCOPE_ENABLED,
        "TASK_COMPARISON_PREFLIGHT_ENABLED": TASK_COMPARISON_PREFLIGHT_ENABLED,
        "TASK_ANSWER_BUFFERING_ENABLED": TASK_ANSWER_BUFFERING_ENABLED,
        "TASK_PLAN_MODEL_ENABLED": TASK_PLAN_MODEL_ENABLED,
        "TASK_MAX_RECOVERY_STEPS": TASK_MAX_RECOVERY_STEPS,
        "TASK_TURN_DEADLINE_SECONDS": TASK_TURN_DEADLINE_SECONDS,
        "TASK_MAX_MODEL_CALLS": TASK_MAX_MODEL_CALLS,
        "TASK_MAX_TOOL_CALLS": TASK_MAX_TOOL_CALLS,
    }


__all__ = [
    "TASK_ANSWER_BUFFERING_ENABLED",
    "TASK_COMPARISON_PREFLIGHT_ENABLED",
    "TASK_COMPARISON_SCOPE_ENABLED",
    "TASK_COMPLETION_GATE_ENABLED",
    "TASK_MAX_MODEL_CALLS",
    "TASK_MAX_RECOVERY_STEPS",
    "TASK_MAX_TOOL_CALLS",
    "TASK_PLANNING_ENABLED",
    "TASK_PLAN_MODEL_ENABLED",
    "TASK_TURN_DEADLINE_SECONDS",
    "snapshot",
]
