from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import config.settings
from src.core.logger import warn
from src.circuit.query_context import CircuitIntent, CircuitQueryPlan, CircuitScope, ResolvedEntity


_SUPPORTED_INTENTS = {
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

_LOGICAL_TOOLS = {
    "list_modules",
    "get_circuit_overview",
    "get_module_detail",
    "get_module_instances",
    "get_module_interfaces",
    "get_module_power_nets",
    "get_instance_detail",
    "get_instance_connections",
    "get_net_detail",
    "get_net_connections",
    "find_connected_modules",
    "get_power_distribution_tree",
    "build_power_topology",
    "trace_signal_path",
    "get_module_pdf_region",
    "get_cross_reference_status",
    "search_entity_across_circuits",
}

_INTENT_PROMPT = """你是硬件原理图 EDF/PDF 查询工具内部的受控意图解析器。

只输出 JSON，不要解释。intent 必须来自这个列表：
{intents}

输出格式：
{{
  "intent": "...",
  "target_entity_type": "circuit|module|instance|net|null",
  "entity_text": "用户提到的实体名，没有则 null",
  "required_fields": ["..."],
  "is_global_query": false,
  "is_single_entity_detail": true,
  "from_entity": null,
  "to_entity": null
}}

用户问题：
{question}
"""

_PLAN_PROMPT = """你是硬件原理图 EDF/PDF 查询工具内部的受控规划器。

你只能选择这些逻辑工具：
{tools}

不要生成 Python，不要生成任意文件路径，不要生成 SQL。只输出 JSON：
{{
  "use_llm_plan": true,
  "reason": "...",
  "goal": "用户真正想完成的电路分析目标",
  "output_format": "markdown|table|list|json|mermaid",
  "answer_style": "brief|normal|detailed",
  "intent": "multi_part_circuit_query",
  "steps": [
    {{"tool": "get_module_power_nets", "entity_type": "module", "purpose": "回答模块供电/输入电压"}},
    {{"tool": "get_module_pdf_region", "entity_type": "module", "purpose": "回答原理图位置"}}
  ]
}}

规则：
- 当用户要求拓扑图、表格、整理、分析、对比、总结、按某种格式输出，或问题包含多个需求，或规则 plan 明显只能覆盖部分问题时，use_llm_plan=true。
- build_power_topology 用于“电源树/供电拓扑/电源转换路径/输入输出电源轨/EN-PG 控制关系”。
- get_power_distribution_tree 只用于“某电源网络分布在哪些模块/页面/实例”，不要用它替代真实电源树。
- 如果单个规则计划足够回答，输出 {{"use_llm_plan": false, "reason": "rule plan is enough", "steps": []}}。
- 工具参数由系统注入，不能自己填写 kb_name、circuit_id、文件路径。
- 不要超过 {max_steps} 个步骤。
- 你只负责选择工具和表达目标，不要在 JSON 中编造电路事实。

用户问题：
{question}

规则 intent：{intent}
目标实体类型：{entity_type}
已解析实体：
{entities}
"""


@dataclass
class LLMPlanDecision:
    use_llm_plan: bool
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


_LLM_UNBOUND_WARNED = False


class LLMControlledPlanner:
    """LLM-assisted planner that only emits validated CircuitQueryPlan objects.

    The LLM never executes tools and never supplies raw file paths. It can only
    pick logical tools from a fixed allow-list; this class compiles them into
    deterministic ``CircuitQueryPlan`` steps with kb/circuit/entity args filled
    by trusted code.
    """

    def __init__(
        self,
        enabled: bool | None = None,
        timeout_seconds: float | None = None,
        llm_resolver: Callable[[], Any] | None = None,
    ):
        self.enabled = bool(
            getattr(config.settings, "CIRCUIT_LLM_PLANNER_ENABLED", True)
            if enabled is None else enabled
        )
        self.timeout_seconds = float(
            getattr(config.settings, "CIRCUIT_LLM_PLANNER_TIMEOUT", 300)
            if timeout_seconds is None else timeout_seconds
        )
        self.llm_resolver = llm_resolver or _resolve_llm

    def interpret_intent(self, question: str, fallback: CircuitIntent) -> CircuitIntent | None:
        if not self.enabled or fallback.intent != "unsupported_or_unclear":
            return None
        data = self._complete_json(
            _INTENT_PROMPT.format(question=question, intents=", ".join(sorted(_SUPPORTED_INTENTS)))
        )
        if not data:
            return None
        intent_name = str(data.get("intent") or "").strip()
        if intent_name not in _SUPPORTED_INTENTS or intent_name == "unsupported_or_unclear":
            return None
        required = data.get("required_fields") if isinstance(data.get("required_fields"), list) else []
        return CircuitIntent(
            intent=intent_name,
            target_entity_type=_none_if_null(data.get("target_entity_type")),
            entity_text=_none_if_null(data.get("entity_text")),
            required_fields=[str(item) for item in required],
            is_global_query=bool(data.get("is_global_query")),
            is_single_entity_detail=bool(data.get("is_single_entity_detail")),
            pre_intent=str(data.get("pre_intent") or "llm_controlled"),
            confidence=float(data.get("confidence") or 0.65),
            from_entity=_none_if_null(data.get("from_entity")),
            to_entity=_none_if_null(data.get("to_entity")),
            planner_source="llm_controlled",
        )

    def plan(
        self,
        question: str,
        kb_name: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        resolved_entities: list[ResolvedEntity],
        rule_plan: CircuitQueryPlan,
        force: bool = False,
    ) -> tuple[CircuitQueryPlan | None, LLMPlanDecision]:
        if not self.enabled or scope.scope_type != "single_circuit":
            return None, LLMPlanDecision(False, "disabled_or_non_single_scope")
        if not force and not _needs_agent_planning(question, intent, rule_plan):
            return None, LLMPlanDecision(False, "rule plan is enough")

        data = self._complete_json(
            _PLAN_PROMPT.format(
                question=question,
                intent=intent.intent,
                entity_type=intent.target_entity_type,
                entities=json.dumps([entity.to_dict() for entity in resolved_entities], ensure_ascii=False),
                tools=", ".join(sorted(_LOGICAL_TOOLS)),
                max_steps=rule_plan.max_steps,
            )
        )
        if not data:
            if force:
                return self._compile_rule_fallback_plan(question, kb_name, intent, scope, resolved_entities, rule_plan), LLMPlanDecision(True, "forced agent plan fell back to rule tool plan")
            return None, LLMPlanDecision(False, "llm unavailable or invalid json")
        if not bool(data.get("use_llm_plan")):
            if force:
                return self._compile_rule_fallback_plan(question, kb_name, intent, scope, resolved_entities, rule_plan, data), LLMPlanDecision(True, str(data.get("reason") or "forced agent plan used rule tool plan"), data)
            return None, LLMPlanDecision(False, str(data.get("reason") or "llm declined"), data)

        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        compiled = self._compile_plan(question, kb_name, intent, scope, resolved_entities, rule_plan, steps, data)
        if compiled is None:
            if force:
                return self._compile_rule_fallback_plan(question, kb_name, intent, scope, resolved_entities, rule_plan, data), LLMPlanDecision(True, "forced agent plan failed validation; used rule tool plan", data)
            return None, LLMPlanDecision(False, "llm plan failed validation", data)
        return compiled, LLMPlanDecision(True, str(data.get("reason") or "llm controlled plan"), data)

    def _compile_rule_fallback_plan(
        self,
        question: str,
        kb_name: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        resolved_entities: list[ResolvedEntity],
        rule_plan: CircuitQueryPlan,
        raw_plan: dict[str, Any] | None = None,
    ) -> CircuitQueryPlan | None:
        if not rule_plan.steps:
            return None
        raw_plan = raw_plan or {}
        llm_intent = CircuitIntent(
            intent="multi_part_circuit_query",
            target_entity_type=intent.target_entity_type,
            entity_text=intent.entity_text,
            required_fields=list(intent.required_fields),
            is_global_query=intent.is_global_query,
            is_single_entity_detail=intent.is_single_entity_detail,
            pre_intent=intent.pre_intent,
            confidence=max(intent.confidence, 0.65),
            from_entity=intent.from_entity,
            to_entity=intent.to_entity,
            planner_source="agentic_rule_fallback",
        )
        plan = CircuitQueryPlan(question, kb_name, llm_intent.intent, llm_intent, scope, resolved_entities=list(resolved_entities))
        plan.steps = [dict(step, purpose=step.get("purpose") or step.get("tool") or f"step_{idx}") for idx, step in enumerate(rule_plan.steps, 1)]
        plan.answer_key = "planned_results"
        plan.complexity = rule_plan.complexity
        plan.max_steps = rule_plan.max_steps
        plan.max_tool_calls = rule_plan.max_tool_calls
        plan.max_recovery_rounds = rule_plan.max_recovery_rounds
        plan.stop_when = [step["as"] for step in plan.steps if step.get("as")]
        plan.parameters["planner"] = "agentic_rule_fallback"
        plan.parameters["source_intent"] = intent.intent
        plan.parameters["agent_goal"] = str(raw_plan.get("goal") or raw_plan.get("reason") or question)
        plan.parameters["output_format"] = _normalize_output_format(raw_plan.get("output_format"), question)
        plan.parameters["answer_style"] = _normalize_answer_style(raw_plan.get("answer_style"), question)
        return plan

    def _compile_plan(
        self,
        question: str,
        kb_name: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        resolved_entities: list[ResolvedEntity],
        rule_plan: CircuitQueryPlan,
        raw_steps: list[Any],
        raw_plan: dict[str, Any] | None = None,
    ) -> CircuitQueryPlan | None:
        circuit_id = scope.circuit_ids[0] if scope.circuit_ids else None
        if not circuit_id:
            return None
        steps: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_steps[: rule_plan.max_steps], 1):
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "").strip()
            if tool not in _LOGICAL_TOOLS or tool in seen:
                continue
            compiled = _compile_step(tool, kb_name, circuit_id, intent, resolved_entities, index, item)
            if compiled is None:
                continue
            steps.append(compiled)
            seen.add(tool)
        if not steps:
            return None

        llm_intent = CircuitIntent(
            intent="multi_part_circuit_query",
            target_entity_type=intent.target_entity_type,
            entity_text=intent.entity_text,
            required_fields=list(intent.required_fields),
            is_global_query=intent.is_global_query,
            is_single_entity_detail=intent.is_single_entity_detail,
            pre_intent=intent.pre_intent,
            confidence=max(intent.confidence, 0.65),
            from_entity=intent.from_entity,
            to_entity=intent.to_entity,
            planner_source="llm_controlled",
        )
        plan = CircuitQueryPlan(question, kb_name, llm_intent.intent, llm_intent, scope, resolved_entities=list(resolved_entities))
        plan.steps = steps
        plan.answer_key = "planned_results"
        plan.complexity = "complex" if len(steps) > 1 else rule_plan.complexity
        plan.max_steps = min(max(rule_plan.max_steps, len(steps)), 5)
        plan.max_tool_calls = min(max(rule_plan.max_tool_calls, len(steps)), 10)
        plan.max_recovery_rounds = rule_plan.max_recovery_rounds
        plan.stop_when = [step["as"] for step in steps if step.get("as")]
        plan.parameters["planner"] = "llm_controlled"
        plan.parameters["source_intent"] = intent.intent
        raw_plan = raw_plan or {}
        plan.parameters["agent_goal"] = str(raw_plan.get("goal") or raw_plan.get("reason") or question)
        plan.parameters["output_format"] = _normalize_output_format(raw_plan.get("output_format"), question)
        plan.parameters["answer_style"] = _normalize_answer_style(raw_plan.get("answer_style"), question)
        return plan

    def _complete_json(self, prompt: str) -> dict[str, Any] | None:
        global _LLM_UNBOUND_WARNED
        llm = self.llm_resolver()
        if llm is None:
            if not _LLM_UNBOUND_WARNED:
                _LLM_UNBOUND_WARNED = True
                warn(
                    "LLMControlledPlanner: Settings._llm is not bound — "
                    "agentic planning skipped; falling back to deterministic rule plan."
                )
            return None
        _LLM_UNBOUND_WARNED = False
        try:
            result = llm.complete(prompt, timeout=self.timeout_seconds)
        except TypeError:
            try:
                result = llm.complete(prompt)
            except Exception as exc:
                warn(f"LLMControlledPlanner: LLM call failed ({exc})")
                return None
        except Exception as exc:
            warn(f"LLMControlledPlanner: LLM call failed ({exc})")
            return None
        text = getattr(result, "text", str(result)).strip()
        return _parse_json_object(text)


def _resolve_llm():
    try:
        from llama_index.core import Settings
    except Exception:
        return None
    return getattr(Settings, "_llm", None)


def _parse_json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _none_if_null(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "nil"}:
        return None
    return text


def _looks_multi_part(question: str) -> bool:
    lowered = (question or "").lower()
    markers = ("同时", "并且", "以及", "另外", "一起", "分别", "和", "、", "，", ",", " and ", " also ")
    if any(marker in lowered for marker in markers):
        return True
    return sum(token in lowered for token in ("电压", "连接", "接口", "位置", "哪一页", "详情", "元件")) >= 2


def _looks_agent_output_request(question: str) -> bool:
    lowered = (question or "").lower()
    markers = (
        "拓扑图", "电源树", "关系图", "画出", "绘制", "整理", "总结", "分析", "对比",
        "表格", "列表", "按", "输出", "mermaid", "json", "table", "diagram", "topology",
        "summarize", "summary", "analyze", "compare",
    )
    return any(marker in lowered for marker in markers)


def _needs_agent_planning(question: str, intent: CircuitIntent, rule_plan: CircuitQueryPlan) -> bool:
    if rule_plan.status != "ready":
        return True
    if _looks_multi_part(question) or _looks_agent_output_request(question):
        return True
    if intent.intent in {"power_distribution", "power_topology", "trace_signal_path"} and _looks_agent_output_request(question):
        return True
    return False


def _normalize_output_format(value: Any, question: str) -> str:
    text = str(value or "").strip().lower()
    allowed = {"markdown", "table", "list", "json", "mermaid"}
    if text in allowed:
        return text
    lowered = (question or "").lower()
    if any(token in lowered for token in ("mermaid", "拓扑图", "关系图", "画出", "绘制", "diagram", "topology")):
        return "mermaid"
    if any(token in lowered for token in ("表格", "table")):
        return "table"
    if "json" in lowered:
        return "json"
    if any(token in lowered for token in ("列表", "list")):
        return "list"
    return "markdown"


def _normalize_answer_style(value: Any, question: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"brief", "normal", "detailed"}:
        return text
    lowered = (question or "").lower()
    if any(token in lowered for token in ("详细", "完整", "全部", "detailed", "full")):
        return "detailed"
    if any(token in lowered for token in ("简要", "简短", "brief")):
        return "brief"
    return "normal"


def _compile_step(
    tool: str,
    kb_name: str,
    circuit_id: str,
    intent: CircuitIntent,
    entities: list[ResolvedEntity],
    index: int,
    raw_step: dict[str, Any],
) -> dict[str, Any] | None:
    module_id = _entity_id(entities, "module") or (intent.entity_text if intent.target_entity_type == "module" else None)
    refdes = _entity_id(entities, "instance") or (intent.entity_text if intent.target_entity_type == "instance" else None)
    net_name = _entity_id(entities, "net") or (intent.entity_text if intent.target_entity_type == "net" else None)
    purpose = str(raw_step.get("purpose") or tool)
    as_key = f"planned_{index}_{tool}"

    if tool == "list_modules":
        return {"tool": tool, "args": {"kb_name": kb_name, "design_id": circuit_id}, "as": as_key, "purpose": purpose}
    if tool == "get_circuit_overview":
        return {"tool": tool, "args": {"kb_name": kb_name, "design_id": circuit_id}, "as": as_key, "purpose": purpose}
    if tool in {"get_module_detail", "get_module_instances", "get_module_interfaces", "get_module_power_nets", "find_connected_modules", "get_module_pdf_region"}:
        if not module_id:
            return None
        return {"tool": tool, "args": {"kb_name": kb_name, "design_id": circuit_id, "module_id_or_name": module_id}, "as": as_key, "purpose": purpose}
    if tool in {"get_instance_detail", "get_instance_connections"}:
        if not refdes:
            return None
        return {"tool": tool, "args": {"kb_name": kb_name, "design_id": circuit_id, "refdes": refdes}, "as": as_key, "purpose": purpose}
    if tool in {"get_net_detail", "get_net_connections"}:
        if not net_name:
            return None
        return {"tool": tool, "args": {"kb_name": kb_name, "design_id": circuit_id, "net_name": net_name}, "as": as_key, "purpose": purpose}
    if tool == "get_power_distribution_tree":
        return {"tool": tool, "args": {"kb_name": kb_name, "design_id": circuit_id}, "as": as_key, "purpose": purpose}
    if tool == "build_power_topology":
        return {"tool": tool, "args": {"kb_name": kb_name, "design_id": circuit_id}, "as": as_key, "purpose": purpose}
    if tool == "trace_signal_path":
        if not (intent.from_entity and intent.to_entity):
            return None
        return {
            "tool": tool,
            "args": {"kb_name": kb_name, "design_id": circuit_id, "from_entity": intent.from_entity, "to_entity": intent.to_entity},
            "as": as_key,
            "purpose": purpose,
        }
    if tool == "get_cross_reference_status":
        return {"tool": tool, "args": {"kb_name": kb_name, "design_id": circuit_id}, "as": as_key, "purpose": purpose}
    if tool == "search_entity_across_circuits":
        if not intent.entity_text:
            return None
        return {"tool": tool, "args": {"kb_name": kb_name, "entity_query": intent.entity_text}, "as": as_key, "purpose": purpose}
    return None


def _entity_id(entities: list[ResolvedEntity], entity_type: str) -> str | None:
    for entity in entities or []:
        if entity.entity_type == entity_type:
            return entity.entity_id
    return None
