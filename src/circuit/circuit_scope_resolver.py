from __future__ import annotations

from src.circuit.intent_parser import IntentParser
from src.circuit.query_context import CircuitIntent, CircuitScope, CircuitSessionContext, ClarificationOption
from src.circuit.query_engine import CircuitQueryEngine


_ALL_SCOPE = {"all_circuits", "all", "全部", "所有"}


class CircuitScopeResolver:
    def __init__(self, engine: CircuitQueryEngine | None = None):
        self.engine = engine or CircuitQueryEngine()

    def resolve(
        self,
        question: str,
        kb_name: str,
        intent: CircuitIntent,
        session_context: CircuitSessionContext,
        circuit_id: str | None = None,
        circuit_scope: dict | None = None,
        upstream_hint: dict | None = None,
    ) -> CircuitScope:
        designs = self.engine.store.list_designs(kb_name)
        by_id = {design.design_id: design for design in designs}

        explicit = circuit_id or (upstream_hint or {}).get("circuit_id")
        if explicit:
            if explicit in by_id:
                return self._single(kb_name, explicit, "explicit circuit_id", 1.0)
            return self._unresolved(kb_name, f"未找到指定电路 `{explicit}`。", designs)

        if circuit_scope:
            scope_type = str(circuit_scope.get("scope_type") or "")
            ids = [str(item) for item in circuit_scope.get("circuit_ids", [])]
            if scope_type in _ALL_SCOPE:
                return self._all(kb_name, designs, "explicit all_circuits")
            valid = [item for item in ids if item in by_id]
            if len(valid) == 1:
                return self._single(kb_name, valid[0], "explicit circuit_scope", 1.0)
            if len(valid) > 1:
                return CircuitScope("multiple_circuits", kb_name, valid, reason="explicit circuit_scope", confidence=1.0)

        source_files = []
        source_files.extend(IntentParser.extract_source_files(question))
        hint_file = (upstream_hint or {}).get("source_file")
        if hint_file:
            source_files.append(str(hint_file))
        for source_file in source_files:
            matches = self.engine.resolve_circuit_by_file(kb_name, source_file)
            if len(matches) == 1:
                match = matches[0]
                return CircuitScope(
                    "single_circuit",
                    kb_name,
                    [match["design_id"]],
                    matched_files=[source_file],
                    reason="source_file matched",
                    confidence=1.0,
                )
            if len(matches) > 1:
                return self._unresolved(kb_name, f"文件 `{source_file}` 匹配到多个电路。", designs)

        if intent.is_global_query:
            return self._all(kb_name, designs, "global query")

        if session_context.current_circuit_id in by_id and not intent.is_global_query:
            return self._single(kb_name, session_context.current_circuit_id, "session current circuit", 0.9)

        if len(designs) == 1:
            return self._single(kb_name, designs[0].design_id, "only circuit in kb", 0.95)

        if intent.entity_text:
            matches = self.engine.search_entity_across_circuits(
                kb_name,
                intent.entity_text,
                intent.target_entity_type,
                current_circuit_id=session_context.current_circuit_id,
            )
            circuit_ids = sorted({item["design_id"] for item in matches})
            if len(circuit_ids) == 1:
                return self._single(kb_name, circuit_ids[0], "unique entity match", 0.9)
            if len(circuit_ids) > 1:
                if intent.is_single_entity_detail:
                    return self._unresolved(
                        kb_name,
                        f"当前知识库中有多个 EDF/电路包含 `{intent.entity_text}`。",
                        [by_id[item] for item in circuit_ids if item in by_id],
                        include_all=True,
                    )
                return CircuitScope("multiple_circuits", kb_name, circuit_ids, reason="entity matched multiple circuits", confidence=0.8)

        if intent.intent in {"list_modules", "circuit_overview"} and len(designs) > 1:
            return self._unresolved(kb_name, "当前知识库中有多个电路，请指定要查询的 EDF/电路，或说明查询所有 EDF。", designs)

        return self._unresolved(kb_name, "无法确定要查询的电路范围。", designs)

    def _single(self, kb_name: str, design_id: str, reason: str, confidence: float) -> CircuitScope:
        return CircuitScope("single_circuit", kb_name, [design_id], reason=reason, confidence=confidence)

    def _all(self, kb_name: str, designs, reason: str) -> CircuitScope:
        return CircuitScope("all_circuits", kb_name, [design.design_id for design in designs], reason=reason, confidence=0.95)

    def _unresolved(self, kb_name: str, reason: str, designs, include_all: bool = False) -> CircuitScope:
        options = []
        for design in designs:
            files = [file.file_name for file in design.files]
            label = files[0] if files else design.design_id
            options.append(
                ClarificationOption(
                    label=label,
                    value=design.design_id,
                    option_type="circuit",
                    metadata={"source_files": files, "circuit_id": design.design_id},
                )
            )
        if include_all:
            options.append(ClarificationOption(label="全部列出", value="all", option_type="action", metadata={}))
        return CircuitScope("unresolved", kb_name, [], reason=reason, confidence=0.0, clarification_options=options)
