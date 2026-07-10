"""Controlled circuit-domain agent (plan §3.2).

``CircuitQueryAgent`` is the EDF/PDF schematic tool's internal domain agent.
It is a bounded Plan-and-Execute + bounded-recovery agent (NOT an open ReAct
loop — plan §6.2). The flow mirrors plan §3.2:

    load session context → intent pre-parse → resolve circuit scope →
    build CircuitQueryContext → resolve entities → plan → execute bounded
    tools (Map-Reduce across circuits) → recover if needed → synthesize answer
    with evidence → update session context → return CircuitToolResponse

The agent never reads ``circuit_state.json`` directly and never walks raw PDF
content — it calls ``CircuitQueryEngine`` / index retrieval functions only
(plan §5.2). State for a single query lives on a ``CircuitQueryContext``
(plan §4.1.4); only ``last_entities`` / ``current_circuit_id`` /
``last_answer_summary`` are written back to the session store.

``query_tool.query_circuit_data`` is a thin entry point that delegates here.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Callable

from src.circuit.answer_synthesizer import AnswerValidation, CircuitAnswerSynthesizer
from src.circuit.circuit_scope_resolver import CircuitScopeResolver
from src.circuit.entity_resolver import EntityResolver
from src.circuit.intent_parser import IntentParser
from src.circuit.llm_controlled_planner import LLMControlledPlanner, LLMPlanDecision
from src.circuit.query_context import (
    CircuitQueryEvaluation,
    CircuitIntent,
    CircuitQueryContext,
    CircuitScope,
    CircuitToolResponse,
    CircuitQueryTrace,
    ResolvedEntity,
)
from src.circuit.query_evidence import QueryEvidenceBuilder
from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.query_planner import BoundedPlanExecutor, ExecutorResult, QueryPlanner
from src.circuit.recovery_manager import RecoveryManager
from src.circuit.response_policy import CircuitResponsePolicySelector
from src.circuit.session_context_store import SessionContextStore
from src.core.logger import log


SUPPORTED_INTENTS = {
    "list_modules",
    "circuit_overview",
    "module_detail",
    "module_instances",
    "module_interfaces",
    "instance_connections",
    "instance_detail",
    "net_connections",
    "net_detail",
    "power_distribution",
    "power_topology",
    "trace_signal_path",
    "find_related_modules",
    "pdf_location",
    "cross_reference_status",
    "list_circuits",
    "entity_search",
    "multi_part_circuit_query",
}

# Intents whose "not found" should go through entity-recovery (candidates /
# re-ask) rather than a bare partial message.
_ENTITY_NOT_FOUND_INTENTS = {
    "module_detail",
    "module_instances",
    "module_interfaces",
    "instance_connections",
    "instance_detail",
    "find_related_modules",
    "pdf_location",
    "trace_signal_path",
    "net_connections",
    "net_detail",
}


class CircuitQueryAgent:
    """EDF/PDF schematic tool's internal controlled domain agent (plan §3.2)."""

    def __init__(
        self,
        engine: CircuitQueryEngine | None = None,
        session_store: SessionContextStore | None = None,
        entity_resolver: EntityResolver | None = None,
        recovery_manager: RecoveryManager | None = None,
        parser: IntentParser | None = None,
        scope_resolver: CircuitScopeResolver | None = None,
        planner: QueryPlanner | None = None,
        llm_planner: LLMControlledPlanner | None = None,
        answer_synthesizer: CircuitAnswerSynthesizer | None = None,
        response_policy_selector: CircuitResponsePolicySelector | None = None,
        executor_factory: Callable[[], BoundedPlanExecutor] | None = None,
    ):
        self.engine = engine or CircuitQueryEngine()
        self.session_store = session_store or SessionContextStore()
        self.entity_resolver = entity_resolver or EntityResolver(self.engine)
        self.recovery_manager = recovery_manager or RecoveryManager(self.engine)
        self.parser = parser or IntentParser()
        self.scope_resolver = scope_resolver or CircuitScopeResolver(self.engine)
        self.planner = planner or QueryPlanner()
        self.llm_planner = llm_planner or LLMControlledPlanner()
        self.answer_synthesizer = answer_synthesizer or CircuitAnswerSynthesizer()
        self.response_policy_selector = response_policy_selector or CircuitResponsePolicySelector()
        self._executor_factory = executor_factory

    # ── public API ──────────────────────────────────────────────────────────

    def query(
        self,
        question: str,
        kb_name: str,
        session_id: str,
        circuit_id: str | None = None,
        circuit_scope: dict | None = None,
        history: list | None = None,
        upstream_hint: dict | None = None,
    ) -> CircuitToolResponse:
        trace = CircuitQueryTrace(kb_name=kb_name, session_id=session_id or "anonymous", question=question)
        # Step 2: intent pre-parse (light-weight question-type classification).
        intent = self.parser.parse(question)
        trace.add(
            "intent_parse",
            intent=intent.intent,
            pre_intent=intent.pre_intent,
            entity_text=intent.entity_text,
            confidence=intent.confidence,
            planner_source=intent.planner_source,
        )
        if intent.intent not in SUPPORTED_INTENTS:
            llm_intent = self.llm_planner.interpret_intent(question, intent)
            if llm_intent is None or llm_intent.intent not in SUPPORTED_INTENTS:
                response = self.recovery_manager.unsupported_query(question, intent)
                return self._attach_response_metadata(response, trace, intent, None)
            intent = llm_intent
            trace.add(
                "llm_intent_recover",
                intent=intent.intent,
                entity_text=intent.entity_text,
                target_entity_type=intent.target_entity_type,
                confidence=intent.confidence,
            )

        # Step 1: load session context.
        session_context = self.session_store.load(session_id or "anonymous", kb_name)
        trace.add(
            "session_load",
            current_circuit_id=session_context.current_circuit_id,
            last_entity_count=len(session_context.last_entities),
        )
        # plan §4.6: clear stale last_entities if the circuit was re-parsed.
        stale = self._check_circuit_staleness(session_context)
        if stale:
            trace.add("session_staleness", "recovered", "cleared stale last_entities")

        # Step 3: resolve circuit scope (single / multiple / all / unresolved).
        scope = self.scope_resolver.resolve(
            question=question,
            kb_name=kb_name,
            intent=intent,
            session_context=session_context,
            circuit_id=circuit_id,
            circuit_scope=circuit_scope,
            upstream_hint=upstream_hint,
        )
        trace.add(
            "scope_resolve",
            scope_type=scope.scope_type,
            circuit_ids=list(scope.circuit_ids),
            reason=scope.reason,
            confidence=scope.confidence,
        )
        if scope.scope_type == "unresolved":
            response = _clarification_response(question, intent, scope)
            response = self._attach_response_metadata(response, trace, intent, scope)
            self.session_store.update_after_response(session_context, response, scope, intent)
            return response

        # Step 4: build the single-query context (plan §4.1.4).
        ctx = CircuitQueryContext(
            question=question,
            kb_name=kb_name,
            session_id=session_id or "anonymous",
            circuit_id=circuit_id,
            circuit_scope=scope,
            session_context=session_context,
            intent=intent,
        )

        # Step 6: resolve entities / pronouns.
        entity_resolution = self.entity_resolver.resolve(question, intent, scope, session_context)
        ctx.resolved_entities = entity_resolution.resolved_entities
        ctx.candidate_entities = entity_resolution.candidates
        trace.add(
            "entity_resolve",
            resolved_count=len(entity_resolution.resolved_entities),
            candidate_count=len(entity_resolution.candidates),
            needs_clarification=entity_resolution.needs_clarification,
            reason=entity_resolution.reason,
        )
        if entity_resolution.needs_clarification:
            response = self.recovery_manager.ambiguous_entity(
                intent, entity_resolution.candidates, entity_resolution.reason or "需要确认要查询的实体。"
            )
            response = self._attach_response_metadata(response, trace, intent, scope)
            self.session_store.update_after_response(session_context, response, scope, intent)
            return response

        # Steps 7-8: plan + bounded execute (Map-Reduce across circuits).
        plan = self.planner.plan(question, kb_name, intent, scope, entity_resolution.resolved_entities)
        response_policy = self.response_policy_selector.select(
            question,
            intent,
            scope,
            entity_resolution.resolved_entities,
            plan,
        )
        plan.parameters["response_policy"] = response_policy.to_dict()
        trace.add(
            "response_policy",
            mode=response_policy.mode,
            output_format=response_policy.output_format,
            verbosity=response_policy.verbosity,
            source=response_policy.source,
            reason=response_policy.reason,
        )
        if response_policy.mode == "agent_plan":
            llm_plan, llm_decision = self.llm_planner.plan(
                question,
                kb_name,
                intent,
                scope,
                entity_resolution.resolved_entities,
                plan,
                force=True,
            )
        else:
            llm_plan = None
            llm_decision = LLMPlanDecision(False, f"response policy selected {response_policy.mode}")
        trace.add(
            "llm_plan",
            use_llm_plan=llm_decision.use_llm_plan,
            reason=llm_decision.reason,
            raw_keys=sorted(llm_decision.raw.keys()) if llm_decision.raw else [],
        )
        if llm_plan is not None:
            plan = llm_plan
            plan.parameters.setdefault("response_policy", response_policy.to_dict())
            intent = plan.intent
        ctx.max_steps = plan.max_steps
        ctx.max_tool_calls = plan.max_tool_calls
        trace.max_steps = plan.max_steps
        trace.max_tool_calls = plan.max_tool_calls
        trace.add(
            "plan",
            status=plan.status,
            operation=plan.operation,
            step_count=len(plan.steps),
            has_map_step=plan.map_step is not None,
            max_steps=plan.max_steps,
            max_tool_calls=plan.max_tool_calls,
            planner=plan.parameters.get("planner", "rule"),
        )
        if plan.status == "unsupported":
            response = self.recovery_manager.unsupported_query(question, intent)
            return self._attach_response_metadata(response, trace, intent, scope)
        executor = self._make_executor()
        result = executor.execute(plan)
        ctx.tool_calls.append({"count": result.tool_calls})
        ctx.evidence.extend(result.evidence)
        ctx.missing_info.extend(result.missing_info)
        trace.tool_calls = result.tool_calls
        trace.add(
            "execute",
            status=result.status,
            tool_calls=result.tool_calls,
            evidence_count=len(result.evidence),
            missing_info=list(result.missing_info),
        )

        # Steps 9-10: recover / synthesize.
        response = self._finalize(question, kb_name, intent, scope, plan, result, ctx)
        trace.add(
            "synthesize",
            answer_mode=response.answer_mode,
            confidence=response.confidence,
            missing_info_count=len(response.missing_info),
        )
        response = self._attach_response_metadata(response, trace, intent, scope)

        # Step 11: update session context (current_circuit_id + last_entities).
        version = self.engine.store.circuit_version(scope.kb_name, scope.circuit_ids[0]) if scope.circuit_ids else None
        self.session_store.update_after_response(session_context, response, scope, intent, circuit_version=version)
        if stale:
            note = "电路数据已更新，已基于最新索引重新查询。"
            response.answer = f"{note}\n\n{response.answer}"
            response.missing_info.append(note)
        log(f"circuit_query trace: {trace.to_dict()}")
        return response

    # ── internals ───────────────────────────────────────────────────────────

    def _make_executor(self) -> BoundedPlanExecutor:
        if self._executor_factory is not None:
            return self._executor_factory()
        return BoundedPlanExecutor(engine=self.engine, recovery_manager=self.recovery_manager)

    def _check_circuit_staleness(self, session_context) -> bool:
        current_id = session_context.current_circuit_id
        if not current_id:
            return False
        current_version = self.engine.store.circuit_version(session_context.kb_name, current_id)
        if (
            current_version
            and session_context.current_circuit_version
            and current_version != session_context.current_circuit_version
        ):
            session_context.last_entities = []
            return True
        return False

    def _finalize(
        self,
        question: str,
        kb_name: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        plan,
        result: ExecutorResult,
        ctx: CircuitQueryContext,
    ) -> CircuitToolResponse:
        if result.status == "truncated":
            return _partial("检索过程中达到工具调用上限，已返回部分结果。", intent, scope, missing_info=result.missing_info)
        if result.status == "not_found":
            return _not_found(question, intent, scope, self.recovery_manager)
        response = _synthesize(question, kb_name, intent, scope, plan, result)
        if result.data.get("response_policy"):
            response.data.setdefault("response_policy", result.data.get("response_policy"))
        response = self.answer_synthesizer.synthesize(question, intent, scope, response)
        validation = self.answer_synthesizer.validate(question, intent, response)
        response.data.setdefault("validation", _validation_dict(validation))
        if validation.is_satisfied:
            return response
        return self._repair_unsatisfied_answer(question, kb_name, intent, scope, plan, result, response, validation)

    def _repair_unsatisfied_answer(
        self,
        question: str,
        kb_name: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        plan,
        result: ExecutorResult,
        response: CircuitToolResponse,
        validation: AnswerValidation,
    ) -> CircuitToolResponse:
        """One bounded validation-repair round.

        If the first answer does not cover the user's requested fields, collect
        low-cost supplemental facts from deterministic rule functions and then
        re-run fact-grounded synthesis. This keeps recovery bounded and avoids
        letting the LLM invent missing circuit facts.
        """
        supplemental: dict[str, Any] = {}
        if validation.needs_more_data:
            supplemental = self._collect_supplemental_facts(kb_name, intent, scope, plan)
        if supplemental:
            result.data.setdefault("supplemental", {}).update(supplemental)
            response.data.setdefault("supplemental", {}).update(supplemental)

        repaired = self.answer_synthesizer.synthesize(question, intent, scope, response, force_llm=True)
        second = self.answer_synthesizer.validate(question, intent, repaired)
        repaired.data["validation"] = {
            "initial": _validation_dict(validation),
            "after_repair": _validation_dict(second),
            "repair_attempted": True,
            "supplemental_keys": sorted(supplemental.keys()),
        }
        if not second.is_satisfied:
            repaired.missing_info.extend(
                item for item in second.missing_fields if item and item not in repaired.missing_info
            )
            repaired.follow_up_suggestions.extend(
                suggestion
                for suggestion in ["请指定更具体的模块/元件/网络", "查询电路概况或模块列表后继续追问"]
                if suggestion not in repaired.follow_up_suggestions
            )
            if repaired.answer_mode == "direct_answer":
                repaired.answer_mode = "partial_answer"
                repaired.confidence = min(repaired.confidence, 0.65)
        return repaired

    def _collect_supplemental_facts(
        self,
        kb_name: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        plan,
    ) -> dict[str, Any]:
        supplemental: dict[str, Any] = {}
        circuit_ids = list(scope.circuit_ids or [])
        if not circuit_ids:
            return supplemental

        if scope.scope_type == "single_circuit":
            circuit_id = circuit_ids[0]
            if intent.intent != "circuit_overview":
                overview = self._safe_tool_call(self.engine.get_circuit_overview, kb_name, circuit_id)
                if overview:
                    supplemental["overview"] = overview
            if intent.intent != "list_modules":
                modules = self._safe_tool_call(self.engine.list_modules, kb_name, circuit_id)
                if modules:
                    supplemental["modules"] = modules
            module_id = _resolved_entity_id(getattr(plan, "resolved_entities", []), "module")
            if module_id:
                if intent.intent != "module_interfaces":
                    interfaces = self._safe_tool_call(self.engine.get_module_interfaces, kb_name, circuit_id, module_id)
                    if interfaces:
                        supplemental["module_interfaces"] = interfaces
                if intent.intent not in {"module_detail", "module_instances"}:
                    instances = self._safe_tool_call(self.engine.get_module_instances, kb_name, circuit_id, module_id)
                    if instances:
                        supplemental["module_instances"] = instances
            refdes = _resolved_entity_id(getattr(plan, "resolved_entities", []), "instance") or intent.entity_text
            if refdes and intent.target_entity_type == "instance" and intent.intent != "instance_connections":
                connections = self._safe_tool_call(self.engine.get_instance_connections, kb_name, circuit_id, refdes)
                if connections:
                    supplemental["instance_connections"] = connections
        else:
            grouped = []
            for circuit_id in circuit_ids:
                modules = self._safe_tool_call(self.engine.list_modules, kb_name, circuit_id)
                if modules:
                    grouped.append({"circuit_id": circuit_id, "modules": modules})
            if grouped:
                supplemental["per_circuit_modules"] = grouped
        return supplemental

    @staticmethod
    def _safe_tool_call(func, *args):
        try:
            return func(*args)
        except Exception:
            return None

    def _attach_response_metadata(
        self,
        response: CircuitToolResponse,
        trace: CircuitQueryTrace,
        intent: CircuitIntent,
        scope: CircuitScope | None,
    ) -> CircuitToolResponse:
        trace.answer_mode = response.answer_mode
        if scope is not None:
            response.source_status = QueryEvidenceBuilder(self.engine).source_status(
                scope.kb_name,
                scope.circuit_ids,
                used_sources=_used_sources_for_intent(intent),
                scope_type=scope.scope_type,
            )
        evaluation = _evaluate_response(response, intent, scope)
        response.data.setdefault("trace", trace.to_dict())
        response.data.setdefault("evaluation", evaluation.to_dict())
        return response


# ── finalize helpers ────────────────────────────────────────────────────────


def _validation_dict(validation: AnswerValidation) -> dict[str, Any]:
    return {
        "is_satisfied": validation.is_satisfied,
        "reason": validation.reason,
        "missing_fields": list(validation.missing_fields),
        "needs_more_data": validation.needs_more_data,
        "needs_resynthesis": validation.needs_resynthesis,
    }


def _used_sources_for_intent(intent: CircuitIntent) -> list[str]:
    if intent.intent in {"pdf_location", "cross_reference_status"}:
        return ["circuit_state", "pdf_schematic"]
    if intent.intent in {"instance_connections", "net_connections", "trace_signal_path", "power_distribution", "power_topology"}:
        return ["circuit_state", "edf_netlist", "connectivity_graph"]
    if intent.intent == "multi_part_circuit_query":
        return ["circuit_state", "edf_netlist", "pdf_schematic", "connectivity_graph"]
    if intent.intent in {"module_screenshot"}:
        return ["circuit_state", "pdf_schematic", "module_screenshots"]
    return ["circuit_state", "edf_netlist"]


def _evaluate_response(
    response: CircuitToolResponse,
    intent: CircuitIntent,
    scope: CircuitScope | None,
) -> CircuitQueryEvaluation:
    checks = {
        "answer_mode_valid": response.answer_mode
        in {"direct_answer", "grouped_by_circuit", "needs_clarification", "partial_answer", "unsupported"},
        "scope_resolved_or_explained": bool(scope is None or scope.scope_type != "unresolved" or response.answer_mode == "needs_clarification"),
        "has_source_status": response.source_status is not None or scope is None,
        "has_evidence_or_explains_gap": bool(response.evidence)
        or response.answer_mode in {"needs_clarification", "partial_answer", "unsupported"},
        "confidence_in_range": 0.0 <= float(response.confidence or 0.0) <= 1.0,
    }
    notes = list(response.missing_info)
    validation = response.data.get("validation")
    if isinstance(validation, dict):
        latest = validation.get("after_repair") if isinstance(validation.get("after_repair"), dict) else validation
        if latest.get("reason"):
            notes.append(str(latest["reason"]))
        if latest.get("is_satisfied") is False:
            checks["answer_validation_satisfied"] = False
    elif response.answer_mode in {"direct_answer", "grouped_by_circuit"}:
        checks["answer_validation_present"] = False
    return CircuitQueryEvaluation(passed=all(checks.values()), checks=checks, notes=notes)


def _resolved_entity_id(entities: list[ResolvedEntity], entity_type: str) -> str | None:
    for entity in entities or []:
        if entity.entity_type == entity_type:
            return entity.entity_id
    return None


def _not_found(
    question: str,
    intent: CircuitIntent,
    scope: CircuitScope,
    recovery_manager: RecoveryManager,
) -> CircuitToolResponse:
    if intent.intent in _ENTITY_NOT_FOUND_INTENTS:
        circuit_id = scope.circuit_ids[0] if scope.circuit_ids else None
        design = recovery_manager.engine.store.load(scope.kb_name, circuit_id) if circuit_id else None
        # No EDF netlist → connection / instance / path queries can't work.
        if design is not None and not design.instances and not design.nets:
            return recovery_manager.source_missing(
                f"电路 `{circuit_id}` 缺少 EDF 网表，无法执行该查询。", intent, scope, missing_source="edf_netlist"
            )
        return recovery_manager.entity_not_found(question, intent, scope, intent.entity_text)
    return _partial("未找到相关电路数据。", intent, scope)


def _synthesize(
    question: str,
    kb_name: str,
    intent: CircuitIntent,
    scope: CircuitScope,
    plan,
    result: ExecutorResult,
) -> CircuitToolResponse:
    op = intent.intent
    data = result.data

    if op == "list_modules":
        if plan.answer_key == "grouped":
            return _answer_grouped_modules(data.get("grouped") or {}, intent, scope, result)
        return _answer_single_modules(data.get("modules") or {}, intent, scope, result)

    if op == "circuit_overview":
        if plan.answer_key == "grouped":
            return _answer_grouped_overview(data.get("grouped") or {}, intent, scope, result)
        return _answer_overview(data.get("overview") or {}, intent, scope, result)

    if op in {"module_detail", "module_instances"}:
        payload = data.get(plan.answer_key) or {}
        return _answer_module_detail(payload, intent, scope, result)

    if op == "module_interfaces":
        return _answer_module_interfaces(data.get("interfaces") or {}, intent, scope, result)

    if op == "instance_connections":
        return _answer_instance_connections(data.get("instance") or {}, intent, scope, result)

    if op == "instance_detail":
        return _answer_instance_detail(data.get("instance") or {}, intent, scope, result)

    if op in {"net_connections", "net_detail"}:
        return _answer_net_connections(data.get("net") or {}, intent, scope, result)

    if op == "power_distribution":
        return _answer_power_distribution(intent, scope, result)

    if op == "power_topology":
        if plan.answer_key == "grouped":
            return _answer_grouped_power_topology(data.get("grouped") or {}, intent, scope, result)
        return _answer_power_topology(data.get("power_topology") or {}, intent, scope, result)

    if op == "trace_signal_path":
        return _answer_signal_path(data.get("path") or {}, intent, scope, result)

    if op == "find_related_modules":
        return _answer_related_modules(data.get("related") or {}, intent, scope, result)

    if op == "pdf_location":
        return _answer_pdf_region(data.get("region") or {}, intent, scope, result)

    if op == "cross_reference_status":
        return _answer_xref_status(data.get("xref") or {}, intent, scope, result)

    if op == "list_circuits":
        return _answer_list_circuits(data.get("circuits") or [], intent, scope, result)

    if op == "entity_search":
        return _answer_entity_search(data.get("entity_hits") or [], intent, scope, result)

    if op == "multi_part_circuit_query":
        # Agentic planning rewrites every intent to ``multi_part_circuit_query``
        # and emits a ``planned_results`` list, which would otherwise force the
        # generic text summarizer (``_answer_planned_results``) and bypass the
        # dedicated renderers — e.g. the power-topology Mermaid graph that
        # ``_answer_power_topology`` emits. When the plan actually wraps a single
        # intent with a dedicated renderer, delegate to it so output_format
        # choices (mermaid/table/…) take effect.
        rendered = _try_render_single_intent_planned(data, intent, scope, result)
        if rendered is not None:
            return rendered
        return _answer_planned_results(data.get("planned_results") or [], intent, scope, result)

    return CircuitToolResponse(answer="该电路查询暂未支持。", answer_mode="unsupported", data={"intent": op})


# ── answer formatters (plan §3.8 DomainSynthesizer) ─────────────────────────


def _answer_single_modules(result: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    modules = result.get("modules", [])
    names = [module.get("name") or module.get("module_id") for module in modules]
    circuit = result.get("circuit_id")
    lines = [f"当前电路 `{circuit}` 共识别到 {len(modules)} 个模块："]
    lines.extend(f"{idx}. {name}" for idx, name in enumerate(names, 1))
    entities = [
        ResolvedEntity("module", module.get("module_id"), module.get("name") or module.get("module_id"), circuit_id=circuit, ordinal=idx)
        for idx, module in enumerate(modules, 1)
    ]
    return CircuitToolResponse(
        answer="\n".join(lines),
        answer_mode="direct_answer",
        data={"intent": intent.intent, "scope": scope.scope_type, "result": result},
        evidence=result_exec.evidence,
        confidence=1.0,
        resolved_entities=entities,
    )


def _answer_grouped_modules(grouped: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    groups = grouped.get("grouped", [])
    lines = [f"当前知识库中共有 {len(groups)} 个电路/EDF，模块列表如下："]
    for index, group in enumerate(groups, 1):
        result = group.get("result") or {}
        modules = result.get("modules", [])
        circuit_id = group.get("circuit_id")
        label = group.get("source_file") or circuit_id
        lines.append(f"\n{index}. {label} (`{circuit_id}`)：{result.get('module_count', len(modules))} 个模块")
        for module in modules:
            lines.append(f"   - {module.get('name') or module.get('module_id')}")
    return CircuitToolResponse(
        answer="\n".join(lines),
        answer_mode="grouped_by_circuit",
        data={"intent": intent.intent, "scope": scope.scope_type, "grouped": grouped},
        evidence=result_exec.evidence,
        confidence=0.95,
    )


def _answer_overview(result: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    lines = [
        f"电路 `{result.get('circuit_id') or result.get('design_id')}` 概况：",
        f"- 实例数：{result.get('instance_count', result.get('instances', 0))}",
        f"- 网络数：{result.get('net_count', result.get('nets', 0))}",
        f"- 模块数：{result.get('module_count', len(result.get('modules', [])))}",
    ]
    modules = result.get("modules") or []
    if modules:
        lines.append("- 模块：" + "、".join(m.get("name") or m.get("module_id") for m in modules))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": result}, evidence=result_exec.evidence, confidence=1.0
    )


def _answer_grouped_overview(grouped: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    groups = grouped.get("grouped", [])
    lines = ["当前知识库电路概况："]
    for group in groups:
        item = group.get("result") or {}
        lines.append(
            f"- `{item.get('circuit_id')}`：{item.get('instance_count', 0)} 实例 / "
            f"{item.get('net_count', 0)} 网络 / {item.get('module_count', 0)} 模块"
        )
    return CircuitToolResponse(
        "\n".join(lines), "grouped_by_circuit", data={"intent": intent.intent, "grouped": grouped}, evidence=result_exec.evidence, confidence=0.95
    )


def _answer_module_detail(result: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    instances = result.get("instances", [])
    lines = [f"模块 `{result.get('name')}` 包含 {len(instances)} 个元件："]
    for inst in instances[:50]:
        lines.append(f"- {inst.get('refdes')}: {inst.get('library_cell') or '-'}")
    entity = ResolvedEntity("module", result.get("module_id"), result.get("name") or result.get("module_id"), circuit_id=result.get("circuit_id"))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": result}, evidence=result_exec.evidence, confidence=1.0, resolved_entities=[entity]
    )


def _answer_module_interfaces(result: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    interfaces = result.get("interfaces", [])
    lines = [f"模块 `{result.get('module')}` 对外接口（{len(interfaces)} 个外部网络）："]
    for iface in interfaces:
        peers = "、".join(iface.get("external_endpoints", [])[:6])
        modules = "、".join(iface.get("external_modules", []))
        suffix = f"（连接到：{modules}）" if modules else ""
        lines.append(f"- {iface.get('net_name')}（{iface.get('net_type')}）：{peers}{suffix}")
    entity = ResolvedEntity("module", result.get("module_id"), result.get("module") or result.get("module_id"), circuit_id=result.get("circuit_id"))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": result}, evidence=result_exec.evidence, confidence=1.0, resolved_entities=[entity]
    )


def _answer_instance_connections(result: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    lines = [f"元件 `{result.get('refdes')}` 的连接关系："]
    for pin in result.get("pins", []):
        peers = "、".join(pin.get("peers") or [])
        lines.append(f"- 引脚 {pin.get('name')} → 网络 {pin.get('net_name') or '-'}" + (f"，连接：{peers}" if peers else ""))
    entity = ResolvedEntity("instance", result.get("refdes"), result.get("refdes"), circuit_id=result.get("circuit_id"))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": result}, evidence=result_exec.evidence, confidence=1.0, resolved_entities=[entity]
    )


def _answer_instance_detail(result: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    lines = [f"元件 `{result.get('refdes')}` 详情："]
    if result.get("library_cell"):
        lines.append(f"- 型号：{result.get('library_cell')}")
    if result.get("part_number"):
        lines.append(f"- 零件号：{result.get('part_number')}")
    if result.get("footprint"):
        lines.append(f"- 封装：{result.get('footprint')}")
    if result.get("value"):
        lines.append(f"- 参数：{result.get('value')}")
    lines.append(f"- 引脚数：{result.get('pin_count', len(result.get('pins', [])))}")
    entity = ResolvedEntity("instance", result.get("refdes"), result.get("refdes"), circuit_id=result.get("circuit_id"))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": result}, evidence=result_exec.evidence, confidence=1.0, resolved_entities=[entity]
    )


def _answer_net_connections(result: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    connections = result.get("connections", [])
    lines = [f"网络 `{result.get('net_name')}` 连接了 {len(connections)} 个元件："]
    for conn in connections[:30]:
        endpoint = conn.get("endpoint") or conn.get("refdes")
        modules = "、".join(conn.get("module_names") or [])
        suffix = f"（模块：{modules}）" if modules else ""
        lines.append(f"- {endpoint}{suffix}")
    entity = ResolvedEntity("net", result.get("net_name"), result.get("net_name"), circuit_id=result.get("circuit_id"))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": result}, evidence=result_exec.evidence, confidence=1.0, resolved_entities=[entity]
    )


def _answer_power_distribution(intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    data = result_exec.data
    if data.get("power_tree"):
        # Net-scoped distribution ("5V 电源经过哪些模块"): which modules does the
        # named supply net feed. Built by get_power_distribution_tree (plan §3.6).
        return _format_power_tree(data["power_tree"], intent, scope, result_exec)
    if "power" in data and data["power"]:
        # Module-scoped power query.
        return _format_power_results([data["power"]], intent, scope, result_exec, answer_mode="direct_answer")
    rows = data.get("power_rows") or []
    allowed = set(scope.circuit_ids)
    if allowed:
        rows = [row for row in rows if (row.get("circuit_id") or row.get("design_id")) in allowed]
    if not rows:
        return _partial("未找到相关供电/电源网络信息。", intent, scope)
    answer_mode = "direct_answer" if scope.scope_type == "single_circuit" else "grouped_by_circuit"
    return _format_power_results(rows, intent, scope, result_exec, answer_mode=answer_mode)


def _answer_power_topology(topology: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    if not topology:
        return _partial("未找到可用于构建电源拓扑的结构化电路数据。", intent, scope, missing_source="edf_netlist")

    circuit_id = topology.get("circuit_id") or topology.get("design_id")
    edges = topology.get("conversion_edges") or []
    inferred_edges = topology.get("inferred_edges") or []
    converters = topology.get("converters") or []
    lines = [f"电源转换拓扑（电路 `{circuit_id}`）："]
    if edges:
        lines.append("")
        lines.append("```mermaid")
        lines.append("graph TD")
        for edge in _order_power_topology_edges(edges[:80]):
            source = _mermaid_node_id(edge.get("from_net") or "UNKNOWN_INPUT")
            target = _mermaid_node_id(edge.get("to_net") or "UNKNOWN_OUTPUT")
            source_label = edge.get("from_net") or "未知输入"
            target_label = edge.get("to_net") or "未知输出"
            via = edge.get("via_refdes") or "?"
            via_type = edge.get("via_type") or "power_device"
            edge_label = f"{via} {via_type}".strip()
            lines.append(
                f'    {source}["{_escape_mermaid_label(source_label)}"] -->|{_escape_mermaid_label(edge_label)}| {target}["{_escape_mermaid_label(target_label)}"]'
            )
        lines.append("```")
        lines.append("")
        if len(edges) > 80:
            lines.append(f"已展示前 80 条明确转换边，另有 {len(edges) - 80} 条未在图中展开。")
        roots = _power_topology_root_nets(topology, edges)
        if roots:
            lines.append("已解析源头电源轨：" + "、".join(f"`{root}`" for root in roots[:12]) + (" 等" if len(roots) > 12 else "") + "。")
            if len(roots) > 1:
                lines.append("多个源头/孤立分支表示当前结构化连接中没有解析到它们之间更上级的输入或转换边。")
        lines.append("图中只绘制明确识别的输入电源轨 → 电源器件 → 输出电源轨转换边。")
    else:
        lines.append("未识别到明确的输入 → 电源器件 → 输出电源轨转换边。")

    if edges:
        lines.append("")
        lines.append("关键转换关系：")
        for edge in edges[:12]:
            via = edge.get("via_refdes") or "?"
            via_type = edge.get("via_type") or "power_device"
            controls = "、".join(edge.get("control_nets") or [])
            suffix = f"，控制/状态：{controls}" if controls else ""
            lines.append(
                f"- `{edge.get('from_net') or '未知输入'}` → `{edge.get('to_net') or '未知输出'}`（{via}，{via_type}{suffix}）"
            )
        if len(edges) > 12:
            lines.append(f"- 其余 {len(edges) - 12} 条转换边已省略。")

    if inferred_edges:
        lines.append("")
        lines.append("疑似转换路径（未画入主图）：")
        for edge in inferred_edges[:8]:
            evidence = "；".join(str(item) for item in (edge.get("evidence") or [])[:2])
            suffix = f"，依据：{evidence}" if evidence else ""
            lines.append(
                f"- `{edge.get('from_net') or '未知输入'}` → `{edge.get('to_net') or '未知输出'}`（{edge.get('via_refdes') or '?'}，{edge.get('via_type') or 'power_device'}{suffix}）"
            )
        if len(inferred_edges) > 8:
            lines.append(f"- 其余 {len(inferred_edges) - 8} 条疑似路径已省略。")

    identified_converters = [
        item for item in converters if item.get("input_nets") or item.get("output_nets") or item.get("enable_nets") or item.get("power_good_nets")
    ]
    if identified_converters:
        lines.append("")
        lines.append("识别到的电源器件摘要：")
        for item in identified_converters[:12]:
            inputs = "、".join(item.get("input_nets") or []) or "未识别"
            outputs = "、".join(item.get("output_nets") or []) or "未识别"
            controls = "、".join((item.get("enable_nets") or []) + (item.get("power_good_nets") or []))
            suffix = f"，控制/状态：{controls}" if controls else ""
            lines.append(f"- `{item.get('refdes')}`（{item.get('type')}）：{inputs} → {outputs}{suffix}")
        if len(identified_converters) > 12:
            lines.append(f"- 其余 {len(identified_converters) - 12} 个器件已省略。")

    rails = [rail for rail in topology.get("rails") or [] if rail.get("produced_by")]
    if rails:
        lines.append("")
        lines.append("主要输出电源轨负载模块：")
        for rail in rails[:10]:
            modules = "、".join(m.get("module_name") or m.get("module_id") for m in rail.get("modules") or [])
            lines.append(f"- `{rail.get('net_name')}`：{modules or '未识别到模块归属'}")
        if len(rails) > 10:
            lines.append(f"- 其余 {len(rails) - 10} 个电源轨负载已省略。")

    missing = topology.get("missing_info") or []
    if missing:
        lines.append("")
        lines.append("不确定项：")
        for item in missing[:12]:
            lines.append(f"- {item}")
        if len(missing) > 12:
            lines.append(f"- 其余 {len(missing) - 12} 项已省略。")

    entities = [
        ResolvedEntity("instance", item.get("refdes"), item.get("refdes"), circuit_id=circuit_id, confidence=item.get("confidence", 0.6))
        for item in converters
        if item.get("refdes")
    ]
    return CircuitToolResponse(
        "\n".join(lines),
        "direct_answer" if edges else "partial_answer",
        data={"intent": intent.intent, "scope": scope.scope_type, "topology": topology, "result": topology},
        evidence=result_exec.evidence,
        confidence=float(topology.get("confidence", 0.5) or 0.5),
        missing_info=missing,
        resolved_entities=entities,
    )


def _answer_grouped_power_topology(grouped: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    rows = grouped.get("grouped") or []
    if not rows:
        return _partial("未找到可用于构建电源拓扑的跨电路结构化数据。", intent, scope, missing_source="edf_netlist")
    lines = ["跨电路电源转换拓扑摘要："]
    for row in rows:
        result = row.get("result") or {}
        circuit_id = row.get("circuit_id") or result.get("circuit_id")
        lines.append(f"- `{circuit_id}`：转换器 {result.get('converter_count', 0)} 个，转换边 {result.get('edge_count', 0)} 条")
        for edge in (result.get("conversion_edges") or [])[:8]:
            lines.append(f"  - {edge.get('from_net') or '未知输入'} → {edge.get('to_net')}（{edge.get('via_refdes')}）")
        for missing in (result.get("missing_info") or [])[:3]:
            lines.append(f"  - 不确定项：{missing}")
    return CircuitToolResponse(
        "\n".join(lines),
        "grouped_by_circuit",
        data={"intent": intent.intent, "scope": scope.scope_type, "grouped": grouped},
        evidence=result_exec.evidence,
        confidence=0.72,
    )


def _try_render_single_intent_planned(
    data: dict[str, Any],
    intent: CircuitIntent,
    scope: CircuitScope,
    result_exec: ExecutorResult,
) -> CircuitToolResponse | None:
    """Delegate an agentic multi-part plan to its dedicated renderer when the
    plan actually wraps a single intent that has one.

    Without this, a ``power_topology`` question reaches dispatch as
    ``multi_part_circuit_query`` (agentic planning rewrites the intent) and is
    answered by ``_answer_planned_results`` — a generic text summarizer that
    cannot emit the requested Mermaid graph. When ``source_intent`` names an
    intent with a dedicated renderer and the plan is a single matching step
    with data, use that renderer instead so format-specific output takes effect.

    Returns ``None`` for genuinely multi-part plans or unrecognized source
    intents, leaving the generic summarizer in charge.
    """
    source_intent = data.get("source_intent")
    planned = data.get("planned_results") or []
    if source_intent == "power_topology":
        topology = _single_power_topology_payload(planned)
        if topology is not None:
            # Reconstruct the source intent so the rendered metadata (and any
            # downstream intent-keyed logic) sees ``power_topology``, not the
            # rewritten ``multi_part_circuit_query`` label.
            source_intent_obj = replace(intent, intent="power_topology")
            rendered = _answer_power_topology(topology, source_intent_obj, scope, result_exec)
            _attach_single_intent_plan_metadata(rendered, data, planned, intent, source_intent)
            return rendered
    return None


def _attach_single_intent_plan_metadata(
    response: CircuitToolResponse,
    data: dict[str, Any],
    planned: list[dict[str, Any]],
    execution_intent: CircuitIntent,
    source_intent: str,
) -> None:
    """Preserve agent-plan metadata when a single-step plan uses a dedicated renderer."""
    response.data.setdefault("planned_results", planned)
    response.data.setdefault(
        "agent_plan",
        {
            "planner": data.get("planner"),
            "execution_intent": execution_intent.intent,
            "source_intent": source_intent,
            "goal": data.get("agent_goal"),
            "output_format": data.get("output_format"),
            "answer_style": data.get("answer_style"),
            "response_policy": data.get("response_policy"),
        },
    )


def _single_power_topology_payload(planned: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the topology dict when the plan is a single ``build_power_topology`` step.

    The rule-fallback planner stores the step under its rule ``as`` key
    (``power_topology``); the LLM-controlled planner stores it under
    ``planned_<index>_build_power_topology``. ``planned_results`` carries both
    uniformly, so scan it instead of guessing the key. Only short-circuit when
    there is exactly one step — a genuinely multi-part plan stays with the
    summarizer.
    """
    if len(planned) != 1:
        return None
    item = planned[0]
    if item.get("tool") != "build_power_topology":
        return None
    payload = item.get("result")
    if not isinstance(payload, dict):
        return None
    return payload


def _answer_planned_results(
    planned: list[dict[str, Any]],
    intent: CircuitIntent,
    scope: CircuitScope,
    result_exec: ExecutorResult,
) -> CircuitToolResponse:
    if not planned:
        return _partial("受控规划未返回可用结果。", intent, scope)
    lines = ["我按你的问题分步查询了电路结构化数据："]
    missing: list[str] = []
    entities: list[ResolvedEntity] = []
    for index, item in enumerate(planned, 1):
        tool = item.get("tool") or "-"
        purpose = item.get("purpose") or tool
        payload = item.get("result")
        lines.append(f"\n{index}. {purpose}（{tool}）")
        if _planned_payload_missing(tool, payload):
            lines.extend("   " + line for line in _planned_missing_lines(tool))
            missing.append(str(purpose))
            continue
        lines.extend("   " + line for line in _summarize_planned_payload(tool, payload))
        entities.extend(_entities_from_planned_payload(tool, payload))
    confidence = 0.85 if not missing else 0.65
    source_intent = result_exec.data.get("source_intent") or intent.intent
    data = {
        "intent": source_intent,
        "scope": scope.scope_type,
        "planned_results": planned,
        "agent_plan": {
            "planner": result_exec.data.get("planner"),
            "execution_intent": intent.intent,
            "source_intent": source_intent,
            "goal": result_exec.data.get("agent_goal"),
            "output_format": result_exec.data.get("output_format"),
            "answer_style": result_exec.data.get("answer_style"),
            "response_policy": result_exec.data.get("response_policy"),
        },
    }
    missing_source = _planned_missing_source(planned)
    if missing_source:
        data["missing_source"] = missing_source
    primary_result = _primary_planned_result(planned)
    if primary_result is not None:
        data["result"] = primary_result
    return CircuitToolResponse(
        "\n".join(lines),
        "direct_answer" if not missing else "partial_answer",
        data=data,
        evidence=result_exec.evidence,
        confidence=confidence,
        missing_info=missing,
        resolved_entities=entities,
    )


def _planned_payload_missing(tool: str, payload: Any) -> bool:
    if not payload:
        return True
    if not isinstance(payload, dict):
        return False
    if tool == "get_module_pdf_region":
        return not payload.get("regions")
    if tool == "get_instance_connections":
        return not payload.get("pins")
    if tool == "get_net_connections":
        return not payload.get("connections")
    if tool == "find_connected_modules":
        return not payload.get("connected_modules")
    if tool == "trace_signal_path":
        return not payload.get("found") or not payload.get("path")
    if tool == "build_power_topology":
        return not payload.get("conversion_edges")
    return False


def _planned_missing_lines(tool: str) -> list[str]:
    if tool == "get_module_pdf_region":
        return ["- 未找到 PDF 区域信息。"]
    if tool in {"get_instance_connections", "get_net_connections", "trace_signal_path"}:
        return ["- 未找到对应连接数据；可能缺少 EDF 网表或目标实体未被解析。"]
    if tool == "build_power_topology":
        return ["- 未识别到明确电源转换边；可能缺少转换器 VIN/VOUT/EN 等引脚角色或器件属性。"]
    return ["- 未找到对应数据。"]


def _planned_missing_source(planned: list[dict[str, Any]]) -> str | None:
    for item in planned:
        tool = item.get("tool") or ""
        if not _planned_payload_missing(tool, item.get("result")):
            continue
        if tool in {"get_instance_connections", "get_net_connections", "trace_signal_path"}:
            return "edf_netlist"
        if tool == "build_power_topology":
            return "edf_netlist"
        if tool == "get_module_pdf_region":
            return "pdf_schematic"
    return None


def _primary_planned_result(planned: list[dict[str, Any]]) -> Any:
    for item in planned:
        payload = item.get("result")
        if payload:
            return payload
    return None


def _entities_from_planned_payload(tool: str, payload: Any) -> list[ResolvedEntity]:
    if not isinstance(payload, dict):
        return []
    circuit_id = payload.get("circuit_id") or payload.get("design_id")
    if tool == "list_modules":
        return [
            ResolvedEntity("module", module.get("module_id"), module.get("name") or module.get("module_id"), circuit_id=circuit_id, ordinal=idx)
            for idx, module in enumerate(payload.get("modules") or [], 1)
        ]
    if tool == "get_power_distribution_tree":
        entities: list[ResolvedEntity] = []
        seen: set[str] = set()
        for net in (payload.get("power_nets") or []) + (payload.get("ground_nets") or []):
            for module in net.get("modules") or []:
                module_id = module.get("module_id")
                if not module_id or module_id in seen:
                    continue
                seen.add(module_id)
                entities.append(ResolvedEntity("module", module_id, module.get("module_name") or module_id, circuit_id=circuit_id))
        return entities
    if tool == "build_power_topology":
        return [
            ResolvedEntity("instance", item.get("refdes"), item.get("refdes"), circuit_id=circuit_id, confidence=item.get("confidence", 0.6))
            for item in payload.get("converters") or []
            if item.get("refdes")
        ]
    return []


def _summarize_planned_payload(tool: str, payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return [f"- {payload}"]
    if tool == "list_modules":
        modules = payload.get("modules") or []
        lines = [f"- 模块数：{len(modules)} 个模块"]
        lines.extend(f"- {module.get('name') or module.get('module_id')}" for module in modules[:20])
        return lines
    if tool == "get_module_power_nets":
        power = payload.get("power_nets") or []
        ground = payload.get("ground_nets") or []
        lines = [f"- 模块：{payload.get('module') or payload.get('name') or payload.get('module_id')}"]
        if power:
            lines.append("- 供电/输入相关网络：" + "、".join(net.get("name") or net.get("net_name") for net in power))
        if ground:
            lines.append("- 参考地网络：" + "、".join(net.get("name") or net.get("net_name") for net in ground))
        if power and not any(_looks_numeric_voltage(net.get("name") or net.get("net_name")) for net in power):
            lines.append("- 结构化 EDF 中提供的是网络名，不包含器件 datasheet 的精确电压范围。")
        return lines
    if tool == "get_module_interfaces":
        interfaces = payload.get("interfaces") or []
        lines = [f"- 模块：{payload.get('module') or payload.get('module_id')}，外部网络数量：{len(interfaces)}"]
        for iface in interfaces[:8]:
            external_modules = "、".join(iface.get("external_modules") or [])
            suffix = f"，外部模块：{external_modules}" if external_modules else ""
            lines.append(f"- {iface.get('net_name')}（{iface.get('net_type')}）{suffix}")
        return lines
    if tool in {"get_module_detail", "get_module_instances"}:
        instances = payload.get("instances") or []
        lines = [f"- 模块：{payload.get('name') or payload.get('module_id')}，元件数：{len(instances)}"]
        lines.extend(f"- {inst.get('refdes')}: {inst.get('library_cell') or '-'}" for inst in instances[:8])
        return lines
    if tool == "get_module_pdf_region":
        regions = payload.get("regions") or []
        if regions:
            first = regions[0]
            return [
                f"- 模块：{payload.get('module') or payload.get('module_id')}",
                f"- 原理图页码：第 {first.get('page_number')} 页",
                f"- 区域置信度：{first.get('confidence', '-')}",
            ]
        return ["- 未找到 PDF 区域信息。"]
    if tool == "get_power_distribution_tree":
        power = payload.get("power_nets") or []
        ground = payload.get("ground_nets") or []
        lines = [f"- 电源网络数量：{len(power)}，参考地网络数量：{len(ground)}"]
        for net in power[:8]:
            modules = "、".join(m.get("module_name") or m.get("module_id") for m in net.get("modules") or [])
            suffix = f" → {modules}" if modules else ""
            lines.append(f"- {net.get('net_name')}（连接 {net.get('connection_count', 0)} 个元件）{suffix}")
        return lines
    if tool == "build_power_topology":
        edges = payload.get("conversion_edges") or []
        converters = payload.get("converters") or []
        lines = [f"- 电源转换器件：{len(converters)} 个，转换边：{len(edges)} 条"]
        for edge in edges[:8]:
            lines.append(f"- {edge.get('from_net') or '未知输入'} → {edge.get('to_net')}（{edge.get('via_refdes')}，{edge.get('via_type')}）")
        for rail in [item for item in payload.get("rails") or [] if item.get("produced_by")][:8]:
            modules = "、".join(m.get("module_name") or m.get("module_id") for m in rail.get("modules") or [])
            if modules:
                lines.append(f"- {rail.get('net_name')} 负载模块：{modules}")
        for missing in (payload.get("missing_info") or [])[:3]:
            lines.append(f"- 不确定项：{missing}")
        return lines
    if tool == "get_instance_connections":
        pins = payload.get("pins") or []
        lines = [f"- 元件：{payload.get('refdes')}，型号：{payload.get('library_cell') or '-'}，连接项数量：{len(pins)}"]
        for pin in pins[:12]:
            peers = "、".join(pin.get("peers") or [])
            peer_text = f"，连接到：{peers}" if peers else ""
            lines.append(f"- Pin {pin.get('name')} → {pin.get('net_name') or '-'}{peer_text}")
        return lines
    if tool == "get_instance_detail":
        pins = payload.get("pins") or []
        lines = [f"- 元件：{payload.get('refdes')}"]
        if payload.get("library_cell"):
            lines.append(f"- 型号：{payload.get('library_cell')}")
        if payload.get("part_number"):
            lines.append(f"- 零件号：{payload.get('part_number')}")
        if payload.get("footprint"):
            lines.append(f"- 封装：{payload.get('footprint')}")
        if payload.get("value"):
            lines.append(f"- 参数：{payload.get('value')}")
        lines.append(f"- 引脚数：{payload.get('pin_count', len(pins))}")
        if pins:
            lines.append("- 连接网络：" + "、".join(pin.get("net_name") for pin in pins[:12] if pin.get("net_name")))
        return lines
    if tool == "get_net_connections":
        connections = payload.get("connections") or []
        lines = [f"- 网络：{payload.get('net_name')}，连接了 {len(connections)} 个端点"]
        for conn in connections[:12]:
            endpoint = conn.get("endpoint") or conn.get("refdes")
            modules = "、".join(conn.get("module_names") or [])
            suffix = f"（模块：{modules}）" if modules else ""
            lines.append(f"- {endpoint}{suffix}")
        return lines
    if tool == "find_connected_modules":
        modules = payload.get("connected_modules") or []
        lines = [f"- 模块：{payload.get('module') or payload.get('module_id')}，相连模块数量：{len(modules)}"]
        for module in modules[:12]:
            shared = "、".join(module.get("shared_nets") or [])
            suffix = f"，共享网络：{shared}" if shared else ""
            lines.append(f"- {module.get('module_name') or module.get('module_id')}{suffix}")
        return lines
    if tool == "trace_signal_path":
        path = payload.get("path") or []
        hops = payload.get("hops") or []
        module_path = payload.get("module_path") or []
        lines = [
            f"- 信号路径：{payload.get('from_entity')} → {payload.get('to_entity')}，共 {payload.get('hop_count', len(hops))} 跳",
            "- 元件路径：" + " → ".join(path),
        ]
        if module_path:
            lines.append("- 经过模块：" + " → ".join(module_path))
        for hop in hops[:8]:
            lines.append(f"- {hop.get('from')} → {hop.get('to')}：{hop.get('net')}（{hop.get('net_type')}）")
        return lines
    if tool == "get_cross_reference_status":
        return [
            f"- 覆盖率：{payload.get('coverage', 0)}",
            f"- 交叉引用数量：{payload.get('cross_reference_count', 0)}",
            f"- 已映射元件：{payload.get('mapped_instance_count', 0)}",
            f"- 未映射元件：{payload.get('unmapped_instance_count', 0)}",
            f"- 平均置信度：{payload.get('avg_confidence', 0)}",
        ]
    if tool == "get_circuit_overview":
        return [
            f"- 实例数：{payload.get('instance_count', payload.get('instances', 0))}",
            f"- 网络数：{payload.get('net_count', payload.get('nets', 0))}",
            f"- 模块数：{payload.get('module_count', len(payload.get('modules', [])))}",
        ]
    return [f"- 结果字段：{', '.join(sorted(str(key) for key in payload.keys())[:8])}"]


def _looks_numeric_voltage(name: str | None) -> bool:
    return bool(name and re.search(r"\d+(?:[._]\d+)?\s*v|\d+v\d+", name, re.IGNORECASE))


def _mermaid_node_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "node")).strip("_")
    if not text:
        text = "node"
    if text[0].isdigit():
        text = "N_" + text
    return text


def _order_power_topology_edges(edges: list[dict]) -> list[dict]:
    return sorted(
        edges,
        key=lambda edge: (
            str(edge.get("from_net") or "UNKNOWN_INPUT"),
            str(edge.get("to_net") or "UNKNOWN_OUTPUT"),
            str(edge.get("via_refdes") or ""),
        ),
    )


def _power_topology_root_nets(topology: dict, edges: list[dict]) -> list[str]:
    roots = [str(name) for name in topology.get("root_nets") or [] if name]
    if roots:
        return sorted(set(roots))
    sources = {str(edge.get("from_net")) for edge in edges if edge.get("from_net")}
    targets = {str(edge.get("to_net")) for edge in edges if edge.get("to_net")}
    return sorted(sources - targets)


def _escape_mermaid_label(value: str) -> str:
    return str(value or "").replace('"', "'")


def _format_power_tree(tree: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    """Format a net→modules power-distribution tree, optionally filtered by net.

    ``tree`` is the output of ``get_power_distribution_tree``: each supply net
    carries the modules/instances it feeds. When ``intent.entity_text`` names a
    net (e.g. ``5V``) only matching nets are shown.
    """
    query = (intent.entity_text or "").lower().replace(" ", "")

    def _matches(net_name: str) -> bool:
        if not query:
            return True
        return query in (net_name or "").lower().replace(" ", "")

    nets = [net for net in (tree.get("power_nets") or []) if _matches(net.get("net_name"))]
    if query:
        # When a specific net was asked for, also surface ground nets that match.
        nets.extend(net for net in (tree.get("ground_nets") or []) if _matches(net.get("net_name")))
    circuit_id = tree.get("circuit_id") or tree.get("design_id")
    if not nets:
        return _partial(
            f"未在电路 `{circuit_id}` 中找到与 `{intent.entity_text}` 匹配的电源网络。",
            intent, scope,
        )
    lines = [f"电源分配（电路 `{circuit_id}`，{len(nets)} 个匹配网络）："]
    entities: list[ResolvedEntity] = []
    seen: set[str] = set()
    for net in nets:
        modules = net.get("modules") or []
        module_names = "、".join(m.get("module_name") or m.get("module_id") for m in modules)
        for m in modules:
            mid = m.get("module_id")
            if mid and mid not in seen:
                seen.add(mid)
                entities.append(ResolvedEntity("module", mid, m.get("module_name") or mid, circuit_id=circuit_id))
        suffix = f" → 模块：{module_names}" if module_names else "（无归属模块）"
        lines.append(f"- `{net.get('net_name')}`（{net.get('role')}，连接 {net.get('connection_count', 0)} 个元件）{suffix}")
    return CircuitToolResponse(
        "\n".join(lines),
        "direct_answer",
        data={"intent": intent.intent, "scope": scope.scope_type, "tree": tree, "filtered_nets": nets},
        evidence=result_exec.evidence,
        confidence=0.9,
        resolved_entities=entities,
    )


def _format_power_results(results: list[dict], intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult, answer_mode: str) -> CircuitToolResponse:
    lines = ["供电/电源网络查询结果："]
    for result in results:
        circuit_id = result.get("circuit_id") or result.get("design_id")
        lines.append(f"- `{result.get('module') or result.get('module_id')}`（{circuit_id}）：")
        power_nets = result.get("power_nets") or []
        ground_nets = result.get("ground_nets") or []
        if power_nets:
            lines.append("  - 电源网络：" + "、".join(_net_label(net) for net in power_nets))
        if ground_nets:
            lines.append("  - 参考地：" + "、".join(_net_label(net) for net in ground_nets))
        if not power_nets and not ground_nets:
            lines.append("  - 未识别到明确的电源或地网络")
    entities = [
        ResolvedEntity("module", item.get("module_id"), item.get("module") or item.get("module_id"), circuit_id=item.get("circuit_id") or item.get("design_id"))
        for item in results
    ]
    return CircuitToolResponse(
        answer="\n".join(lines),
        answer_mode=answer_mode,
        data={"intent": intent.intent, "scope": scope.scope_type, "results": results},
        evidence=result_exec.evidence,
        confidence=0.85,
        resolved_entities=entities,
    )


def _answer_signal_path(path: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    hops = path.get("hops", [])
    lines = [f"信号路径（`{path.get('from_entity')}` → `{path.get('to_entity')}`），共 {len(hops)} 跳："]
    for hop in hops:
        fm = hop.get("from_module")
        tm = hop.get("to_module")
        module_note = ""
        if fm or tm:
            module_note = f"，模块：{fm or '?'} → {tm or '?'}"
        lines.append(f"- {hop.get('from')} → {hop.get('to')}（网络：{hop.get('net')}，{hop.get('net_type')}{module_note}）")
    lines.append(f"路径：{' → '.join(path.get('path', []))}")
    module_path = path.get("module_path") or []
    if module_path:
        lines.append("经过模块：" + " → ".join(module_path))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": path}, evidence=result_exec.evidence, confidence=0.9
    )


def _answer_related_modules(related: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    modules = related.get("connected_modules", [])
    lines = [f"与模块 `{related.get('module')}` 相连的模块（{len(modules)} 个）："]
    for item in modules:
        nets = "、".join(item.get("shared_nets") or [])
        lines.append(f"- {item.get('module_name')}（共享网络：{nets}）")
    entity = ResolvedEntity("module", related.get("module_id"), related.get("module") or related.get("module_id"), circuit_id=related.get("circuit_id"))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": related}, evidence=result_exec.evidence, confidence=1.0, resolved_entities=[entity]
    )


def _answer_pdf_region(region: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    regions = region.get("regions", [])
    if not regions:
        return _partial(f"未在 PDF 中找到模块 `{region.get('module')}` 的位置信息。", intent, scope, source_missing=True)
    lines = [f"模块 `{region.get('module')}` 在原理图中的位置："]
    for item in regions:
        bbox = item.get("bbox")
        bbox_text = f"，区域 {bbox}" if bbox else ""
        lines.append(f"- 第 {item.get('page_number')} 页{bbox_text}（置信度 {item.get('confidence')}，策略 {item.get('strategy')}）")
    entity = ResolvedEntity("module", region.get("module_id"), region.get("module") or region.get("module_id"), circuit_id=region.get("circuit_id"))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": region}, evidence=result_exec.evidence, confidence=0.9, resolved_entities=[entity]
    )


def _answer_xref_status(xref: dict, intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    lines = [
        f"电路 `{xref.get('circuit_id')}` 交叉引用状态：",
        f"- 交叉引用条数：{xref.get('cross_reference_count', 0)}",
        f"- 已映射元件：{xref.get('mapped_instance_count', 0)}",
        f"- 未映射元件：{xref.get('unmapped_instance_count', 0)}",
        f"- 覆盖率：{xref.get('coverage', 0)}",
        f"- 平均置信度：{xref.get('avg_confidence', 0)}",
    ]
    strategies = xref.get("strategies") or []
    if strategies:
        lines.append("- 匹配策略：" + "、".join(strategies))
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "result": xref}, evidence=result_exec.evidence, confidence=1.0
    )


def _answer_list_circuits(circuits: list[dict], intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    lines = [f"当前知识库共有 {len(circuits)} 个电路/EDF："]
    for idx, circuit in enumerate(circuits, 1):
        files = circuit.get("source_files") or []
        label = files[0] if files else circuit.get("circuit_id")
        aliases = circuit.get("aliases") or []
        alias_text = f"（别名：{'、'.join(aliases[:3])}）" if aliases else ""
        lines.append(f"{idx}. {label} (`{circuit.get('circuit_id')}`) — {circuit.get('module_count', 0)} 模块 / {circuit.get('instance_count', 0)} 元件{alias_text}")
    return CircuitToolResponse(
        "\n".join(lines), "direct_answer", data={"intent": intent.intent, "circuits": circuits}, evidence=result_exec.evidence, confidence=1.0
    )


def _answer_entity_search(hits: list[dict], intent: CircuitIntent, scope: CircuitScope, result_exec: ExecutorResult) -> CircuitToolResponse:
    """Format cross-circuit entity discovery ("哪个 EDF 包含 CAN").

    ``hits`` come from ``search_entity_across_circuits`` (plan §4.7) — each
    carries ``circuit_id`` / ``source_files`` / ``entity_type`` / ``entity_id``.
    Results are grouped by circuit, preserving the engine's ranking order.
    """
    if not hits:
        return _partial(f"未在知识库的电路中找到 `{intent.entity_text}`。", intent, scope)
    groups: dict[str, list[dict]] = {}
    for hit in hits:
        groups.setdefault(hit.get("circuit_id") or hit.get("design_id") or "", []).append(hit)
    lines = [f"在以下电路中找到 `{intent.entity_text}`："]
    for circuit_id, group in groups.items():
        files = group[0].get("source_files") or []
        file_label = files[0] if files else circuit_id
        detail = "、".join(
            f"{h.get('entity_type')} `{h.get('display_name') or h.get('entity_id')}`" for h in group
        )
        lines.append(f"- {file_label} (`{circuit_id}`)：{detail}")
    entities = [
        ResolvedEntity(
            h.get("entity_type") or "entity",
            h.get("entity_id"),
            h.get("display_name") or h.get("entity_id"),
            circuit_id=h.get("circuit_id") or h.get("design_id"),
            confidence=float(h.get("confidence", 1.0) or 1.0),
        )
        for h in hits
    ]
    return CircuitToolResponse(
        "\n".join(lines),
        "grouped_by_circuit",
        data={"intent": intent.intent, "scope": scope.scope_type, "hits": hits},
        evidence=result_exec.evidence,
        confidence=0.9,
        resolved_entities=entities,
    )


# ── shared helpers ──────────────────────────────────────────────────────────


def _net_label(net: dict) -> str:
    label = net.get("name") or "-"
    reason = net.get("reason")
    if reason:
        return f"{label}（{reason}）"
    return label


def _clarification_response(question: str, intent: CircuitIntent, scope: CircuitScope) -> CircuitToolResponse:
    lines = [scope.reason or "需要进一步确认要查询的电路。"]
    if scope.clarification_options:
        lines.append("请选择要查询的 EDF/电路：")
        for idx, option in enumerate(scope.clarification_options, 1):
            lines.append(f"{idx}. {option.label}")
    return CircuitToolResponse(
        answer="\n".join(lines),
        answer_mode="needs_clarification",
        data={"intent": intent.intent, "scope": scope.scope_type, "question": question},
        clarification_options=scope.clarification_options,
        confidence=0.0,
    )


def _partial(message: str, intent: CircuitIntent, scope: CircuitScope, missing_info: list[str] | None = None, source_missing: bool = False) -> CircuitToolResponse:
    suggestions = None
    if source_missing:
        suggestions = ["上传对应 EDF/PDF 后重新查询", "查询模块列表或电路概况"]
    return CircuitToolResponse(
        answer=message,
        answer_mode="partial_answer",
        data={"intent": intent.intent, "scope": scope.scope_type},
        missing_info=missing_info or [message],
        follow_up_suggestions=suggestions or [],
        confidence=0.4,
    )
