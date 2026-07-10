from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable

import config.settings
from src.core.logger import warn
from src.circuit.query_context import CircuitIntent, CircuitQueryPlan, CircuitScope, ResolvedEntity


_POLICY_PROMPT = """你是硬件原理图 EDF/PDF 电路查询 Agent 的响应策略选择器。

你不查询事实，也不回答用户问题。你只判断本次电路工具内部应该如何处理。

只输出 JSON，不要解释，不要 markdown：
{{
  "mode": "direct_tool_answer|tool_then_summarize|agent_plan",
  "reason": "...",
  "output_format": "markdown|table|list|json|mermaid",
  "verbosity": "brief|normal|detailed"
}}

模式定义：
- direct_tool_answer：单个确定性工具的模板答案已足够，例如“U3 连到哪里”“有哪些模块”。
- tool_then_summarize：单个规则工具能取到 facts，但用户希望整理、去重、分组、表格、结论或更自然表达。
- agent_plan：需要多步工具、拓扑图、路径追踪、跨实体关联、源头/负载分析、对比、综合判断。

约束：
- 你只能选择处理策略，不能编造任何电路事实。
- 如果用户要求 Mermaid/拓扑图/关系图，output_format 选 mermaid，通常 mode 选 agent_plan。
- 如果用户要求表格，output_format 选 table。
- 对普通明确查询，优先 direct_tool_answer。

用户问题：
{question}

规则解析 intent：
{intent}

规则计划：
{plan}

已解析实体：
{entities}

电路范围：
{scope}
"""


@dataclass(frozen=True)
class CircuitResponsePolicy:
    mode: str = "direct_tool_answer"
    reason: str = "rule default"
    output_format: str = "markdown"
    verbosity: str = "normal"
    source: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CircuitResponsePolicySelector:
    """Choose how the circuit-domain Agent should present retrieved facts.

    This selector is intentionally separate from the top-level router. The
    router decides whether a question belongs to the circuit tool; this class
    decides how the circuit tool should handle a question once routed here.
    """

    def __init__(
        self,
        enabled: bool | None = None,
        timeout_seconds: float | None = None,
        llm_resolver: Callable[[], Any] | None = None,
    ):
        self.enabled = bool(
            getattr(config.settings, "CIRCUIT_AGENT_POLICY_ENABLED", True)
            if enabled is None else enabled
        )
        self.timeout_seconds = float(
            getattr(config.settings, "CIRCUIT_AGENT_POLICY_TIMEOUT", 300)
            if timeout_seconds is None else timeout_seconds
        )
        self.llm_resolver = llm_resolver or _resolve_llm

    def select(
        self,
        question: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        resolved_entities: list[ResolvedEntity],
        rule_plan: CircuitQueryPlan,
    ) -> CircuitResponsePolicy:
        rule_policy = _rule_policy(question, intent, rule_plan)
        if not self.enabled:
            return rule_policy
        force_agent_plan = getattr(config.settings, "CIRCUIT_AGENT_MODE", "agentic") == "agentic"
        data = self._complete_json(
            _POLICY_PROMPT.format(
                question=question,
                intent=_clip_json(intent),
                plan=_clip_json(rule_plan),
                entities=_clip_json([entity.to_dict() for entity in resolved_entities]),
                scope=_clip_json(scope),
            )
        )
        if not data:
            return rule_policy
        selected_mode = _normalize_mode(data.get("mode"), fallback=rule_policy.mode)
        if force_agent_plan:
            selected_mode = "agent_plan"
        return CircuitResponsePolicy(
            mode=selected_mode,
            reason=str(data.get("reason") or rule_policy.reason),
            output_format=_normalize_output_format(data.get("output_format"), question, rule_policy.output_format),
            verbosity=_normalize_verbosity(data.get("verbosity"), question, rule_policy.verbosity),
            source="llm",
        )

    def _complete_json(self, prompt: str) -> dict[str, Any] | None:
        llm = self.llm_resolver()
        if llm is None:
            return None
        try:
            result = llm.complete(prompt, timeout=self.timeout_seconds)
        except TypeError:
            try:
                result = llm.complete(prompt)
            except Exception as exc:
                warn(f"CircuitResponsePolicySelector: LLM call failed ({exc})")
                return None
        except Exception as exc:
            warn(f"CircuitResponsePolicySelector: LLM call failed ({exc})")
            return None
        text = getattr(result, "text", str(result)).strip()
        return _parse_json_object(text)


def _resolve_llm():
    try:
        from llama_index.core import Settings
    except Exception:
        return None
    return getattr(Settings, "_llm", None)


def _clip_json(value: Any, limit: int = 2500) -> str:
    try:
        text = json.dumps(value.to_dict() if hasattr(value, "to_dict") else asdict(value) if hasattr(value, "__dataclass_fields__") else value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


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


def _rule_policy(question: str, intent: CircuitIntent, rule_plan: CircuitQueryPlan) -> CircuitResponsePolicy:
    output_format = _normalize_output_format(None, question, "markdown")
    verbosity = _normalize_verbosity(None, question, "normal")
    if getattr(config.settings, "CIRCUIT_AGENT_MODE", "agentic") == "agentic":
        return CircuitResponsePolicy("agent_plan", "agentic mode forces managed planning for every circuit query", output_format, verbosity, "rule")
    if rule_plan.status != "ready":
        return CircuitResponsePolicy("agent_plan", "rule plan is not ready", output_format, verbosity, "rule")
    if _looks_agentic(question) or _looks_multi_part(question):
        return CircuitResponsePolicy("agent_plan", "question asks for analysis/diagram/multi-step organization", output_format, verbosity, "rule")
    if _looks_summarize_only(question):
        return CircuitResponsePolicy("tool_then_summarize", "question asks to organize retrieved facts", output_format, verbosity, "rule")
    return CircuitResponsePolicy("direct_tool_answer", "single deterministic tool answer is sufficient", output_format, verbosity, "rule")


def _looks_agentic(question: str) -> bool:
    lowered = (question or "").lower()
    return any(token in lowered for token in ("拓扑图", "关系图", "画出", "绘制", "路径", "源头", "负载", "对比", "mermaid", "diagram", "topology", "trace"))


def _looks_multi_part(question: str) -> bool:
    lowered = (question or "").lower()
    markers = ("同时", "并且", "以及", "另外", "一起", "分别", " and ", " also ")
    if any(marker in lowered for marker in markers):
        return True
    return sum(token in lowered for token in ("电压", "连接", "接口", "位置", "哪一页", "详情", "元件", "网络")) >= 2


def _looks_summarize_only(question: str) -> bool:
    lowered = (question or "").lower()
    return any(token in lowered for token in ("整理", "总结", "分析", "表格", "列表", "按", "输出", "json", "table", "summary", "summarize", "analyze"))


def _normalize_mode(value: Any, fallback: str = "direct_tool_answer") -> str:
    text = str(value or "").strip()
    if text in {"direct_tool_answer", "tool_then_summarize", "agent_plan"}:
        return text
    return fallback


def _normalize_output_format(value: Any, question: str, fallback: str = "markdown") -> str:
    text = str(value or "").strip().lower()
    if text in {"markdown", "table", "list", "json", "mermaid"}:
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
    return fallback


def _normalize_verbosity(value: Any, question: str, fallback: str = "normal") -> str:
    text = str(value or "").strip().lower()
    if text in {"brief", "normal", "detailed"}:
        return text
    lowered = (question or "").lower()
    if any(token in lowered for token in ("详细", "完整", "全部", "detailed", "full")):
        return "detailed"
    if any(token in lowered for token in ("简要", "简短", "brief")):
        return "brief"
    return fallback
