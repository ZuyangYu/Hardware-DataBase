from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from src.circuit.query_context import CircuitIntent, CircuitScope, CircuitSessionContext, ResolvedEntity
from src.circuit.query_engine import CircuitQueryEngine


_FOLLOWUP_TERMS = ("它", "这个模块", "该模块", "刚才那个", "刚才的", "上一个")
_POWER_KEYWORDS = ("电源", "供电", "降压", "稳压", "电压")
_POWER_EXPANSION = ["power", "supply", "buck", "regulator", "voltage", "vcc", "vdd"]

# Intents whose subject is a module / instance, so EntityResolver must run.
_MODULE_INTENTS = {"module_detail", "module_instances", "module_interfaces", "find_related_modules", "pdf_location"}
_INSTANCE_INTENTS = {"instance_connections", "instance_detail"}


@dataclass
class EntityResolution:
    resolved_entities: list[ResolvedEntity] = field(default_factory=list)
    candidates: list[ResolvedEntity] = field(default_factory=list)
    needs_clarification: bool = False
    reason: str | None = None


class EntityResolver:
    """Resolve circuit-domain entity mentions without executing final queries."""

    def __init__(self, engine: CircuitQueryEngine | None = None):
        self.engine = engine or CircuitQueryEngine()

    def resolve(
        self,
        question: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        session_context: CircuitSessionContext,
    ) -> EntityResolution:
        if scope.scope_type != "single_circuit":
            return EntityResolution()
        if intent.intent == "power_distribution" and intent.target_entity_type != "module":
            return EntityResolution()
        if intent.target_entity_type == "module" or intent.intent in _MODULE_INTENTS:
            return self.resolve_module(question, intent, scope, session_context)
        if intent.target_entity_type == "instance" or intent.intent in _INSTANCE_INTENTS:
            return self.resolve_instance(question, intent, scope)
        return EntityResolution()

    def resolve_module(
        self,
        question: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        session_context: CircuitSessionContext,
    ) -> EntityResolution:
        circuit_id = scope.circuit_ids[0] if scope.circuit_ids else None
        if not circuit_id:
            return EntityResolution()

        if intent.ordinal:
            entity = self._module_from_session(session_context.last_entities, circuit_id, ordinal=intent.ordinal)
            if entity:
                return EntityResolution([entity])

        if self._has_followup_reference(question) and not intent.entity_text:
            modules = self._session_modules(session_context.last_entities, circuit_id)
            if len(modules) == 1:
                return EntityResolution([modules[0]])
            if len(modules) > 1:
                return EntityResolution(candidates=modules, needs_clarification=True, reason="指代的模块不唯一。")

        query = (intent.entity_text or "").strip()
        if not query:
            return EntityResolution()

        design = self.engine.store.load(scope.kb_name, circuit_id)
        if design:
            normalized = self._normalize(query)
            for module in design.modules:
                if normalized in {self._normalize(module.module_id), self._normalize(module.name)}:
                    return EntityResolution([
                        ResolvedEntity(
                            "module",
                            module.module_id,
                            module.name or module.module_id,
                            circuit_id=circuit_id,
                            confidence=1.0,
                            reason="exact_or_normalized_match",
                        )
                    ])

        candidates = self._module_candidates(scope.kb_name, scope.circuit_ids, query or question, question)
        if len(candidates) == 1:
            candidates[0].reason = candidates[0].reason or "module_search_match"
            return EntityResolution([candidates[0]])
        if len(candidates) > 1:
            return EntityResolution(candidates=candidates, needs_clarification=True, reason=f"找到多个可能的模块 `{query}`。")
        return EntityResolution(reason=f"未找到模块 `{query}`。")

    def resolve_instance(self, question: str, intent: CircuitIntent, scope: CircuitScope) -> EntityResolution:
        circuit_id = scope.circuit_ids[0] if scope.circuit_ids else None
        query = (intent.entity_text or "").strip()
        if not circuit_id or not query:
            return EntityResolution()
        design = self.engine.store.load(scope.kb_name, circuit_id)
        if design:
            normalized = self.engine._normalize_refdes(query)
            for instance in design.instances:
                if self.engine._normalize_refdes(instance.refdes) == normalized:
                    return EntityResolution([
                        ResolvedEntity(
                            "instance",
                            instance.refdes,
                            instance.refdes,
                            circuit_id=circuit_id,
                            confidence=1.0,
                            reason="refdes_normalized_match",
                        )
                    ])

            # Cross-reference match (plan §3.5 layer 4): a PDF label may map
            # to an EDF refdes. Only cross-refs whose edf_refdes resolves to a
            # real instance count, and only when the direct refdes match failed.
            resolved = []
            for ref in self.engine.search_cross_references(scope.kb_name, query, limit=10):
                if ref.get("design_id") != circuit_id:
                    continue
                instance = self.engine._find_instance_by_refdes(design, ref.get("edf_refdes"))
                if instance is None:
                    continue
                resolved.append(
                    ResolvedEntity(
                        "instance",
                        instance.refdes,
                        instance.refdes,
                        circuit_id=circuit_id,
                        confidence=max(0.6, float(ref.get("confidence") or 0.6)),
                        reason="cross_reference_match",
                    )
                )
            if len(resolved) == 1:
                return EntityResolution(resolved)
            if len(resolved) > 1:
                return EntityResolution(candidates=resolved, needs_clarification=True, reason=f"通过交叉引用找到多个元件 `{query}`。")

        candidates = []
        for row in self.engine.search_instances(scope.kb_name, query, limit=10):
            if row.get("design_id") not in set(scope.circuit_ids):
                continue
            candidates.append(
                ResolvedEntity(
                    "instance",
                    row.get("refdes"),
                    row.get("refdes"),
                    circuit_id=row.get("design_id"),
                    confidence=0.75,
                    reason="instance_search_match",
                )
            )
        if len(candidates) == 1:
            return EntityResolution([candidates[0]])
        if len(candidates) > 1:
            return EntityResolution(candidates=candidates, needs_clarification=True, reason=f"找到多个可能的元件 `{query}`。")
        return EntityResolution(reason=f"未找到元件 `{query}`。")

    def _module_candidates(self, kb_name: str, circuit_ids: list[str], query: str, question: str) -> list[ResolvedEntity]:
        keywords = self._expanded_keywords(query, question)
        rows = self.engine.search_modules(kb_name, query, keywords=keywords, limit=20)
        allowed = set(circuit_ids)
        candidates: list[ResolvedEntity] = []
        seen: set[tuple[str | None, str | None]] = set()
        for row in rows:
            design_id = row.get("design_id")
            module_id = row.get("module_id")
            if allowed and design_id not in allowed:
                continue
            key = (design_id, module_id)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                ResolvedEntity(
                    "module",
                    module_id,
                    row.get("name") or module_id,
                    circuit_id=design_id,
                    confidence=0.75,
                    reason="module_search_match",
                )
            )
        return candidates

    @staticmethod
    def _expanded_keywords(query: str, question: str) -> list[str] | None:
        text = f"{query} {question}"
        keywords: list[str] = []
        if query:
            keywords.append(query)
        if any(term in text for term in _POWER_KEYWORDS):
            keywords.extend(_POWER_EXPANSION)
        return keywords or None

    @staticmethod
    def _normalize(value: str | None) -> str:
        return re.sub(r"[^A-Z0-9一-鿿]+", "", str(value or "").upper())

    @staticmethod
    def _has_followup_reference(question: str) -> bool:
        lowered = (question or "").lower()
        return any(term in lowered for term in _FOLLOWUP_TERMS)

    @staticmethod
    def _session_modules(entities: Iterable[ResolvedEntity], circuit_id: str) -> list[ResolvedEntity]:
        modules = []
        seen = set()
        for entity in entities:
            if entity.entity_type != "module":
                continue
            if entity.circuit_id and entity.circuit_id != circuit_id:
                continue
            key = (entity.circuit_id, entity.entity_id)
            if key in seen:
                continue
            seen.add(key)
            modules.append(entity)
        return modules

    def _module_from_session(self, entities: Iterable[ResolvedEntity], circuit_id: str, ordinal: int) -> ResolvedEntity | None:
        for entity in self._session_modules(entities, circuit_id):
            if entity.ordinal == ordinal:
                return entity
        return None
