"""Turn-scoped orchestration budget for planning, tools and recovery.

Evidence byte accounting stays in :class:`EvidenceBudgetCoordinator`; this
module only owns the counters that span multiple agent/tool loops (model
calls, tool calls, recovery steps and an overall deadline).  Keeping the
profile read-only in ``TaskPlan`` prevents the plan contract from becoming a
second mutable accounting source.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.services import task_settings


BudgetKind = Literal["model_call", "tool_call", "recovery_step"]


class TurnBudgetProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_model_calls: int = Field(default=8, ge=1)
    max_tool_calls: int = Field(default=24, ge=1)
    max_evidence_bytes: int = Field(default=22000, ge=0)
    max_comparison_bytes: int = Field(default=12000, ge=0)
    max_inline_items: int = Field(default=24, ge=0)
    max_recovery_steps: int = Field(default=3, ge=0)
    deadline_seconds: float = Field(default=120.0, gt=0)

    @classmethod
    def from_settings(cls) -> "TurnBudgetProfile":
        import src.settings as settings

        global_bytes = max(
            0, int(getattr(settings, "EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES", 32000) or 0)
        )
        system_reserve = max(
            0, int(getattr(settings, "EVIDENCE_SYSTEM_PROMPT_RESERVE_BYTES", 4000) or 0)
        )
        answer_reserve = max(
            0, int(getattr(settings, "EVIDENCE_ANSWER_RESERVE_BYTES", 6000) or 0)
        )
        evidence_bytes = max(0, global_bytes - system_reserve - answer_reserve)
        comparison_bytes = min(
            evidence_bytes,
            max(
                0,
                int(getattr(settings, "DOCUMENT_COMPARISON_MAX_MODEL_BYTES", 12000) or 0),
            ),
        )
        item_limit = max(
            0,
            int(
                getattr(
                    settings,
                    "MULTI_KB_EVIDENCE_MAX_ITEMS",
                    24,
                )
                or 0
            ),
        )
        return cls(
            max_model_calls=task_settings.TASK_MAX_MODEL_CALLS,
            max_tool_calls=task_settings.TASK_MAX_TOOL_CALLS,
            max_evidence_bytes=evidence_bytes,
            max_comparison_bytes=comparison_bytes,
            max_inline_items=item_limit,
            max_recovery_steps=task_settings.TASK_MAX_RECOVERY_STEPS,
            deadline_seconds=task_settings.TASK_TURN_DEADLINE_SECONDS,
        )


class TurnBudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_calls: int = 0
    tool_calls: int = 0
    evidence_bytes: int = 0
    comparison_bytes: int = 0
    recovery_steps: int = 0
    elapsed_seconds: float = 0.0


class TurnBudget:
    """Thread-safe live counters for one user turn."""

    def __init__(self, profile: TurnBudgetProfile | None = None):
        self.profile = profile or TurnBudgetProfile.from_settings()
        self.usage = TurnBudgetUsage()
        self._started = time.monotonic()
        self._lock = threading.RLock()

    def _elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._started)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self.usage.elapsed_seconds = round(self._elapsed(), 3)
            return {
                "profile": self.profile.model_dump(),
                "usage": self.usage.model_dump(),
                "remaining_model_calls": max(
                    0, self.profile.max_model_calls - self.usage.model_calls
                ),
                "remaining_tool_calls": max(
                    0, self.profile.max_tool_calls - self.usage.tool_calls
                ),
                "remaining_recovery_steps": max(
                    0, self.profile.max_recovery_steps - self.usage.recovery_steps
                ),
                "deadline_exceeded": self._elapsed() >= self.profile.deadline_seconds,
            }

    def remaining(self, kind: BudgetKind) -> int:
        with self._lock:
            if kind == "model_call":
                return max(0, self.profile.max_model_calls - self.usage.model_calls)
            if kind == "tool_call":
                return max(0, self.profile.max_tool_calls - self.usage.tool_calls)
            return max(0, self.profile.max_recovery_steps - self.usage.recovery_steps)

    def can_consume(self, kind: BudgetKind, amount: int = 1) -> bool:
        amount = max(0, int(amount or 0))
        if amount == 0:
            return True
        with self._lock:
            if self._elapsed() >= self.profile.deadline_seconds:
                return False
            return self.remaining(kind) >= amount

    def consume(self, kind: BudgetKind, amount: int = 1) -> bool:
        """Consume a counter atomically; return False when the limit is hit."""

        amount = max(0, int(amount or 0))
        if amount == 0:
            return True
        with self._lock:
            if self._elapsed() >= self.profile.deadline_seconds:
                return False
            if kind == "model_call":
                if self.usage.model_calls + amount > self.profile.max_model_calls:
                    return False
                self.usage.model_calls += amount
                return True
            if kind == "tool_call":
                if self.usage.tool_calls + amount > self.profile.max_tool_calls:
                    return False
                self.usage.tool_calls += amount
                return True
            if self.usage.recovery_steps + amount > self.profile.max_recovery_steps:
                return False
            self.usage.recovery_steps += amount
            return True

    def note_evidence(self, *, evidence_bytes: int, comparison_bytes: int) -> None:
        """Synchronize authoritative byte usage from the evidence coordinator."""

        with self._lock:
            self.usage.evidence_bytes = max(0, int(evidence_bytes or 0))
            self.usage.comparison_bytes = max(0, int(comparison_bytes or 0))

    def can_admit_evidence(self, *, comparison: bool = False, cost_bytes: int = 0) -> bool:
        cost = max(0, int(cost_bytes or 0))
        with self._lock:
            if self.usage.evidence_bytes + cost > self.profile.max_evidence_bytes:
                return False
            if comparison and self.usage.comparison_bytes + cost > self.profile.max_comparison_bytes:
                return False
            return True

    def can_start_recovery(self, *, estimated_tool_calls: int = 1) -> bool:
        return self.can_consume("recovery_step", 1) and self.can_consume(
            "tool_call", max(0, int(estimated_tool_calls or 0))
        )


__all__ = [
    "BudgetKind",
    "TurnBudget",
    "TurnBudgetProfile",
    "TurnBudgetUsage",
]
