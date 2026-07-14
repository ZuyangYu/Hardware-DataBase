from __future__ import annotations

from src.circuit.query_context import CircuitIntent, CircuitScope, CircuitToolResponse, ClarificationOption, ResolvedEntity
from src.circuit.query_engine import CircuitQueryEngine
from src.core.logger import debug, log


_POWER_KEYWORDS = ["power", "supply", "buck", "regulator", "voltage", "vcc", "vdd", "电源", "供电", "降压", "稳压"]


class RecoveryManager:
    """Build bounded recovery responses for circuit query failures."""

    def __init__(self, engine: CircuitQueryEngine | None = None):
        self.engine = engine or CircuitQueryEngine()

    def unsupported_query(self, question: str, intent: CircuitIntent) -> CircuitToolResponse:
        debug(f"recovery: unsupported_query intent={intent.intent}")
        suggestions = [
            "查询电路中有哪些模块",
            "查询某个模块包含哪些元件",
            "查询 U3 这类元件的连接关系",
            "查询某个模块的供电网络",
        ]
        return CircuitToolResponse(
            answer="该问题暂未匹配到 EDF/PDF 原理图结构化查询能力。你可以尝试：" + "；".join(suggestions) + "。",
            answer_mode="unsupported",
            confidence=intent.confidence,
            data={"intent": intent.intent, "question": question},
            follow_up_suggestions=suggestions,
        )

    def ambiguous_entity(self, intent: CircuitIntent, candidates: list[ResolvedEntity], reason: str) -> CircuitToolResponse:
        debug(f"recovery: ambiguous_entity intent={intent.intent} candidates={len(candidates)}")
        options = [
            ClarificationOption(
                label=entity.display_name,
                value=entity.entity_id,
                option_type=entity.entity_type if entity.entity_type in {"module", "instance", "net"} else "module",
                metadata={"circuit_id": entity.circuit_id, "entity_type": entity.entity_type},
            )
            for entity in candidates
        ]
        lines = [reason or "找到多个可能的实体，请选择一个："]
        for idx, entity in enumerate(candidates, 1):
            suffix = f"（{entity.circuit_id}）" if entity.circuit_id else ""
            lines.append(f"{idx}. {entity.display_name}{suffix}")
        return CircuitToolResponse(
            answer="\n".join(lines),
            answer_mode="needs_clarification",
            data={"intent": intent.intent, "candidates": [entity.to_dict() for entity in candidates]},
            clarification_options=options,
            resolved_entities=candidates,
            confidence=0.0,
        )

    def entity_not_found(
        self,
        question: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        entity_text: str | None,
    ) -> CircuitToolResponse:
        debug(f"recovery: entity_not_found intent={intent.intent} entity={entity_text}")
        if intent.target_entity_type == "module" or intent.intent in {"module_detail", "power_distribution"}:
            candidates = self._recover_module_candidates(question, scope, entity_text)
            if candidates:
                return self.ambiguous_entity(intent, candidates, f"未找到明确模块 `{entity_text or question}`，但找到以下可能候选：")
        message = f"未找到 `{entity_text or question}` 对应的电路实体。"
        suggestions = ["确认实体名称是否与 EDF 中一致", "先查询模块列表", "指定 EDF/电路后重试"]
        return CircuitToolResponse(
            answer=message,
            answer_mode="partial_answer",
            data={"intent": intent.intent, "scope": scope.scope_type, "entity_text": entity_text},
            missing_info=[message],
            follow_up_suggestions=suggestions,
            confidence=0.35,
        )

    def data_missing(
        self,
        message: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        suggestions: list[str] | None = None,
    ) -> CircuitToolResponse:
        log(f"recovery: data_missing intent={intent.intent} scope={scope.scope_type}")
        return CircuitToolResponse(
            answer=message,
            answer_mode="partial_answer",
            data={"intent": intent.intent, "scope": scope.scope_type},
            missing_info=[message],
            follow_up_suggestions=suggestions or ["确认该 EDF 已完成解析", "尝试查询模块列表或电路概况"],
            confidence=0.4,
        )

    def source_missing(
        self,
        message: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        missing_source: str = "edf_netlist",
        suggestions: list[str] | None = None,
    ) -> CircuitToolResponse:
        """A required source (EDF netlist / PDF) is absent → degraded answer.

        Connection tracing, power-tree and instance queries need an EDF netlist;
        PDF location / screenshot queries need a parsed schematic. Say so
        instead of returning a bare "未匹配" (plan §3.7 ``source_missing``).
        """
        log(f"recovery: source_missing intent={intent.intent} source={missing_source}")
        if suggestions is None:
            if missing_source == "pdf_schematic":
                suggestions = ["上传对应 PDF 原理图后重新查询", "查询模块列表或电路概况"]
            else:
                suggestions = ["上传对应 EDF 网表后重新查询", "查询模块列表或电路概况"]
        return CircuitToolResponse(
            answer=message,
            answer_mode="partial_answer",
            data={"intent": intent.intent, "scope": scope.scope_type, "missing_source": missing_source},
            missing_info=[message],
            follow_up_suggestions=suggestions,
            confidence=0.3,
        )

    def _recover_module_candidates(self, question: str, scope: CircuitScope, entity_text: str | None) -> list[ResolvedEntity]:
        """Recover module candidates via keyword recall then vector semantic recall.

        Keyword hits rank above semantic hits; both are scoped to ``scope.circuit_ids``
        so a single-circuit recovery never surfaces neighbours from other circuits
        (plan §3.7 / §4.7).
        """
        query = entity_text or question
        keywords = None
        if any(term in f"{question} {entity_text or ''}" for term in ("电源", "供电", "降压", "稳压", "电压")):
            keywords = _POWER_KEYWORDS
        allowed = set(scope.circuit_ids) or None
        seen: set[tuple[str | None, str | None]] = set()
        candidates: list[ResolvedEntity] = []
        # 1. keyword recall.
        for row in self.engine.search_modules(scope.kb_name, query, keywords=keywords, limit=10):
            design_id = row.get("design_id")
            if allowed and design_id not in allowed:
                continue
            key = (design_id, row.get("module_id"))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                ResolvedEntity(
                    "module",
                    row.get("module_id"),
                    row.get("name") or row.get("module_id"),
                    circuit_id=design_id,
                    confidence=0.65,
                    reason="recovery_module_search",
                )
            )
        # 2. semantic recall over module description docs (no-op when no embed model).
        if self.engine.vector_index.is_available():
            for row in self.engine.search_module_descriptions(
                scope.kb_name, query, circuit_ids=scope.circuit_ids or None, limit=10
            ):
                key = (row.get("design_id"), row.get("module_id"))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    ResolvedEntity(
                        "module",
                        row.get("module_id"),
                        row.get("module_name") or row.get("module_id"),
                        circuit_id=row.get("design_id"),
                        confidence=0.55,
                        reason="recovery_semantic_search",
                    )
                )
        # Keyword matches (0.65) outrank semantic guesses (0.55); stable sort.
        candidates.sort(key=lambda entity: entity.confidence, reverse=True)
        return candidates
