"""Bounded Plan-and-Execute for the circuit domain agent (plan §3.6 / §6.2).

``QueryPlanner`` turns a parsed ``(intent, scope, resolved_entities)`` triple
into a finite ``CircuitQueryPlan`` — a list of deterministic tool steps (or,
for cross-circuit queries, a Map-Reduce ``map_step`` + reduce strategy) plus
bounded budgets.

``BoundedPlanExecutor`` runs that plan against ``CircuitQueryEngine``:

* resolves ``$var`` references between steps (so a step can consume a value
  selected from a prior step's output — e.g. ``search_nets`` → pick first net
  → ``get_net_connections``);
* enforces ``max_steps`` / ``max_tool_calls`` so the agent can never run away;
* records per-step ``evidence`` carrying ``circuit_id`` / ``source_files``
  provenance (plan §4.8);
* runs cross-circuit queries as Map-Reduce via
  ``CircuitQueryEngine.aggregate_circuit_results`` — each circuit is queried
  independently and the per-circuit results are reduced afterwards, never by
  dumping every ``circuit_state.json`` into one context (plan §4.7).

This is the bounded Plan-and-Execute + bounded-recovery shape the plan
prescribes instead of an open ReAct loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.circuit.query_context import CircuitIntent, CircuitQueryPlan, CircuitScope, ResolvedEntity
from src.circuit.query_engine import CircuitQueryEngine


# Bounded-agent budgets (plan §6.2). Simple queries stay cheap; complex
# multi-step analyses (signal tracing, power distribution) get more rope but
# are still capped so the agent can never run away.
_SIMPLE_BUDGET = {"max_steps": 3, "max_tool_calls": 6, "max_recovery_rounds": 1}
_COMPLEX_BUDGET = {"max_steps": 5, "max_tool_calls": 10, "max_recovery_rounds": 2}

_COMPLEX_INTENTS = {"trace_signal_path", "power_distribution", "power_topology", "find_related_modules"}

# Engine methods the executor may dispatch to. Whitelisting keeps the bounded
# agent from reaching into mutating / unrelated APIs.
_ALLOWED_TOOLS = {
    "list_modules", "list_circuits", "get_circuit_overview",
    "get_module_detail", "get_module_instances", "get_module_interfaces",
    "get_module_power_nets", "search_module_power_nets",
    "get_instance_detail", "get_instance_connections",
    "get_net_detail", "get_net_connections", "search_nets",
    "find_connected_modules", "get_power_distribution_tree", "build_power_topology",
    "trace_signal_path", "get_module_pdf_region", "get_module_screenshot",
    "get_cross_reference_status", "aggregate_circuit_results",
    "search_entity_across_circuits",
}

# Intents that make sense as a cross-circuit Map-Reduce. Entity-specific
# intents (module_detail, instance_connections, …) are single-circuit only;
# an all_circuits scope for them is handled upstream by CircuitScopeResolver.
_MAP_REDUCIBLE = {"list_modules", "circuit_overview", "power_distribution", "power_topology"}


@dataclass
class ExecutorResult:
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    # ready | not_found | truncated | needs_clarification
    status: str = "ready"
    missing_info: list[str] = field(default_factory=list)
    circuit_results: list[dict[str, Any]] = field(default_factory=list)


class QueryPlanner:
    """Turn parsed intent + scope into a bounded execution plan."""

    def plan(
        self,
        question: str,
        kb_name: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        resolved_entities: list[ResolvedEntity] | None = None,
    ) -> CircuitQueryPlan:
        plan = CircuitQueryPlan(
            question=question,
            kb_name=kb_name,
            operation=intent.intent,
            intent=intent,
            scope=scope,
            resolved_entities=list(resolved_entities or []),
        )
        if scope.scope_type in {"all_circuits", "multiple_circuits"} and intent.intent in _MAP_REDUCIBLE:
            self._fill_map_reduce(plan, intent)
        else:
            self._fill_single(plan, intent, resolved_entities or [])

        budget = _COMPLEX_BUDGET if intent.intent in _COMPLEX_INTENTS else _SIMPLE_BUDGET
        plan.complexity = "complex" if intent.intent in _COMPLEX_INTENTS else "simple"
        plan.max_steps = budget["max_steps"]
        plan.max_tool_calls = budget["max_tool_calls"]
        plan.max_recovery_rounds = budget["max_recovery_rounds"]
        return plan

    # ── single-circuit plans ─────────────────────────────────────────────

    def _fill_single(self, plan: CircuitQueryPlan, intent: CircuitIntent, entities: list[ResolvedEntity]) -> None:
        circuit_id = plan.scope.circuit_ids[0] if plan.scope.circuit_ids else None
        kb = plan.kb_name
        module_id = _entity_id(entities, "module")
        refdes = _entity_id(entities, "instance")
        net_name = intent.entity_text
        op = intent.intent

        if op == "list_modules":
            plan.steps = [{"tool": "list_modules", "args": {"kb_name": kb, "design_id": circuit_id}, "as": "modules"}]
            plan.answer_key = "modules"
        elif op == "circuit_overview":
            plan.steps = [{"tool": "get_circuit_overview", "args": {"kb_name": kb, "design_id": circuit_id}, "as": "overview"}]
            plan.answer_key = "overview"
        elif op == "module_detail":
            plan.steps = [{"tool": "get_module_detail", "args": {"kb_name": kb, "design_id": circuit_id, "module_id_or_name": module_id}, "as": "module"}]
            plan.answer_key = "module"
        elif op == "module_instances":
            plan.steps = [{"tool": "get_module_instances", "args": {"kb_name": kb, "design_id": circuit_id, "module_id_or_name": module_id}, "as": "instances"}]
            plan.answer_key = "instances"
        elif op == "module_interfaces":
            plan.steps = [{"tool": "get_module_interfaces", "args": {"kb_name": kb, "design_id": circuit_id, "module_id_or_name": module_id}, "as": "interfaces"}]
            plan.answer_key = "interfaces"
        elif op == "instance_connections":
            plan.steps = [{"tool": "get_instance_connections", "args": {"kb_name": kb, "design_id": circuit_id, "refdes": refdes}, "as": "instance"}]
            plan.answer_key = "instance"
        elif op == "instance_detail":
            plan.steps = [{"tool": "get_instance_detail", "args": {"kb_name": kb, "design_id": circuit_id, "refdes": refdes}, "as": "instance"}]
            plan.answer_key = "instance"
        elif op in {"net_connections", "net_detail"}:
            tool = "get_net_connections" if op == "net_connections" else "get_net_detail"
            plan.steps = [{"tool": tool, "args": {"kb_name": kb, "design_id": circuit_id, "net_name": net_name}, "as": "net"}]
            plan.answer_key = "net"
        elif op == "power_distribution":
            # Net-scoped ("5V 电源经过哪些模块") wins over module-scoped: the net
            # is the query subject, not a module to resolve. EntityResolver may
            # spuriously match the net name to a module (e.g. "VCC" → Power), so
            # the net branch must be checked before module_id.
            if intent.target_entity_type == "circuit":
                plan.steps = [{"tool": "get_power_distribution_tree", "args": {"kb_name": kb, "design_id": circuit_id}, "as": "power_tree"}]
                plan.answer_key = "power_tree"
            elif module_id:
                plan.steps = [{"tool": "get_module_power_nets", "args": {"kb_name": kb, "design_id": circuit_id, "module_id_or_name": module_id}, "as": "power"}]
                plan.answer_key = "power"
            else:
                plan.steps = [{"tool": "search_module_power_nets", "args": {"kb_name": kb, "query": plan.question}, "as": "power_rows"}]
                plan.answer_key = "power_rows"
        elif op == "power_topology":
            plan.steps = [{"tool": "build_power_topology", "args": {"kb_name": kb, "design_id": circuit_id}, "as": "power_topology"}]
            plan.answer_key = "power_topology"
        elif op == "trace_signal_path":
            plan.steps = [{"tool": "trace_signal_path", "args": {"kb_name": kb, "design_id": circuit_id, "from_entity": intent.from_entity, "to_entity": intent.to_entity}, "as": "path"}]
            plan.answer_key = "path"
        elif op == "find_related_modules":
            plan.steps = [{"tool": "find_connected_modules", "args": {"kb_name": kb, "design_id": circuit_id, "module_id_or_name": module_id}, "as": "related"}]
            plan.answer_key = "related"
        elif op == "pdf_location":
            plan.steps = [{"tool": "get_module_pdf_region", "args": {"kb_name": kb, "design_id": circuit_id, "module_id_or_name": module_id}, "as": "region"}]
            plan.answer_key = "region"
        elif op == "cross_reference_status":
            plan.steps = [{"tool": "get_cross_reference_status", "args": {"kb_name": kb, "design_id": circuit_id}, "as": "xref"}]
            plan.answer_key = "xref"
        elif op == "list_circuits":
            plan.steps = [{"tool": "list_circuits", "args": {"kb_name": kb}, "as": "circuits"}]
            plan.answer_key = "circuits"
        elif op == "entity_search":
            # Cross-circuit discovery ("哪个 EDF 包含 CAN"). search_entity_across_circuits
            # already iterates every circuit in kb_name and ranks by plan §4.7, so
            # this is a single step — no Map-Reduce — grouped by the formatter.
            plan.steps = [{"tool": "search_entity_across_circuits", "args": {"kb_name": kb, "entity_query": intent.entity_text}, "as": "entity_hits"}]
            plan.answer_key = "entity_hits"
        else:
            plan.status = "unsupported"

    # ── cross-circuit Map-Reduce plans ────────────────────────────────────

    def _fill_map_reduce(self, plan: CircuitQueryPlan, intent: CircuitIntent) -> None:
        op = intent.intent
        kb = plan.kb_name
        if op == "list_modules":
            plan.map_step = {"tool": "list_modules", "args": {"kb_name": kb, "design_id": "$each_circuit"}, "as": "result"}
        elif op == "circuit_overview":
            plan.map_step = {"tool": "get_circuit_overview", "args": {"kb_name": kb, "design_id": "$each_circuit"}, "as": "result"}
        elif op == "power_distribution":
            plan.map_step = {"tool": "search_module_power_nets", "args": {"kb_name": kb, "query": plan.question}, "as": "result"}
        elif op == "power_topology":
            plan.map_step = {"tool": "build_power_topology", "args": {"kb_name": kb, "design_id": "$each_circuit"}, "as": "result"}
        plan.reduce_strategy = "group_by_circuit"
        plan.answer_key = "grouped"


class BoundedPlanExecutor:
    """Run a ``CircuitQueryPlan`` against the engine within its budgets."""

    def __init__(self, engine: CircuitQueryEngine | None = None, recovery_manager=None):
        self.engine = engine or CircuitQueryEngine()
        self.recovery_manager = recovery_manager

    def execute(self, plan: CircuitQueryPlan) -> ExecutorResult:
        result = ExecutorResult()
        data: dict[str, Any] = dict(plan.parameters or {})
        data["kb_name"] = plan.kb_name
        data["question"] = plan.question
        data.setdefault("from_entity", plan.intent.from_entity)
        data.setdefault("to_entity", plan.intent.to_entity)

        if plan.map_step is not None:
            self._run_map_reduce(plan, data, result)
        else:
            self._run_steps(plan, data, result)
            if plan.answer_key == "planned_results":
                data["planned_results"] = _planned_results(plan, data)

        result.data = data
        if result.status == "ready":
            self._finalize_status(plan, data, result)
        return result

    # ── step execution ────────────────────────────────────────────────────

    def _run_steps(self, plan: CircuitQueryPlan, data: dict[str, Any], result: ExecutorResult) -> None:
        for step in plan.steps:
            if result.tool_calls >= plan.max_tool_calls:
                result.status = "truncated"
                result.missing_info.append("达到工具调用上限，已停止检索。")
                break
            self._run_step(step, data, result)

    def _run_step(self, step: dict[str, Any], data: dict[str, Any], result: ExecutorResult) -> Any:
        tool_name = step.get("tool")
        if tool_name not in _ALLOWED_TOOLS:
            result.missing_info.append(f"工具 `{tool_name}` 不在允许列表内。")
            return None
        method = getattr(self.engine, tool_name, None)
        if method is None or not callable(method):
            result.missing_info.append(f"工具 `{tool_name}` 不可用。")
            return None
        args = _resolve_args(step.get("args", {}), data)
        try:
            output = method(**args)
        except Exception as exc:  # bounded agent: never propagate tool errors
            from src.core.logger import warn

            result.missing_info.append(f"`{tool_name}` 执行失败：{exc}")
            warn(f"tool '{tool_name}' execution failed: {exc}")
            return None
        result.tool_calls += 1
        if step.get("as"):
            data[step["as"]] = output
        if step.get("select"):
            self._apply_select(step["select"], output, data)
        if output is not None:
            result.evidence.extend(_evidence_for(output, step))
        return output

    def _apply_select(self, select: dict[str, Any], output: Any, data: dict[str, Any]) -> None:
        src = data.get(select["from"]) if select.get("from") else output
        chosen: Any = src
        if isinstance(src, list):
            chosen = src[0] if src else None
        field = select.get("field")
        if field and isinstance(chosen, dict):
            chosen = chosen.get(field)
        if select.get("bind"):
            data[select["bind"]] = chosen

    # ── Map-Reduce ────────────────────────────────────────────────────────

    def _run_map_reduce(self, plan: CircuitQueryPlan, data: dict[str, Any], result: ExecutorResult) -> None:
        step = plan.map_step or {}
        circuit_results: list[dict[str, Any]] = []
        for circuit_id in plan.scope.circuit_ids:
            if result.tool_calls >= plan.max_tool_calls:
                result.status = "truncated"
                result.missing_info.append("达到工具调用上限，跨 circuit 检索已截断。")
                break
            local = dict(data)
            local["each_circuit"] = circuit_id
            local["circuit_id"] = circuit_id
            output = self._run_step(step, local, result)
            if output:
                circuit_results.append(output)
        data["circuit_results"] = circuit_results
        grouped = self.engine.aggregate_circuit_results(plan.kb_name, circuit_results, plan.reduce_strategy)
        result.tool_calls += 1
        data["grouped"] = grouped
        if not circuit_results:
            result.status = "not_found"

    # ── status finalization ──────────────────────────────────────────────

    def _finalize_status(self, plan: CircuitQueryPlan, data: dict[str, Any], result: ExecutorResult) -> None:
        if plan.answer_key == "grouped":
            grouped = data.get("grouped") or {}
            if not grouped.get("circuit_count") and not grouped.get("grouped"):
                result.status = "not_found"
            return
        payload = data.get(plan.answer_key) if plan.answer_key else None
        if _is_empty(payload):
            result.status = "not_found"


# ── module-level helpers ──────────────────────────────────────────────────


def _entity_id(entities: list[ResolvedEntity], entity_type: str) -> str | None:
    for entity in entities or []:
        if entity.entity_type == entity_type:
            return entity.entity_id
    return None


def _resolve_args(args: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    return {key: _resolve_value(value, data) for key, value in (args or {}).items()}


def _resolve_value(value: Any, data: dict[str, Any]) -> Any:
    # "$var" resolves to a prior step's bound value; "$$" escapes a literal
    # leading dollar sign. Anything else is passed through verbatim.
    if isinstance(value, str) and value.startswith("$$"):
        return value[1:]
    if isinstance(value, str) and value.startswith("$"):
        return data.get(value[1:])
    return value


def _evidence_for(output: Any, step: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(output, dict):
        return []
    return [
        {
            "source_type": step.get("source_type", "edf_netlist"),
            "tool": step.get("tool"),
            "circuit_id": output.get("circuit_id") or output.get("design_id"),
            "source_files": output.get("source_files", []),
            "entity_type": step.get("evidence_type"),
            "entity_id": step.get("evidence_id")
            or output.get("module_id")
            or output.get("refdes")
            or output.get("net_name"),
            "confidence": float(output.get("confidence", 1.0) or 1.0),
        }
    ]


def _is_empty(payload: Any) -> bool:
    if payload is None:
        return True
    if isinstance(payload, (list, dict, str)) and not payload:
        return True
    return False


def _planned_results(plan: CircuitQueryPlan, data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in plan.steps:
        as_key = step.get("as")
        if not as_key:
            continue
        rows.append(
            {
                "tool": step.get("tool"),
                "purpose": step.get("purpose") or step.get("tool"),
                "key": as_key,
                "result": data.get(as_key),
            }
        )
    return rows
