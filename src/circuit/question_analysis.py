"""Deterministic normalization for broad circuit questions.

The result selects query capabilities only; it never asserts an electrical
fact.  This makes natural-language recall broader without allowing question
rewrites to manufacture circuit evidence.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CircuitQuestionPlan:
    operations: tuple[str, ...] = ()
    requires_datasheet: bool = False


_BIAS_TERMS = ("上拉", "下拉", "pull-up", "pullup", "pull-down", "pulldown", "默认电平", "偏置")
_CONNECTION_TERMS = ("连接", "连到", "接到", "引脚", "网络", "网表", "路径", "connection", "pin", "net")
_POWER_TERMS = ("电源输出", "供电路径", "电源路径", "电源树", "power output", "power path", "power tree")
_PROTECTION_TERMS = ("保护", "短电源", "短地", "短路", "过流", "过压", "反接", "esd", "tvs", "ocp", "scp")
_DATASHEET_TERMS = (
    "短电源", "短地", "短路", "过流", "过压", "反接", "保护能力", "规范", "额定", "datasheet", "ocp", "scp", "thermal",
)


def analyze_question(question: str) -> CircuitQuestionPlan:
    """Return bounded EDF operations implied by a natural-language question."""
    text = str(question or "").strip().casefold()
    if not text:
        return CircuitQuestionPlan()

    operations: list[str] = []
    if any(term in text for term in _BIAS_TERMS):
        operations.append("bias")
    if any(term in text for term in _POWER_TERMS):
        operations.append("power_path")
    if any(term in text for term in _PROTECTION_TERMS):
        operations.append("protection")
    if not operations and any(term in text for term in _CONNECTION_TERMS):
        operations.append("connection")

    return CircuitQuestionPlan(
        operations=tuple(operations),
        requires_datasheet=bool(operations and any(term in text for term in _DATASHEET_TERMS)),
    )
