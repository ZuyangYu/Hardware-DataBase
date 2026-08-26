"""Deterministic normalization for broad circuit questions.

The result selects query capabilities only; it never asserts an electrical
fact.  This makes natural-language recall broader without allowing question
rewrites to manufacture circuit evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class CircuitQuestionPlan:
    operations: tuple[str, ...] = ()
    requires_datasheet: bool = False
    # Retrieval-intent vocabulary hit (SoC/MCU/主控…). Describes the query,
    # never a device fact; concrete assertions come only from EDF/catalog
    # evidence during identity resolution.
    role_term: str | None = None


_BIAS_TERMS = ("上拉", "下拉", "pull-up", "pullup", "pull up", "pull-down", "pulldown", "pull down", "默认电平", "偏置")
_CONNECTION_TERMS = ("连接", "连到", "接到", "引脚", "网络", "网表", "路径", "connection", "pin", "net")
_POWER_TERMS = ("电源输出", "供电路径", "电源路径", "电源树", "power output", "power path", "power tree")
_POWER_SWITCH_TERMS = ("负载开关", "电源开关", "load switch", "power switch", "tps22918")
_POWER_ROLE_TERMS = ("输入", "输出", "使能", "vin", "vout", "enable", " on")
_PROTECTION_TERMS = ("保护", "短电源", "短地", "短路", "过流", "过压", "反接", "esd", "tvs", "ocp", "scp")
_DATASHEET_TERMS = (
    "短电源", "短地", "短路", "过流", "过压", "反接", "保护能力", "规范", "额定", "datasheet", "ocp", "scp", "thermal",
)
_PLACEMENT_TERMS = ("靠近", "布局", "摆放", "placement", "layout")
_VALUE_TERMS = ("阻值", "数值", "参数", "resistance", "频率")
_CLOCK_TERMS = ("晶振", "晶体", "振荡器", "频率", "时钟", "crystal", "oscillator", "clock", "frequency")
_ENABLE_TERMS = ("使能", "禁止", "唤醒", "inhibit", "wakeup", "wake-up", "enable", "en_sync", "ecu_en")
_I2C_TERMS = ("i2c", "i²c", "scl", "sda")
_COMPONENT_SELECTION_TERMS = ("芯片型号", "器件型号", "型号", "选型", "可使用", "part number", "component selection")
_STRUCTURE_OVERVIEW_TERMS = (
    "结构信息", "结构概览", "电路结构", "原理图结构", "设计概况", "概况", "概览", "overview",
)
_MODULE_LIST_TERMS = ("所有模块", "模块列表", "列出模块", "哪些模块", "模块划分", "module list")
_VISUAL_STRUCTURE_TERMS = ("页面数", "原理图页面", "标题栏", "坐标数据", "视觉布局", "布局图", "schematic page")
# Controlled retrieval-intent words. Longer phrases first where overlap exists.
_ROLE_TERM_PATTERNS = (
    "system on chip",
    "microcontroller",
    "ethernet phy",
    "以太网phy",
    "以太网 phy",
    "电源管理芯片",
    "soc",
    "mcu",
    "pmic",
    "主控芯片",
    "主控",
    "处理器",
    "单片机",
    "存储器",
    "收发器",
    "transceiver",
    "连接器",
    "connector",
)
_REFDES_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d+(?![A-Za-z0-9])")
_NET_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]*(?:_[A-Z0-9.]+)+(?![A-Za-z0-9])")


def _has_term(text: str, terms: tuple[str, ...]) -> bool:
    """Match CJK phrases directly and Latin terms as complete identifiers."""
    for term in terms:
        if _match_term(text, term):
            return True
    return False


def _match_term(text: str, term: str) -> bool:
    if any("\u4e00" <= char <= "\u9fff" for char in term):
        return term in text
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def extract_role_term(question: str) -> str | None:
    """Return the first controlled role word present in the question."""
    text = str(question or "").strip().casefold()
    if not text:
        return None
    for term in _ROLE_TERM_PATTERNS:
        if _match_term(text, term):
            return term
    return None


def has_explicit_refdes(question: str) -> bool:
    return bool(_REFDES_RE.search(str(question or "")))


def analyze_question(question: str) -> CircuitQuestionPlan:
    """Return bounded EDF operations implied by a natural-language question."""
    text = str(question or "").strip().casefold()
    if not text:
        return CircuitQuestionPlan()

    operations: list[str] = []
    role_term = extract_role_term(text)
    if role_term:
        operations.append("entity_role")
    if _has_term(text, _BIAS_TERMS):
        operations.append("bias")
    if _has_term(text, _I2C_TERMS):
        operations.append("i2c")
    if _has_term(text, _ENABLE_TERMS):
        operations.append("enable")
    if _has_term(text, _CLOCK_TERMS):
        operations.append("clock")
    if _has_term(text, _COMPONENT_SELECTION_TERMS):
        operations.append("component_selection")
    if _has_term(text, _PLACEMENT_TERMS):
        operations.append("placement")
    if _has_term(text, _VALUE_TERMS):
        operations.append("value")
    if _has_term(text, _STRUCTURE_OVERVIEW_TERMS):
        operations.append("structure_overview")
    if _has_term(text, _MODULE_LIST_TERMS):
        operations.append("module_list")
    if _has_term(text, _VISUAL_STRUCTURE_TERMS):
        operations.append("visual_structure")
    if _has_term(text, _POWER_TERMS):
        operations.append("power_path")
    if _has_term(text, ("输入电压", "输出电压", "输出电流", "vin", "vout")) and "power_path" not in operations:
        operations.append("power_path")
    if _has_term(text, _POWER_SWITCH_TERMS) and _has_term(text, _POWER_ROLE_TERMS):
        operations.append("power_switch")
    if _has_term(text, _PROTECTION_TERMS):
        operations.append("protection")
    if (
        _has_term(text, _CONNECTION_TERMS)
        or "i2c" in operations
        or "enable" in operations
        or _NET_RE.search(text.upper())
    ):
        operations.append("connection")
    elif _REFDES_RE.search(text):
        operations.append("entity_lookup")

    return CircuitQuestionPlan(
        operations=tuple(dict.fromkeys(operations)),
        requires_datasheet=bool(operations and _has_term(text, _DATASHEET_TERMS)),
        role_term=role_term,
    )
