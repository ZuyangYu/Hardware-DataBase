"""Same-turn gap-closure policy.

``GapClosureMiddleware`` owns the hook and the jump; this module owns only the
deterministic decision of whether a closure round is worth attempting.  Keeping
the policy separate from the graph mechanics makes it unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.agents.turn_meta import answer_gap_lines

_MAX_CLOSURE_ROUNDS = 1
_SLOW_TURN_SECONDS = 360.0


@dataclass(frozen=True)
class GapClosureDecision:
    gaps: list[str] = field(default_factory=list)
    actionable: bool = False
    should_close: bool = False
    reason: str = ""


def decide_gap_closure(
    *,
    draft: str,
    rt: Any,
    pre_candidates: set[str],
    elapsed_seconds: float,
    gap_rounds: int,
    is_general: bool,
    query_mode: str,
    has_checkpointer: bool,
) -> GapClosureDecision:
    """Decide whether one same-turn closure round should run."""

    if is_general or query_mode == "fast" or not has_checkpointer:
        return GapClosureDecision(reason="disabled")
    gaps = answer_gap_lines(draft)
    if not gaps:
        return GapClosureDecision(reason="no_gaps")
    new_candidates = bool(set(rt.candidate_evidence) - pre_candidates)
    retrieval_tools = {
        str(item.get("tool_name") or "") for item in rt.diagnostics
    }
    actionable = new_candidates or len(retrieval_tools) < 2
    if not actionable:
        return GapClosureDecision(gaps=gaps, reason="no_actionable_pointers")
    if gap_rounds >= _MAX_CLOSURE_ROUNDS:
        return GapClosureDecision(gaps=gaps, actionable=True, reason="already_closed")
    if elapsed_seconds > _SLOW_TURN_SECONDS:
        return GapClosureDecision(gaps=gaps, actionable=True, reason="slow_turn")
    return GapClosureDecision(
        gaps=gaps, actionable=True, should_close=True, reason="ok"
    )


__all__ = ["GapClosureDecision", "decide_gap_closure"]
