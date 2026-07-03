from __future__ import annotations

import re


_HARDWARE_TERMS = (
    "bom",
    "emi",
    "emc",
    "pcb",
    "mpn",
    "sku",
    "layout",
    "schematic",
    "datasheet",
    "design",
    "test",
    "report",
    "原理图",
    "设计",
    "布局",
    "测试",
    "报告",
    "物料",
    "料号",
    "替代",
    "数量",
    "用量",
    "供应商",
    "规格",
    "参数",
    "电压",
    "电流",
    "功耗",
    "温度",
    "封装",
    "电阻",
    "电容",
    "电感",
)


def tokenize_hardware_query(
    text: str,
    *,
    max_tokens: int = 12,
    include_cjk_ngrams: bool = True,
) -> list[str]:
    """Return recall-oriented tokens for mixed Chinese/hardware queries."""
    value = str(text or "")
    lowered = value.casefold()
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        token = token.strip().casefold()
        if len(token) < 2 or token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for match in re.findall(r"[a-z][a-z0-9_.+-]{1,}|[0-9][a-z0-9_.+-]{1,}", lowered):
        add(match)

    for match in re.findall(
        r"\d+(?:\.\d+)?\s?(?:v|a|ma|ua|w|mw|uf|nf|pf|ohm|kohm|mhz|khz|ghz|mm|mil|%)",
        lowered,
    ):
        add(match.replace(" ", ""))

    for term in _HARDWARE_TERMS:
        if term.casefold() in lowered:
            add(term)

    cjk_blocks = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    if include_cjk_ngrams or not tokens:
        for block in cjk_blocks:
            if len(block) <= 4:
                add(block)
                continue
            for size in (2, 3):
                for index in range(0, len(block) - size + 1):
                    add(block[index:index + size])

    return tokens[:max(1, int(max_tokens))]
