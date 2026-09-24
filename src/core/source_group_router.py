from dataclasses import dataclass

from src.ingestion.source_groups import (
    DESIGN_GROUP,
    DOCS_GROUP,
    MATERIAL_GROUP,
    NETLIST_GROUP,
    PROJECT_GROUP,
    SCHEMATIC_GROUP,
    TEST_GROUP,
    UNKNOWN_GROUP,
)


@dataclass(frozen=True)
class SourceGroupRoute:
    weights: dict[str, float]
    reason: str
    source_groups: tuple[str, ...] = ()
    confidence: float = 0.0

    @property
    def should_filter(self) -> bool:
        return bool(self.source_groups) and self.confidence >= 0.7


_MATERIAL_QUERY_KEYWORDS = [
    "bom",
    "mpn",
    "part number",
    "manufacturer",
    "supplier",
    "vendor",
    "quantity",
    "替代",
    "替代料",
    "物料",
    "料号",
    "用量",
    "数量",
    "供应商",
    "厂商",
    "封装",
    "库存",
]

_DOC_QUERY_KEYWORDS = [
    "datasheet",
    "hsi",
    "manual",
    "errata",
    "register",
    "can fd",
    "transceiver",
    "pin",
    "voltage",
    "current",
    "timing",
    "layout",
    "spec",
    "接口文档",
    "手册",
    "文档",
    "规格",
    "收发器",
    "电压",
    "电流",
    "时序",
    "引脚",
    "寄存器",
    "版图",
    "布局",
    "布线",
    "参数",
]

_DESIGN_QUERY_KEYWORDS = [
    "schematic",
    "pcb",
    "netlist",
    "layout",
    "constraint",
    "simulation",
    "原理图",
    "网表",
    "约束",
    "仿真",
    "布局",
    "布线",
]

_NETLIST_QUERY_KEYWORDS = [
    "edf",
    "edif",
    "netlist",
    "refdes",
    "instance",
    "net",
    "pin",
    "module",
    "网表",
    "位号",
    "网络",
    "引脚",
    "模块",
]

_SCHEMATIC_QUERY_KEYWORDS = [
    "schematic",
    "sch",
    "pdf schematic",
    "page",
    "label",
    "cross reference",
    "原理图",
    "图纸",
    "页面",
    "标签",
    "映射",
]

_TEST_QUERY_KEYWORDS = [
    "test",
    "report",
    "validation",
    "verify",
    "emi",
    "emc",
    "pass",
    "fail",
    "测试",
    "报告",
    "验证",
    "超标",
    "通过",
    "失败",
]

_PROJECT_QUERY_KEYWORDS = [
    "meeting",
    "review",
    "schedule",
    "milestone",
    "task",
    "owner",
    "deadline",
    "会议",
    "评审",
    "计划",
    "进度",
    "任务",
    "负责人",
    "截止",
    "里程碑",
]

_GROUP_KEYWORDS = {
    DOCS_GROUP: _DOC_QUERY_KEYWORDS,
    MATERIAL_GROUP: _MATERIAL_QUERY_KEYWORDS,
    NETLIST_GROUP: _NETLIST_QUERY_KEYWORDS,
    SCHEMATIC_GROUP: _SCHEMATIC_QUERY_KEYWORDS,
    DESIGN_GROUP: _DESIGN_QUERY_KEYWORDS,
    TEST_GROUP: _TEST_QUERY_KEYWORDS,
    PROJECT_GROUP: _PROJECT_QUERY_KEYWORDS,
}

_INTERFACE_DOCUMENT_KEYWORDS = (
    "hsi",
    "hardware software interface",
    "interface document",
    "接口文档",
)


def route_source_groups(query: str) -> SourceGroupRoute:
    text = query.lower()
    if any(keyword in text for keyword in _INTERFACE_DOCUMENT_KEYWORDS):
        weights = {group: 0.65 for group in _GROUP_KEYWORDS}
        weights[DOCS_GROUP] = 1.25
        weights[DESIGN_GROUP] = 1.2
        weights[UNKNOWN_GROUP] = 0.5
        return SourceGroupRoute(
            weights=weights,
            reason="interface document route",
            source_groups=(DOCS_GROUP, DESIGN_GROUP),
            confidence=0.85,
        )
    hits = {
        group: sum(1 for keyword in keywords if keyword.lower() in text)
        for group, keywords in _GROUP_KEYWORDS.items()
    }
    best_group, best_hits = max(hits.items(), key=lambda item: item[1])
    sorted_hits = sorted(hits.values(), reverse=True)
    second_hits = sorted_hits[1] if len(sorted_hits) > 1 else 0

    if best_hits >= 2 and best_hits > second_hits:
        weights = {group: 0.65 for group in _GROUP_KEYWORDS}
        weights[best_group] = 1.3
        weights[UNKNOWN_GROUP] = 0.5
        return SourceGroupRoute(
            weights=weights,
            reason=f"{best_group} high-confidence route",
            source_groups=(best_group,),
            confidence=0.85,
        )

    if best_hits == 1 and best_hits > second_hits:
        weights = {group: 0.85 for group in _GROUP_KEYWORDS}
        weights[best_group] = 1.15
        weights[UNKNOWN_GROUP] = 0.65
        return SourceGroupRoute(
            weights=weights,
            reason=f"{best_group} low-confidence route",
            source_groups=(best_group,),
            confidence=0.55,
        )

    balanced_weights = {group: 1.0 for group in _GROUP_KEYWORDS}
    balanced_weights[UNKNOWN_GROUP] = 0.75
    return SourceGroupRoute(
        weights=balanced_weights,
        reason="balanced query",
        confidence=0.0,
    )
