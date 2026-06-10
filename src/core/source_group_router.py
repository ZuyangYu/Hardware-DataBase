from dataclasses import dataclass

from src.ingestion.source_groups import DOCS_GROUP, MATERIAL_GROUP, UNKNOWN_GROUP


@dataclass(frozen=True)
class SourceGroupRoute:
    weights: dict[str, float]
    reason: str


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
    "manual",
    "errata",
    "register",
    "pin",
    "voltage",
    "current",
    "timing",
    "layout",
    "spec",
    "手册",
    "规格",
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


def route_source_groups(query: str) -> SourceGroupRoute:
    text = query.lower()
    material_hits = sum(1 for keyword in _MATERIAL_QUERY_KEYWORDS if keyword.lower() in text)
    docs_hits = sum(1 for keyword in _DOC_QUERY_KEYWORDS if keyword.lower() in text)

    if material_hits > docs_hits:
        return SourceGroupRoute(
            weights={MATERIAL_GROUP: 1.25, DOCS_GROUP: 0.8, UNKNOWN_GROUP: 0.65},
            reason="material-oriented query",
        )

    if docs_hits > material_hits:
        return SourceGroupRoute(
            weights={DOCS_GROUP: 1.2, MATERIAL_GROUP: 0.85, UNKNOWN_GROUP: 0.65},
            reason="document-oriented query",
        )

    return SourceGroupRoute(
        weights={DOCS_GROUP: 1.0, MATERIAL_GROUP: 1.0, UNKNOWN_GROUP: 0.75},
        reason="balanced query",
    )
