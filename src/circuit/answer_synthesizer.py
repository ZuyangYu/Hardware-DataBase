from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import config.settings
from src.core.logger import warn
from src.circuit.query_context import CircuitIntent, CircuitScope, CircuitToolResponse


_COUNT_TERMS = ("多少", "几个", "数量", "count", "how many")
_LIST_TERMS = ("哪些", "列表", "名称", "列出", "list", "what")
_LOCATION_TERMS = ("哪里", "在哪", "第几页", "位置", "where", "page")
_CONNECTION_TERMS = ("连接", "连到", "接到", "经过", "path", "connect")
_POWER_SCOPE_TERMS = ("电源域", "电源树", "电源拓扑", "供电域", "供电树", "拓扑图", "power domain", "power tree", "power topology")
_VOLTAGE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_./+-])(?:[+]?\d+(?:[._]?\d+)?V\d*|VCC\d*V\d*|VDD\d*V\d*)(?![A-Za-z0-9_./+-])", re.I)


@dataclass
class AnswerValidation:
    is_satisfied: bool
    reason: str = ""
    missing_fields: list[str] = field(default_factory=list)
    needs_more_data: bool = False
    needs_resynthesis: bool = False


def _resolve_llm():
    try:
        from llama_index.core import Settings
    except Exception:
        return None
    return getattr(Settings, "_llm", None)


_LLM_UNBOUND_WARNED = False


def _call_llm(prompt: str, timeout: float) -> str | None:
    global _LLM_UNBOUND_WARNED
    llm = _resolve_llm()
    if llm is None:
        # This is the silent-failure mode: every circuit answer synthesis (and
        # thus every mermaid/topology rewrite) is skipped with no trace. Warn
        # once per unbound stretch so the regression surfaces in the logs.
        if not _LLM_UNBOUND_WARNED:
            _LLM_UNBOUND_WARNED = True
            warn(
                "CircuitAnswerSynthesizer: Settings._llm is not bound — LLM "
                "synthesis skipped. Ensure init_generation_model() runs before "
                "querying (query_circuit_data binds it on entry)."
            )
        return None
    _LLM_UNBOUND_WARNED = False  # reset so a later regression warns again
    try:
        result = llm.complete(prompt, timeout=timeout)
    except TypeError:
        try:
            result = llm.complete(prompt)
        except Exception as exc:
            warn(f"CircuitAnswerSynthesizer: LLM call failed ({exc})")
            return None
    except Exception as exc:
        warn(f"CircuitAnswerSynthesizer: LLM call failed ({exc})")
        return None
    text = getattr(result, "text", str(result)).strip()
    return text or None


def _clip_json(payload: Any, limit: int = 6000) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        text = str(payload)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _parse_validation(text: str | None) -> AnswerValidation | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    missing = data.get("missing_fields") or []
    if not isinstance(missing, list):
        missing = []
    return AnswerValidation(
        is_satisfied=bool(data.get("is_satisfied")),
        reason=str(data.get("reason") or ""),
        missing_fields=[str(item) for item in missing],
        needs_more_data=bool(data.get("needs_more_data")),
        needs_resynthesis=bool(data.get("needs_resynthesis")),
    )


_VALIDATION_PROMPT = """你是硬件电路问答质量检查器。请判断答案是否满足用户问题。

只依据给出的结构化数据和答案判断，不要补充新事实。输出严格 JSON：
{{"is_satisfied": true|false, "reason": "...", "missing_fields": ["..."], "needs_more_data": true|false, "needs_resynthesis": true|false}}

用户问题：
{question}

标准 intent：
{intent}

required_fields：
{required_fields}

当前答案：
{answer}

结构化数据：
{data}
"""


_SYNTHESIS_PROMPT = """你是硬件电路领域答案合成器。请严格基于结构化 facts 回答用户问题。

规则：
- 不得添加 facts 中没有的模块、元件、网络、页码或连接关系。
- 如果 facts 不足，必须明确说明缺失点。
- 按用户问题和 agent_plan 选择合适格式：数量/列表/表格/分组/简短解释/Mermaid 拓扑图。
- 如果 facts.response_policy 存在，优先遵循其中的 output_format 和 verbosity。
- 如果 agent_plan.output_format=mermaid 或用户要求拓扑图/关系图，优先输出 Mermaid 代码块；图后只给简短依据、关键转换关系和不确定项，不展开内部字段解释或长负载清单。
- 当回答电源树/供电拓扑/电源转换拓扑图时，Mermaid 边只能来自 facts 中的 conversion_edges。
- 不得根据 input_nets、output_nets、root_nets、rails、produced_by 或 consumed_by 自行构造新增拓扑边。
- produced_by/consumed_by 只能作为电源轨说明字段引用，不能替代 conversion_edges 生成 Mermaid 连接。
- 如果某网络没有 conversion_edges 连接，只能说明“缺少明确转换边/未纳入拓扑”，不能画成直连。
- inferred_edges 只能作为“疑似转换路径/候选路径”简短列出，默认不要画入主 Mermaid 图。
- 如果 agent_plan.output_format=table，输出 Markdown 表格。
- 如果 agent_plan.output_format=json，输出合法 JSON，不要包 markdown 代码块。
- 保留关键来源标识，例如 circuit_id、source_file、模块名、网络名。
- 你可以重组、归纳、去重和排序 facts，但不能补充 facts 中不存在的连接。

用户问题：
{question}

intent：
{intent}

answer_mode：
{answer_mode}

facts：
{facts}

当前模板答案：
{template_answer}
"""


class CircuitAnswerSynthesizer:
    """Fact-grounded answer synthesis plus answer validation.

    The deterministic formatter remains the first pass. This layer checks
    whether the answer covers the user's requested fields and can optionally
    ask the configured LLM to rewrite only from the already-retrieved facts.
    """

    def __init__(
        self,
        llm_enabled: bool | None = None,
        validation_llm_enabled: bool | None = None,
        timeout_seconds: float | None = None,
    ):
        self.llm_enabled = bool(
            getattr(config.settings, "CIRCUIT_SYNTHESIS_LLM_ENABLED", True)
            if llm_enabled is None else llm_enabled
        )
        self.validation_llm_enabled = bool(
            getattr(config.settings, "CIRCUIT_ANSWER_VALIDATION_LLM_ENABLED", True)
            if validation_llm_enabled is None else validation_llm_enabled
        )
        self.timeout_seconds = float(
            getattr(config.settings, "CIRCUIT_SYNTHESIS_LLM_TIMEOUT", 300)
            if timeout_seconds is None else timeout_seconds
        )

    def validate(
        self,
        question: str,
        intent: CircuitIntent,
        response: CircuitToolResponse,
    ) -> AnswerValidation:
        deterministic = self._deterministic_validate(question, intent, response)
        if not deterministic.is_satisfied:
            return deterministic
        if not self.validation_llm_enabled:
            return deterministic
        text = _call_llm(
            _VALIDATION_PROMPT.format(
                question=question,
                intent=intent.intent,
                required_fields=", ".join(intent.required_fields or []),
                answer=response.answer,
                data=_clip_json(response.data),
            ),
            self.timeout_seconds,
        )
        llm_result = _parse_validation(text)
        return llm_result or deterministic

    def synthesize(
        self,
        question: str,
        intent: CircuitIntent,
        scope: CircuitScope,
        response: CircuitToolResponse,
        force_llm: bool = False,
    ) -> CircuitToolResponse:
        if response.answer_mode not in {"direct_answer", "grouped_by_circuit", "partial_answer"}:
            return response
        if not (self.llm_enabled or force_llm):
            return response
        if not (force_llm or self._should_llm_synthesize(question, intent, response)):
            return response
        text = _call_llm(
            _SYNTHESIS_PROMPT.format(
                question=question,
                intent=intent.intent,
                answer_mode=response.answer_mode,
                facts=_clip_json(response.data, limit=12000),
                template_answer=response.answer,
            ),
            self.timeout_seconds,
        )
        if not text:
            return response
        text = text.strip()
        if intent.intent == "power_topology" and not _power_topology_mermaid_edges_are_grounded(text, response.data):
            response.data.setdefault("synthesis", {})
            response.data["synthesis"].update(
                {
                    "mode": "rule_template",
                    "rejected_mode": "llm_fact_grounded",
                    "reject_reason": "mermaid_edges_not_in_conversion_edges",
                    "scope": scope.scope_type,
                }
            )
            return response
        response.answer = text
        response.data.setdefault("synthesis", {})
        response.data["synthesis"].update({"mode": "llm_fact_grounded", "scope": scope.scope_type})
        return response

    def _deterministic_validate(
        self,
        question: str,
        intent: CircuitIntent,
        response: CircuitToolResponse,
    ) -> AnswerValidation:
        if response.answer_mode in {"needs_clarification", "unsupported"}:
            return AnswerValidation(True, "clarification_or_unsupported")
        if not response.answer.strip():
            return AnswerValidation(False, "答案为空。", ["answer"], needs_resynthesis=True)
        if response.answer_mode == "partial_answer":
            return AnswerValidation(True, "partial answer explicitly reports missing data")

        missing: list[str] = []
        answer_lower = response.answer.lower()
        question_lower = question.lower()

        if any(term in question_lower for term in _COUNT_TERMS):
            if not re.search(r"\d+", response.answer):
                missing.append("count")
        if intent.intent == "list_modules":
            if any(term in question_lower for term in _POWER_SCOPE_TERMS) or _VOLTAGE_TOKEN_RE.search(question):
                missing.append("power_domain_filter")
            result = response.data.get("result") or {}
            grouped = response.data.get("grouped") or {}
            modules = result.get("modules") or []
            if grouped:
                modules = [
                    module
                    for group in grouped.get("grouped", [])
                    for module in ((group.get("result") or {}).get("modules") or [])
                ]
            names = [m.get("name") or m.get("module_id") for m in modules if isinstance(m, dict)]
            if any(term in question_lower for term in _LIST_TERMS) and names:
                absent = [name for name in names if name and name.lower() not in answer_lower]
                if absent:
                    missing.append("module_names")
        if intent.intent in {"instance_connections", "net_connections", "trace_signal_path", "power_topology"}:
            if any(term in question_lower for term in _CONNECTION_TERMS) and not any(token in response.answer for token in ("→", "连接", "网络", "路径")):
                missing.append("connections")
        if intent.intent == "pdf_location":
            if any(term in question_lower for term in _LOCATION_TERMS) and "页" not in response.answer and "page" not in answer_lower:
                missing.append("page_number")

        if missing:
            return AnswerValidation(
                False,
                "答案缺少用户问题要求的字段。",
                missing,
                needs_more_data=True,
                needs_resynthesis=True,
            )
        return AnswerValidation(True, "deterministic validation passed")

    @staticmethod
    def _should_llm_synthesize(question: str, intent: CircuitIntent, response: CircuitToolResponse) -> bool:
        lowered = question.lower()
        agent_plan = response.data.get("agent_plan") if isinstance(response.data, dict) else None
        if isinstance(agent_plan, dict) and agent_plan.get("planner") == "llm_controlled":
            return True
        response_policy = response.data.get("response_policy") if isinstance(response.data, dict) else None
        if isinstance(response_policy, dict) and response_policy.get("mode") in {"tool_then_summarize", "agent_plan"}:
            return True
        if any(term in lowered for term in ("总结", "分析", "为什么", "对比", "综合", "summary", "analyze", "compare", "why")):
            return True
        if any(term in lowered for term in ("拓扑图", "关系图", "画出", "绘制", "mermaid", "diagram", "topology")):
            return True
        if response.answer_mode == "grouped_by_circuit" and len(response.answer) > 1200:
            return True
        return intent.intent in {"circuit_overview", "power_distribution", "power_topology", "trace_signal_path", "entity_search"}


def _power_topology_mermaid_edges_are_grounded(answer: str, data: Any) -> bool:
    """Reject synthesized power-topology diagrams that invent conversion edges."""
    if "-->" not in answer and "-.->" not in answer:
        return True
    allowed = _allowed_power_topology_edge_keys(data)
    rendered = _extract_mermaid_edge_keys(answer)
    if not rendered:
        return True
    if not allowed:
        return False
    return rendered.issubset(allowed)


def _allowed_power_topology_edge_keys(data: Any) -> set[tuple[str, str]]:
    allowed: set[tuple[str, str]] = set()
    for topology in _iter_power_topology_payloads(data):
        for edge in (topology.get("conversion_edges") or []) + (topology.get("inferred_edges") or []):
            source = edge.get("from_net") or "UNKNOWN_INPUT"
            target = edge.get("to_net") or "UNKNOWN_OUTPUT"
            source_keys = _endpoint_aliases(source)
            target_keys = _endpoint_aliases(target)
            for source_key in source_keys:
                for target_key in target_keys:
                    allowed.add((source_key, target_key))
    return allowed


def _iter_power_topology_payloads(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    payloads: list[dict[str, Any]] = []
    for key in ("topology", "result"):
        value = data.get(key)
        if isinstance(value, dict) and "conversion_edges" in value:
            payloads.append(value)
    grouped = data.get("grouped")
    if isinstance(grouped, dict):
        for row in grouped.get("grouped") or []:
            result = row.get("result") if isinstance(row, dict) else None
            if isinstance(result, dict) and "conversion_edges" in result:
                payloads.append(result)
    for item in data.get("planned_results") or []:
        result = item.get("result") if isinstance(item, dict) else None
        if isinstance(result, dict) and "conversion_edges" in result:
            payloads.append(result)
    return payloads


def _extract_mermaid_edge_keys(answer: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for line in answer.splitlines():
        delimiter = "-.->" if "-.->" in line else "-->" if "-->" in line else None
        if delimiter is None:
            continue
        left, right = line.split(delimiter, 1)
        source = _normalize_mermaid_endpoint(left)
        target = _normalize_mermaid_endpoint(right)
        if source and target:
            edges.add((source, target))
    return edges


def _normalize_mermaid_endpoint(value: str) -> str:
    text = value.strip()
    if text.startswith("|"):
        parts = text.split("|", 2)
        text = parts[2] if len(parts) == 3 else ""
    match = re.search(r"[A-Za-z0-9_]+", text)
    return match.group(0).lower() if match else ""


def _endpoint_aliases(value: str) -> set[str]:
    raw = str(value or "").strip()
    aliases = {raw.lower(), _mermaid_node_id(raw).lower()}
    return {alias for alias in aliases if alias}


def _mermaid_node_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "node")).strip("_")
    if not text:
        text = "node"
    if text[0].isdigit():
        text = "N_" + text
    return text
