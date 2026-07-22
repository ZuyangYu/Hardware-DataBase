from __future__ import annotations

from collections import defaultdict
import re
from typing import Any


_EMPTY_VALUES = {"", "nan", "none", "null", "n/a", "header"}


def response_scope(question: str) -> dict[str, str]:
    return {
        "requested_detail": "key" if any(word in question for word in ("关键", "主要")) else "complete",
        "response_shape": "enumeration"
        if any(word in question for word in ("哪些", "每个", "列出"))
        else "direct",
    }


def answer_contract(
    question: str,
    claim_coverage: list[dict[str, Any]],
    retrieval_ledger: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> str:
    scope = response_scope(question)
    missing_count = sum(item.get("status") not in {"covered", "supported"} for item in retrieval_ledger)
    lines = [
        f"回答范围：{scope['requested_detail']} / {scope['response_shape']}。",
        "先给直接结论，再给支撑结论所必需的最少证据。",
        "只陈述有直接支持证据的事实，不得根据器件名或网络名相似性推断关系。",
        "没有直接证据时明确说明缺口，不要用邻近事实补全。",
        "不要输出检索诊断、质量分数或内部账本。",
    ]
    if scope["requested_detail"] == "key":
        lines.append("只列出问题要求的关键项，不要扩展为全部引脚、全部器件或全部网络。")
    if missing_count:
        lines.append(f"当前有 {missing_count} 个子问题未被完全覆盖，必须在对应结论后简洁标注缺口。")
    if conflicts:
        lines.append("存在可比较的来源冲突；分别列出字段、取值和来源，不得合并为确定结论。")
    return "\n".join(lines)


def reportable_conflicts(evidence: list[dict[str, Any]], question: str = "") -> list[dict[str, Any]]:
    values_by_field: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    controller_models: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for item in evidence:
        source_name = str(item.get("source_name") or "").strip()
        content = str(item.get("content") or "")
        model_match = re.search(r"\bTC3\d{2}", content, flags=re.IGNORECASE)
        content_kind = str(item.get("content_kind") or "")
        if source_name and model_match and content_kind in {"circuit_design", "document_text"}:
            controller_models[content_kind][model_match.group(0).casefold()].add(source_name)

        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        entity_id = str(metadata.get("entity_id") or item.get("entity_id") or "").strip()
        field = str(metadata.get("field") or item.get("field") or "").strip()
        value = str(metadata.get("value") or item.get("value") or "").strip()
        normalized_value = value.casefold()
        if not entity_id or not field or not source_name or normalized_value in _EMPTY_VALUES:
            continue
        normalized_field = f"{entity_id.casefold()}.{field.casefold()}"
        values_by_field[normalized_field][normalized_value].add(source_name)


    circuit_models = controller_models.get("circuit_design") or {}
    document_models = controller_models.get("document_text") or {}
    asks_for_controller_model = any(term in question.casefold() for term in ("mcu", "主控", "型号", "model"))
    if asks_for_controller_model and circuit_models and document_models:
        for model, sources in circuit_models.items():
            values_by_field["main_mcu.model"][model].update(sources)
        for model, sources in document_models.items():
            values_by_field["main_mcu.model"][model].update(sources)

    conflicts = []
    for field, values in sorted(values_by_field.items()):
        sources = {source for source_names in values.values() for source in source_names}
        if len(values) < 2 or len(sources) < 2:
            continue
        conflicts.append(
            {
                "field": field,
                "values": [
                    {"value": value, "sources": sorted(source_names)}
                    for value, source_names in sorted(values.items())
                ],
                "reason": "不同来源对同一实体字段给出不同取值",
            }
        )
    return conflicts
