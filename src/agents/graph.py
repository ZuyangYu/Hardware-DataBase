from __future__ import annotations

import re
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import config.settings
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

from src.agents.claim_evidence import Claim, ClaimCoverage, EvidenceCapability, plan_claims
from src.agents.answer_constraints import reportable_conflicts
from src.agents.query_tokens import tokenize_hardware_query
from src.agents.state import (
    AgentState,
    CoverageItem,
    CoverageMatrix,
    EvidenceQuality,
    QuestionAnalysis,
    RetrievalLedgerItem,
    SourcePlan,
    SourcePlanItem,
    SubQuestion,
    SufficiencyDecision,
    ToolCallPlan,
)
from src.circuit.question_analysis import analyze_question as analyze_circuit_question
from src.core.cancellation import QueryCancelled
from src.agents.query_tokens import _HARDWARE_TERMS
from src.ingestion.parser_registry import PARSER_REGISTRY
from src.observability import observe, submit_with_current_context
from src.observability.metrics import record_retrieval
from src.agents.prompts import (
    DIRECT_ANSWER_SYSTEM_PROMPT,
    PLAN_NEXT_RETRIEVAL_SYSTEM_PROMPT,
    QUERY_ROUTER_SYSTEM_PROMPT,
    SUFFICIENCY_JUDGE_SYSTEM_PROMPT,
)


def _write_stream_event(event: dict[str, Any]) -> None:
    if get_stream_writer is None:
        return
    try:
        get_stream_writer()(event)
    except RuntimeError:
        return


_STAGE_LABELS = {
    "route_query": "识别问题类型",
    "query_router": "识别问题类型",
    "compose_direct_answer": "生成直接回答",
    "analyze_question": "分析硬件问题",
    "question_analysis_agent": "分析硬件问题",
    "scan_kb_catalog": "读取数据目录",
    "plan_source_selection": "规划检索来源",
    "retrieval_planner_agent": "规划检索来源",
    "retrieve_evidence": "多源硬件数据召回",
    "merge_evidence": "整理证据",
    "score_and_compare_evidence": "评估证据覆盖",
    "draft_intermediate_answer": "生成中间草稿",
    "judge_sufficiency": "判断充分性",
    "plan_next_retrieval": "规划下一轮检索",
    "compose_answer": "生成回答",
    "verify_grounding": "校验来源",
}

_STAGE_KEYS = {
    "route_query": "route",
    "query_router": "route",
    "compose_direct_answer": "generate",
    "analyze_question": "analyze",
    "question_analysis_agent": "analyze",
    "scan_kb_catalog": "catalog",
    "plan_source_selection": "plan",
    "retrieval_planner_agent": "plan",
    "retrieve_evidence": "retrieve",
    "merge_evidence": "merge",
    "score_and_compare_evidence": "evaluate",
    "draft_intermediate_answer": "draft",
    "judge_sufficiency": "judge",
    "plan_next_retrieval": "plan",
    "compose_answer": "generate",
    "verify_grounding": "verify",
}

_STAGE_RUNNING_DETAILS = {
    "route_query": "正在判断是否需要检索知识库",
    "compose_direct_answer": "正在生成未挂载知识库的通用回答",
    "analyze_question": "正在拆解问题、硬件实体和子问题",
    "scan_kb_catalog": "正在读取当前知识库的数据目录",
    "plan_source_selection": "正在规划文档、表格和电路数据的检索来源",
    "retrieve_evidence": "正在并行召回相关硬件资料",
    "merge_evidence": "正在合并并去重多源证据",
    "score_and_compare_evidence": "正在评估证据覆盖度和冲突",
    "draft_intermediate_answer": "正在基于当前证据生成中间草稿",
    "judge_sufficiency": "正在判断证据是否足够回答",
    "plan_next_retrieval": "正在根据缺口规划下一轮检索",
    "compose_answer": "正在流式生成答案正文",
    "verify_grounding": "正在校验答案是否有来源支撑",
}


def _compact_stage_detail(message: str, metadata: dict[str, Any] | None = None) -> str:
    parts: list[str] = []
    if message:
        parts.append(str(message))
    compact: list[str] = []
    for key, value in (metadata or {}).items():
        if value in (None, "", [], {}, ()):
            continue
        if isinstance(value, bool):
            compact.append(f"{key}={'是' if value else '否'}")
        elif isinstance(value, (int, float)):
            compact.append(f"{key}={value}")
        elif isinstance(value, list):
            compact.append(f"{key}={len(value)}项")
        elif isinstance(value, dict):
            compact.append(f"{key}={len(value)}项")
        else:
            text = str(value).strip()
            if text:
                compact.append(f"{key}={text[:60]}{'...' if len(text) > 60 else ''}")
        if len(compact) >= 3:
            break
    if compact:
        parts.append("；".join(compact))
    return " · ".join(parts)


def _write_stage_event(node: str, status: str, detail: str = "") -> None:
    _write_stream_event(
        {
            "type": "stage",
            "key": _STAGE_KEYS.get(node, node),
            "label": _STAGE_LABELS.get(node, node),
            "status": status,
            "detail": detail or (_STAGE_RUNNING_DETAILS.get(node, "") if status == "running" else ""),
        }
    )


def _trace(state: AgentState, node: str, message: str, metadata: dict[str, Any] | None = None):
    trace = list(state.get("trace") or [])
    trace.append({"node": node, "message": message, "metadata": metadata or {}})
    detail = (
        "已完成问题范围分析"
        if node in {"analyze_question", "question_analysis_agent"}
        else _compact_stage_detail(message, metadata)
    )
    _write_stage_event(node, "done", detail)
    return trace


def _claims_for_subquestions(sub_questions: list[SubQuestion]) -> list[Claim]:
    claims: list[Claim] = []
    for sub_question in sub_questions:
        for index, claim in enumerate(
            plan_claims(sub_question.question, sub_question.expected_evidence), start=1
        ):
            claims.append(claim.model_copy(update={"id": f"{sub_question.id}:claim_{index}"}))
    return claims


_LEGACY_PROCESSOR_CAPABILITIES: dict[str, set[str]] = {
    "circuit_design": {
        "entity_lookup",
        "relationship_lookup",
        "identity_lookup",
        "structure_lookup",
        "datasheet_link_lookup",
    },
    "spreadsheet_table": {"entity_lookup", "tabular_lookup", "revision_lookup"},
    "ragflow": {"entity_lookup", "document_claim_lookup"},
}

_QUERYABLE_CIRCUIT_STATUSES = {"", "indexed", "degraded"}


def _source_capabilities(source: dict[str, Any]) -> set[str]:
    source_group = str(source.get("source_group") or "")
    registered: tuple[EvidenceCapability, ...] = PARSER_REGISTRY.capabilities_for(source_group)
    if registered:
        return {capability.name for capability in registered}
    return set(_LEGACY_PROCESSOR_CAPABILITIES.get(str(source.get("processor_kind") or ""), set()))


def _claim_compatible(source: dict[str, Any], claim: Claim) -> bool:
    if source.get("processor_kind") == "circuit_design" and source.get("status") not in _QUERYABLE_CIRCUIT_STATUSES:
        return False
    available = _source_capabilities(source)
    required = set(claim.required_capabilities)
    if not required:
        return bool(available)
    if claim.support_mode == "composite":
        return bool(available & required)
    return required <= available


def _claim_coverage(claims: list[Claim], evidence: list[dict[str, Any]]) -> list[ClaimCoverage]:
    coverage: list[ClaimCoverage] = []
    for claim in claims:
        evidence_ids: list[str] = []
        missing: list[str] = []
        for capability in claim.required_capabilities:
            matches = [item for item in evidence if _evidence_supports_capability(item, capability)]
            if not matches:
                missing.append(capability)
                continue
            for item in matches:
                evidence_id = str(item.get("id") or "")
                if evidence_id and evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
        coverage.append(
            ClaimCoverage(
                claim_id=claim.id,
                status="supported" if not missing else "partial" if evidence_ids else "missing",
                evidence_ids=evidence_ids,
                missing_capabilities=missing,
            )
        )
    return coverage


def _evidence_supports_capability(evidence: dict[str, Any], capability: str) -> bool:
    content_kind = str(evidence.get("content_kind") or "")
    metadata = evidence.get("metadata") or {}
    locator = evidence.get("locator") or {}
    certainty = str(metadata.get("certainty") or "")
    fact_type = str(metadata.get("fact_type") or "")
    entity_type = str(locator.get("entity_type") or "")
    if capability == "relationship_lookup":
        return content_kind == "circuit_design" and certainty == "direct" and fact_type == "relationship"
    if capability == "entity_lookup":
        return certainty == "direct" and fact_type in {"entity", "relationship", "attribute"}
    if capability == "tabular_lookup":
        return content_kind in {"spreadsheet_table", "test_data"} and certainty == "direct"
    if capability == "document_claim_lookup":
        return content_kind == "document_text"
    if capability == "revision_lookup":
        return bool(metadata.get("revision") or metadata.get("observed_at"))
    if capability == "identity_lookup":
        # Only a unique, evidence-backed identity resolution counts; ambiguous
        # or no_evidence statuses must never support a role claim.
        return (
            content_kind == "circuit_design"
            and entity_type == "component_identity"
            and str(metadata.get("resolution_status") or "") == "unique"
        )
    if capability == "structure_lookup":
        return content_kind == "circuit_design" and entity_type in {"circuit_overview", "module_list"}
    if capability == "datasheet_link_lookup":
        # Populated only by the governed link index (plan task 5b). Absent in
        # M1, so datasheet-backed claims stay unsupported — fail closed.
        return str(metadata.get("datasheet_link_status") or "") == "verified"
    return False


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("LLM response must be a JSON object")
    return data


def _json_for_prompt(value: Any, limit: int = 12000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... truncated ..."


def _chat_with_usage_stage(llm_client: Any, messages: list[dict[str, str]], stage: str) -> str:
    try:
        return llm_client.chat(messages, usage_stage=stage)
    except TypeError as exc:
        if "usage_stage" not in str(exc):
            raise
        return llm_client.chat(messages)


def _stream_chat_with_usage_stage(llm_client: Any, messages: list[dict[str, str]], stage: str):
    try:
        yield from llm_client.stream_chat(messages, usage_stage=stage)
    except TypeError as exc:
        if "usage_stage" not in str(exc):
            raise
        yield from llm_client.stream_chat(messages)


def _response_tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build an OpenAI-style function-tool schema for structured LLM output."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _chat_structured(
    llm_client: Any,
    messages: list[dict[str, str]],
    tool_schema: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    """Get a structured dict from the LLM via native tool-calling.

    Asks the model to call ``tool_schema``'s function (tool_choice=required) and
    returns the parsed arguments. Falls back to JSON-text parsing of the content
    when the provider rejects tools or returns no tool call -- emitting a
    ``degraded`` stream event so the fallback is never silent. Raises on total
    failure; callers keep their existing fail-open (_partial / _empty / ...).
    """
    # "auto" is the most universal across OpenAI-compatible vendors; the client
    # ladder falls back if a vendor rejects tools entirely.
    result = llm_client.chat_with_tools(
        messages,
        tools=[tool_schema],
        tool_choice="auto",
        usage_stage=stage,
    )
    if result.tool_calls:
        for call in result.tool_calls:
            arguments = call.get("arguments")
            if isinstance(arguments, dict) and arguments:
                return arguments
    if not result.tool_call_supported:
        _write_stream_event(
            {"type": "degraded", "stage": stage, "reason": "provider 不支持原生 tool-calling，已回退 JSON 文本解析"}
        )
    if result.content:
        return _extract_json_object(result.content)
    raise RuntimeError("LLM 未返回工具调用，也未返回可解析内容")


def _chat_structured_streaming(
    llm_client: Any,
    messages: list[dict[str, str]],
    tool_schema: dict[str, Any],
    stage: str,
    tool_choice: Any = None,
) -> dict[str, Any]:
    """Streaming variant of _chat_structured.

    Forwards content deltas as ``thought`` stream events (live reasoning for
    the frontend thinking panel) while assembling the structured tool_call
    decision. ``tool_choice`` defaults to required; pass ``"auto"`` for nodes
    where visible reasoning matters more than forcing the call (the model then
    tends to reason in content before calling the tool; JSON-text fallback still
    applies if it returns no tool_call). Same fail-open semantics as
    _chat_structured; raises on total failure.
    """
    # "auto" default works across OpenAI-compatible vendors; the client ladder
    # falls back if a vendor rejects tools entirely.
    effective_choice = tool_choice if tool_choice is not None else "auto"

    # Reasoning deltas are not surfaced (OpenWorker: display-only, dropped
    # end-to-end). The tool_call still streams + accumulates; we just don't
    # forward a thought event for the reasoning content.
    result = llm_client.stream_chat_with_tools(
        messages,
        tools=[tool_schema],
        tool_choice=effective_choice,
        usage_stage=stage,
        on_delta=None,
    )
    if result.tool_calls:
        for call in result.tool_calls:
            arguments = call.get("arguments")
            if isinstance(arguments, dict) and arguments:
                return arguments
    if not result.tool_call_supported:
        _write_stream_event(
            {"type": "degraded", "stage": stage, "reason": "provider 不支持流式 tool-calling，已回退 JSON 文本解析"}
        )
    if result.content:
        return _extract_json_object(result.content)
    raise RuntimeError("LLM 未返回工具调用，也未返回可解析内容")


def _normalize_expected_evidence(values: Any) -> list[str]:
    allowed = {"document_text", "spreadsheet_table", "circuit_design"}
    if isinstance(values, str):
        values = [values]
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text in allowed and text not in result:
            result.append(text)
    return result or ["document_text", "spreadsheet_table", "circuit_design"]


def _as_unique_strings(values: Any, limit: int = 20) -> list[str]:
    if isinstance(values, str):
        values = [values]
    result = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _keyword_entities(query: str) -> list[str]:
    entities = []
    for match in re.findall(r"\b[A-Z]{2,}[A-Z0-9_.-]{1,}\b", query):
        if match not in entities:
            entities.append(match)
    pattern = r"[\u4e00-\u9fffA-Za-z0-9_.-]*(?:料号|替代料|BOM|EMI|EMC|PCB|原理图)[\u4e00-\u9fffA-Za-z0-9_.-]*"
    for match in re.findall(pattern, query):
        if match and match not in entities:
            entities.append(match)
    return entities[:12]


def _required_candidate_evidence(question: str) -> set[str]:
    """Return evidence types that the LLM may supplement but cannot remove."""
    text = str(question or "")
    lower = text.casefold()
    has_refdes = bool(re.search(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d+(?![A-Za-z0-9])", text))
    has_net_identifier = bool(re.search(r"\b[A-Z][A-Z0-9_]{2,}\b", text))
    circuit_terms = (
        "connection",
        "pin",
        "net",
        "topology",
        "signal path",
        "power path",
        "power tree",
        "load switch",
        "连接",
        "引脚",
        "网络",
        "网表",
        "拓扑",
        "信号路径",
        "供电路径",
        "电源树",
        "电源",
        "电压轨",
        "负载开关",
    )
    protection_terms = ("protection", "tvs", "ocp", "scp", "保护", "过压", "过流", "短路", "反接")
    required: set[str] = set()
    circuit_plan = analyze_circuit_question(question)
    if circuit_plan.operations:
        required.add("circuit_design")
    # Role/structure intents are explicit circuit read-model questions; the
    # deterministic plan must include the circuit source even when a weak LLM
    # plan only proposes documents.
    if {"entity_role", "structure_overview", "module_list", "visual_structure"} & set(circuit_plan.operations):
        required.add("circuit_design")
    if (
        has_refdes
        or (has_net_identifier and any(term in lower for term in circuit_terms + protection_terms))
        or any(term in lower for term in circuit_terms + protection_terms)
    ):
        required.add("circuit_design")
    if any(term in lower for term in ("bom", "mpn", "quantity", "supplier", "vendor", "替代料", "单价", "供应商", "用量", "数量")):
        required.add("spreadsheet_table")
    if any(
        term in lower
        for term in ("configuration", "register", "software", "datasheet", "manual", "配置", "寄存器", "软件", "手册", "保护能力") + protection_terms
    ):
        required.add("document_text")
    if "component_selection" in circuit_plan.operations:
        # Selection needs direct circuit constraints plus the supporting
        # specification/documentation; neither source may silently replace
        # the other in an LLM-proposed plan.
        required.update({"circuit_design", "document_text"})
    return required


def _is_pure_pin_mapping_question(question: str) -> bool:
    text = str(question or "")
    lower = text.casefold()
    has_refdes = bool(re.search(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d+(?![A-Za-z0-9])", text))
    connectivity_terms = ("connection", "pin", "net", "连接", "引脚", "网络", "网表")
    additional_evidence_terms = (
        "datasheet",
        "manual",
        "configuration",
        "register",
        "bom",
        "quantity",
        "supplier",
        "vendor",
        "price",
        "数据手册",
        "手册",
        "配置",
        "寄存器",
        "替代料",
        "单价",
        "供应商",
        "用量",
        "数量",
    )
    return (
        has_refdes
        and any(term in lower for term in connectivity_terms)
        and not any(term in lower for term in additional_evidence_terms)
    )


def _merge_expected_evidence(question: str, llm_expected: Any) -> list[str]:
    if _is_pure_pin_mapping_question(question):
        return ["circuit_design"]
    merged = set(_normalize_expected_evidence(llm_expected))
    merged.update(_required_candidate_evidence(question))
    return [kind for kind in ("circuit_design", "document_text", "spreadsheet_table") if kind in merged]


def _expected_evidence(question: str) -> list[str]:
    lower = question.lower()
    expected = []
    has_refdes = bool(re.search(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d{1,}(?![A-Za-z0-9])", question or ""))
    has_net_identifier = bool(re.search(r"\b[A-Z][A-Z0-9_]{2,}\b", question or ""))
    has_circuit_context = any(word in lower for word in [
        "connection",
        "pin",
        "net",
        "topology",
        "module",
        "power",
        "连接",
        "引脚",
        "网络",
        "拓扑",
        "模块",
        "电源",
    ])
    if any(word in lower for word in [
        "edf",
        "edif",
        "netlist",
        "schematic",
        "pin",
        "net ",
        "原理图",
        "网表",
        "引脚",
        "网络",
        "连接",
        "拓扑",
    ]) or has_refdes or (has_net_identifier and has_circuit_context):
        expected.append("circuit_design")
    if any(word in lower for word in [
        "bom",
        "mpn",
        "quantity",
        "supplier",
        "vendor",
        "替代",
        "料号",
        "用量",
        "数量",
        "供应商",
    ]):
        expected.append("spreadsheet_table")
    if any(word in lower for word in [
        "design",
        "layout",
        "schematic",
        "datasheet",
        "test",
        "report",
        "设计",
        "注意",
        "测试",
        "报告",
        "原理图",
    ]):
        expected.append("document_text")
    return _merge_expected_evidence(question, expected)


_SMALL_TALK_PATTERNS = (
    "你好", "您好", "嗨", "hi", "hello", "谢谢", "感谢", "你是谁", "你能做什么",
    "bye", "再见", "早上好", "下午好", "晚上好",
)


# Project/product context signals: when present alongside hardware terms, the
# query is about THIS enterprise's specific project/product — facts that can
# only come from the KB, never general knowledge. Used as a hard override so a
# weak LLM cannot misroute "ADAS项目硬件设计" as general_knowledge.
_PROJECT_CONTEXT_PATTERNS = ("项目", "产品", "项目组", "型号", "机型")
_PROJECT_CODE_RE = re.compile(r"\bADAS\b|\bTCN\d?\b|\bEMS\b", re.IGNORECASE)


def _has_project_context(query: str) -> bool:
    text = query or ""
    if _PROJECT_CODE_RE.search(text):
        return True
    lower = text.lower()
    return any(p in lower for p in _PROJECT_CONTEXT_PATTERNS)


def _route_query_deterministic(query: str) -> dict[str, Any]:
    """Deterministic fallback for query routing.

    Intentionally lopsided: only unmistakable greetings/chitchat short-circuit
    to a direct answer. Anything containing a hardware signal (or anything we
    cannot classify) falls through to retrieval — the conservative default.
    """
    text = (query or "").strip().lower()
    if not text:
        return {"needs_retrieval": True, "category": "hardware_kb_query", "reason": "空查询，按检索处理。"}
    # Whole-query greetings only (avoid matching "你好" inside a real question).
    normalized = text.strip(" ,.，。！!？?;；")
    if normalized in {p.lower() for p in _SMALL_TALK_PATTERNS}:
        return {"needs_retrieval": False, "category": "small_talk", "reason": "识别为问候/寒暄。"}
    if any(p in normalized for p in ("你是谁", "你能做什么", "你是干嘛")):
        return {"needs_retrieval": False, "category": "small_talk", "reason": "识别为身份/能力询问。"}
    # Hardware signal → retrieve.
    if _keyword_entities(query):
        return {"needs_retrieval": True, "category": "hardware_kb_query", "reason": "检测到硬件关键实体，需检索。"}
    lower = query.lower()
    if any(term in lower for term in _HARDWARE_TERMS):
        return {"needs_retrieval": True, "category": "hardware_kb_query", "reason": "检测到硬件术语，需检索。"}
    if _expected_evidence(query):
        return {"needs_retrieval": True, "category": "hardware_kb_query", "reason": "问题预期硬件证据，需检索。"}
    # Default: retrieve (conservative).
    return {"needs_retrieval": True, "category": "hardware_kb_query", "reason": "无法确定类别，保守走检索。"}


def route_query(state: AgentState, llm_client: Any | None = None) -> AgentState:
    """Decide whether the query needs knowledge-base retrieval.

    Returns state with a ``route_decision`` dict:
    ``{"needs_retrieval": bool, "category": str, "reason": str}``.
    Conservative default is retrieval — any failure populates needs_retrieval=True.
    When needs_retrieval is False, also sets a minimal question_analysis for
    trace/observability; the retrieval path leaves question_analysis to
    analyze_question_with_llm.
    """
    query = state.get("user_query", "").strip()
    fallback = _route_query_deterministic(query)
    if llm_client is None or state.get("_skip_route_llm"):
        # KB-scoped chat always retrieves: skip the routing LLM call and use the
        # deterministic fallback (conservative-defaults to retrieval).
        decision = fallback
        trace_node = "route_query"
        trace_msg = "Query routed by deterministic fallback"
    else:
        system_prompt = (
            "You are the Query Router in an enterprise hardware Agentic RAG system. "
            "Return ONLY valid JSON. Do not include chain-of-thought."
        )
        user_prompt = (
            "Classify the user query. Return JSON: "
            '{"category": "small_talk|general_knowledge|hardware_kb_query", '
            '"needs_retrieval": true|false, "reason": "简短中文理由"}\n\n'
            "Categories:\n"
            "- small_talk: greetings/chitchat/identity (needs_retrieval=false)\n"
            "- general_knowledge: answerable from general knowledge, no enterprise KB needed (needs_retrieval=false)\n"
            "- hardware_kb_query: needs facts from THIS knowledge base (needs_retrieval=true)\n\n"
            "Conservative default: if the query mentions specific document names, part numbers, "
            "or facts that should come from the enterprise KB, set needs_retrieval=true. "
            "When uncertain, prefer true.\n\n"
            f"User query:\n{query}\n\n"
            f"Recent chat history:\n{_json_for_prompt(state.get('history') or [], limit=2000)}"
        )
        try:
            raw = _chat_with_usage_stage(
                llm_client,
                [
                    {"role": "system", "content": QUERY_ROUTER_SYSTEM_PROMPT + "\n" + system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "query_router",
            )
            payload = _extract_json_object(raw)
            category = str(payload.get("category") or fallback["category"]).strip()
            if category not in {"small_talk", "general_knowledge", "hardware_kb_query"}:
                category = fallback["category"]
            needs = bool(payload.get("needs_retrieval", fallback["needs_retrieval"]))
            reason = str(payload.get("reason") or fallback["reason"])
            decision = {
                "needs_retrieval": needs,
                "category": category,
                "reason": reason,
            }
            trace_node = "query_router"
            trace_msg = "Query routed by LLM router"
        except Exception:
            decision = fallback
            trace_node = "query_router"
            trace_msg = "LLM router failed; used deterministic fallback"

    new_state = {**state, "route_decision": decision}
    if not decision["needs_retrieval"]:
        intent = "small_talk" if decision["category"] == "small_talk" else "general_question"
        new_state["question_analysis"] = QuestionAnalysis(
            intent=intent,
            summary=decision["reason"],
        ).model_dump()
    new_state["trace"] = _trace(new_state, trace_node, trace_msg, decision)
    return new_state


def compose_direct_answer(state: AgentState, llm_client: Any | None = None) -> AgentState:
    """Answer directly from LLM parametric knowledge without retrieval.

    Used for small_talk / general_knowledge queries. Sets ``answer`` (not
    final_response — the runner wraps it with a lightweight footer). On LLM
    failure returns a canned response instead of the retrieval-specific fallback.
    """
    query = state.get("user_query", "").strip()
    history = state.get("history") or []
    if llm_client is None:
        answer = "你好，我是硬件资料助手。如需查询知识库中的具体资料，请直接描述您的硬件问题。"
    else:
        user_prompt = (
            f"对话历史：\n{_json_for_prompt(history[-6:], limit=3000)}\n\n"
            f"用户问题：{query}\n\n请用中文回答。"
        )
        try:
            answer = _chat_with_usage_stage(
                llm_client,
                [
                    {"role": "system", "content": DIRECT_ANSWER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "direct_answer",
            )
        except Exception as exc:
            answer = (
                f"生成回答时出现错误：{exc}\n"
                "如需查询知识库中的具体资料，请重新描述您的硬件问题。"
            )
    return {
        **state,
        "answer": answer,
        "trace": _trace(state, "compose_direct_answer", "Direct answer (no retrieval)", {}),
    }


def analyze_question(state: AgentState) -> AgentState:
    query = state.get("user_query", "").strip()
    entities = _keyword_entities(query)
    parts = [
        part.strip(" ，,;；。")
        for part in re.split(r"[，,；;。]|\s+和\s+|\s+以及\s+|\s+与\s+", query)
        if part.strip(" ，,;；。")
    ]
    if len(parts) <= 1:
        parts = [query]
    sub_questions = [
        SubQuestion(id=f"sq_{index}", question=part, expected_evidence=_expected_evidence(part))
        for index, part in enumerate(parts[:6], start=1)
    ]
    analysis = QuestionAnalysis(
        intent="multi_source_hardware_query",
        summary=f"我理解你想查询：{query}",
        entities=entities,
        sub_questions=sub_questions,
        claims=_claims_for_subquestions(sub_questions),
        multi_hop=len(sub_questions) > 1,
    )
    return {
        **state,
        "question_analysis": analysis.model_dump(),
        "trace": _trace(state, "analyze_question", "Question analyzed", {"entities": entities}),
    }


def analyze_question_with_llm(state: AgentState, llm_client: Any | None = None) -> AgentState:
    if llm_client is None:
        return analyze_question(state)

    query = state.get("user_query", "").strip()
    history = state.get("history") or []
    system_prompt = (
        "You are the Question Analysis Agent in an enterprise hardware Agentic RAG system. "
        "Analyze the user's request, briefly reason about what entities and evidence are needed, "
        "then call the analyze_question tool with your structured analysis. "
        "Your visible reasoning is streamed to the user as a live thought; keep it concise and in Chinese. "
        "Expose auditable rationale in reasoning_summary."
    )
    user_prompt = (
        "Call the analyze_question tool with arguments matching this schema:\n"
        "{\n"
        '  "intent": "multi_source_hardware_query|lookup|comparison|troubleshooting|general_question",\n'
        '  "summary": "Chinese summary of what the user wants",\n'
        '  "reasoning_summary": "Brief visible rationale",\n'
        '  "entities": ["part numbers, document names, hardware terms"],\n'
        '  "sub_questions": [\n'
        '    {"id": "sq_1", "question": "...", "expected_evidence": ["document_text", "spreadsheet_table", "circuit_design"]}\n'
        "  ],\n"
        '  "multi_hop": true|false\n'
        "}\n\n"
        "multi_hop: true 表示该问题需要多步/跨源推理才能完整回答，例如\"先从设计文档查出某料号，"
        "再去 BOM 表查该料号的用量/供应商\"。单文档单次检索即可回答的问题为 false。"
        "硬件查询常涉及跨文档与表格的联动，倾向 true。\n\n"
        f"User query:\n{query}\n\n"
        f"Recent chat history:\n{_json_for_prompt(history[-6:], limit=4000)}"
    )
    analyze_tool = _response_tool(
        "analyze_question",
        "Analyze the user's hardware query into intent, entities, and sub-questions. Call exactly once.",
        {
            "intent": {"type": "string"},
            "summary": {"type": "string"},
            "reasoning_summary": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "sub_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "question": {"type": "string"},
                        "expected_evidence": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "multi_hop": {"type": "boolean"},
        },
        ["summary", "sub_questions"],
    )
    try:
        payload = _chat_structured_streaming(
            llm_client,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            analyze_tool,
            "question_analysis",
            tool_choice="auto",
        )
        sub_questions = []
        for index, item in enumerate(payload.get("sub_questions") or [], start=1):
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            sub_questions.append(
                SubQuestion(
                    id=str(item.get("id") or f"sq_{index}"),
                    question=question,
                    expected_evidence=_merge_expected_evidence(question, item.get("expected_evidence")),
                )
            )
        if not sub_questions:
            sub_questions = [
                SubQuestion(id="sq_1", question=query, expected_evidence=_expected_evidence(query))
            ]
        analysis = QuestionAnalysis(
            intent=str(payload.get("intent") or "multi_source_hardware_query"),
            summary=str(payload.get("summary") or f"我理解你想查询：{query}"),
            reasoning_summary=str(payload.get("reasoning_summary") or ""),
            entities=_as_unique_strings(payload.get("entities") or _keyword_entities(query), limit=12),
            sub_questions=sub_questions[:6],
            claims=_claims_for_subquestions(sub_questions[:6]),
            multi_hop=bool(payload.get("multi_hop", True)),
        )
        return {
            **state,
            "question_analysis": analysis.model_dump(),
            "trace": _trace(
                state,
                "question_analysis_agent",
                "Question analyzed by LLM agent",
                {
                    "entities": analysis.entities,
                    "sub_questions": len(analysis.sub_questions),
                    "multi_hop": analysis.multi_hop,
                },
            ),
        }
    except Exception as exc:
        fallback = analyze_question(state)
        return {
            **fallback,
            "trace": _trace(
                fallback,
                "question_analysis_agent",
                "LLM analysis failed; used deterministic fallback",
                {"error": str(exc)[:300]},
            ),
        }


def scan_kb_catalog(state: AgentState, catalog_tool) -> AgentState:
    try:
        catalog = catalog_tool.scan(state["kb_name"], state.get("_ctx_obj"), query=state.get("user_query", ""))
    except TypeError as exc:
        if "query" not in str(exc):
            raise
        # Preserve compatibility with custom catalog tools that predate
        # deterministic structured-source ranking.
        catalog = catalog_tool.scan(state["kb_name"], state.get("_ctx_obj"))
    return {
        **state,
        "catalog": catalog,
        "trace": _trace(state, "scan_kb_catalog", "Catalog scanned", catalog.get("summary", {})),
    }


def _source_matches_analysis(source: dict[str, Any], analysis: dict[str, Any]) -> tuple[bool, str]:
    processor = source.get("processor_kind", "")
    content_kind = source.get("content_kind", "")
    name = source.get("document_name", "")
    source_group = source.get("source_group", "")
    claims = [Claim.model_validate(item) for item in analysis.get("claims") or []]
    if claims and any(_claim_compatible(source, claim) for claim in claims):
        return True, "该来源具备至少一条待证明结论所需的检索能力。"
    sub_questions = analysis.get("sub_questions") or []
    expected = {item for sq in sub_questions for item in sq.get("expected_evidence", [])}
    if processor == "spreadsheet_table" and "spreadsheet_table" in expected:
        return True, "该文件是结构化 Excel，适合查询 BOM、用量、替代料、参数或测试矩阵等表格事实。"
    if processor == "circuit_design":
        if str(source.get("status") or "") not in _QUERYABLE_CIRCUIT_STATUSES:
            return False, "电路文件尚未索引成功，跳过结构化电路检索。"
        if "circuit_design" in expected:
            return True, "该文件是结构化电路设计数据，适合查询网表、引脚、网络连接和拓扑事实。"
    if content_kind == "document_text" and "document_text" in expected:
        return True, "该文件是文本文档，适合查询设计说明、测试报告、规格说明和上下文解释。"
    entity_hit = any(str(entity).casefold() in name.casefold() for entity in analysis.get("entities", []))
    if entity_hit:
        return True, "文件名包含问题中的关键实体，建议纳入检索范围。"
    if source_group in {"design", "docs", "test", "material"}:
        return True, f"文件归类为 {source_group}，可能包含相关硬件资料。"
    return False, "与当前问题的证据类型或关键实体关联较弱。"


def _source_aliases(source: dict[str, Any]) -> set[str]:
    aliases = set()
    for field in ["document_name", "original_file_name"]:
        aliases.update(_name_aliases(str(source.get(field) or "")))
    return aliases


def _name_aliases(value: str) -> set[str]:
    aliases = set()
    text = str(value or "").strip()
    if not text:
        return aliases
    aliases.add(text.casefold())
    basename = re.split(r"[\\/]", text)[-1]
    if basename:
        aliases.add(basename.casefold())
        stem = basename.rsplit(".", 1)[0]
        if len(stem) >= 3:
            aliases.add(stem.casefold())
    return aliases


def _resolve_catalog_source(sources: list[dict[str, Any]], source_name: str) -> dict[str, Any] | None:
    requested_aliases = _name_aliases(source_name)
    if requested_aliases:
        for source in sources:
            if requested_aliases & _source_aliases(source):
                return source
    # Lenient fallback: substring / contains match against document_name and
    # original_file_name, so a weak model that half-remembers a source still resolves.
    norm = str(source_name or "").strip().lower()
    if norm:
        for source in sources:
            for field in ("document_name", "original_file_name"):
                candidate = str(source.get(field) or "").strip().lower()
                if candidate and (norm in candidate or candidate in norm):
                    return source
    return None


def _complete_required_source_plan(
    state: AgentState,
    source_plan: SourcePlan,
    max_circuit_sources: int | None = None,
) -> SourcePlan:
    """Ensure required catalog evidence types are not omitted by an LLM plan."""
    expected = {
        evidence_type
        for sub_question in (state.get("question_analysis") or {}).get("sub_questions") or []
        for evidence_type in sub_question.get("expected_evidence") or []
    }
    if not expected:
        return source_plan

    planned = {item.source_name: item for item in source_plan.source_plan}
    sources = (state.get("catalog") or {}).get("sources") or []
    need_circuit = "circuit_design" in expected
    need_document = "document_text" in expected

    # 电路源：按路由相关度排序并受 max_circuit_sources 上限约束，确定性补全 LLM 计划遗漏的电路源。
    if need_circuit:
        circuit_sources = [
            source
            for source in sources
            if source.get("processor_kind") == "circuit_design"
            and source.get("status") in _QUERYABLE_CIRCUIT_STATUSES
        ]
        circuit_sources.sort(
            key=lambda source: (-_routing_metadata(source)[0], str(source.get("document_name") or ""))
        )
        planned_circuit_count = sum(
            1 for item in source_plan.source_plan if item.processor_kind == "circuit_design"
        )
        for source in circuit_sources:
            if max_circuit_sources is not None and planned_circuit_count >= max_circuit_sources:
                break
            source_name = str(source.get("document_name") or "")
            if not source_name:
                continue
            filters = {
                key: value
                for key, value in {
                    "source_name": source_name,
                    "record_id": source.get("record_id"),
                }.items()
                if value not in (None, "")
            }
            item = planned.get(source_name)
            if item is None:
                item = SourcePlanItem(
                    source_name=source_name,
                    processor_kind="circuit_design",
                    reason="Deterministic required circuit evidence source.",
                )
                source_plan.source_plan.append(item)
                planned[source_name] = item
                planned_circuit_count += 1
            if any(call.tool_name == "circuit_query" for call in item.tool_calls):
                continue
            item.tool_calls.append(
                ToolCallPlan(
                    tool_name="circuit_query",
                    query=state.get("user_query", ""),
                    reason="Search deterministic required circuit evidence source.",
                    top_k=8,
                    filters=filters,
                )
            )

    # 文档源：确定性补全 LLM 计划遗漏的文档证据。
    if need_document:
        for source in sources:
            processor = str(source.get("processor_kind") or "")
            content_kind = str(source.get("content_kind") or "")
            is_document = content_kind == "document_text" or processor == "ragflow"
            if not is_document:
                continue
            source_name = str(source.get("document_name") or "")
            if not source_name:
                continue
            filters = {
                key: value
                for key, value in {
                    "source_name": source_name,
                    "record_id": source.get("record_id"),
                }.items()
                if value not in (None, "")
            }
            item = planned.get(source_name)
            if item is None:
                item = SourcePlanItem(
                    source_name=source_name,
                    processor_kind=processor,
                    reason="Deterministic required document evidence source.",
                )
                source_plan.source_plan.append(item)
                planned[source_name] = item
            if any(call.tool_name == "document_rag" for call in item.tool_calls):
                continue
            item.tool_calls.append(
                ToolCallPlan(
                    tool_name="document_rag",
                    query=state.get("user_query", ""),
                    reason="Search deterministic required document evidence source.",
                    top_k=8,
                    filters=filters,
                )
            )
    return source_plan


_FAST_SOURCE_LIMITS = {
    "spreadsheet_table": 2,
    "circuit_design": 2,
    "default": 3,
}


def _routing_metadata(source: dict[str, Any]) -> tuple[int, list[str]]:
    routing = (source.get("metadata") or {}).get("routing") or {}
    try:
        score = max(0, int(routing.get("score") or 0))
    except (TypeError, ValueError):
        score = 0
    terms = [str(term) for term in routing.get("matched_terms") or [] if str(term)]
    return score, terms


def plan_source_selection(state: AgentState) -> AgentState:
    analysis = state.get("question_analysis") or {}
    sources = (state.get("catalog") or {}).get("sources") or []
    plan_items = []
    skipped = []
    query = state.get("user_query", "")
    candidates_by_processor: dict[str, list[tuple[int, dict, str, int, list[str]]]] = {}
    for index, source in enumerate(sources):
        source_name = source.get("document_name", "")
        should_use, reason = _source_matches_analysis(source, analysis)
        processor = str(source.get("processor_kind") or "")
        routing_score, matched_terms = _routing_metadata(source)
        if processor == "circuit_design" and str(source.get("status") or "") != "indexed":
            skipped.append({"source_name": source_name, "reason": "电路文件尚未索引成功，跳过结构化电路检索。"})
            continue
        if not should_use and not routing_score:
            skipped.append({"source_name": source_name, "reason": reason})
            continue
        if routing_score:
            reason = f"本地结构化索引命中：{', '.join(matched_terms[:4])}。"
        candidates_by_processor.setdefault(processor or "default", []).append(
            (index, source, reason, routing_score, matched_terms)
        )

    selected_indexes: set[int] = set()
    for processor, candidates in candidates_by_processor.items():
        limit = _FAST_SOURCE_LIMITS.get(processor, _FAST_SOURCE_LIMITS["default"])
        matched = [candidate for candidate in candidates if candidate[3] > 0]
        ranked = sorted(matched or candidates, key=lambda item: (-item[3], item[0]))
        selected_indexes.update(item[0] for item in ranked[:limit])
        for index, source, _reason, score, _terms in ranked[limit:]:
            skipped.append(
                {
                    "source_name": source.get("document_name", ""),
                    "reason": "本地索引相关度较低，未进入快速模式的受控检索范围。" if score else "同类来源过多，未进入快速模式的受控回退范围。",
                }
            )

    for index, source in enumerate(sources):
        if index not in selected_indexes:
            continue
        source_name = source.get("document_name", "")
        _score, _terms = _routing_metadata(source)
        should_use, reason = _source_matches_analysis(source, analysis)
        if _score:
            reason = f"本地结构化索引命中：{', '.join(_terms[:4])}。"
        processor = source.get("processor_kind", "")
        calls = []
        filters = {
            "source_name": source.get("document_name", ""),
            "record_id": source.get("record_id"),
        }
        compact_filters = {key: value for key, value in filters.items() if value}
        if processor == "spreadsheet_table":
            calls.append(
                ToolCallPlan(
                    tool_name="spreadsheet_semantic",
                    query=query,
                    reason="查找 Excel 语义行，覆盖行级事实。",
                    top_k=8,
                    filters=compact_filters,
                )
            )
            calls.append(
                ToolCallPlan(
                    tool_name="spreadsheet_cell",
                    query=query,
                    reason="查找 Excel 单元格，覆盖精确料号或字段值。",
                    top_k=12,
                    filters=compact_filters,
                )
            )
        elif processor == "circuit_design":
            calls.append(
                ToolCallPlan(
                    tool_name="circuit_query",
                    query=query,
                    reason="查询结构化电路数据，覆盖网表、引脚、网络连接和拓扑事实。",
                    top_k=8,
                    filters=compact_filters,
                )
            )
        else:
            calls.append(
                ToolCallPlan(
                    tool_name="document_rag",
                    query=query,
                    reason="查找文档片段，覆盖设计、规格、测试和说明性内容。",
                    top_k=8,
                    filters=compact_filters,
                )
            )
        plan_items.append(
            SourcePlanItem(
                source_name=source_name,
                processor_kind=processor,
                reason=reason,
                tool_calls=calls,
            )
        )
    source_plan = _complete_required_source_plan(
        state,
        SourcePlan(source_plan=plan_items, skipped_sources=skipped),
        max_circuit_sources=_FAST_SOURCE_LIMITS["circuit_design"],
    )
    return {
        **state,
        "source_plan": source_plan.model_dump(),
        "trace": _trace(state, "plan_source_selection", "Source plan generated", {"planned_sources": len(plan_items)}),
    }


def plan_source_selection_with_llm(state: AgentState, llm_client: Any | None = None) -> AgentState:
    if llm_client is None:
        return plan_source_selection(state)

    sources = (state.get("catalog") or {}).get("sources") or []
    visible_sources = sources
    if not visible_sources:
        return plan_source_selection(state)

    source_by_name = {str(source.get("document_name") or ""): source for source in visible_sources}
    compact_sources = [
        {
            "document_name": source.get("document_name"),
            "original_file_name": source.get("original_file_name"),
            "record_id": source.get("record_id"),
            "processor_kind": source.get("processor_kind"),
            "content_kind": source.get("content_kind"),
            "source_group": source.get("source_group"),
            "status": source.get("status"),
            "profile": source.get("profile") or {},
        }
        for source in visible_sources[:80]
    ]
    system_prompt = (
        "You are the Retrieval Planner and Query Rewriter Agent in an enterprise hardware Agentic RAG system. "
        "Follow a Google-style Agentic RAG search-fanout pattern: decompose the request into search tasks, "
        "select the best corpus/source for each task, and propose parallel tool calls. "
        "Call the emit_retrieval_plan tool with your plan; do not include chain-of-thought; use concise visible reasons."
    )
    user_prompt = (
        "Available tools:\n"
        "- document_rag: use for document_text / ragflow sources\n"
        "- spreadsheet_semantic: use for spreadsheet_table row-level facts\n"
        "- spreadsheet_cell: use for spreadsheet_table exact cells, part numbers, quantities, parameters\n\n"
        "- circuit_query: use for circuit_design netlist/schematic connection facts\n\n"
        "Planning requirements:\n"
        "- Build search fanout from the sub_questions, not a single best source.\n"
        "- If different sub_questions need different evidence types or corpora, select multiple sources and tool calls.\n"
        "- When the catalog contains multiple plausible data sources, cover each distinct evidence type/source needed by the sub_questions instead of repeatedly choosing one source.\n"
        "- For spreadsheet_table sources, prefer both spreadsheet_semantic and spreadsheet_cell when exact fields/parts/models may matter.\n"
        "- Do not add sources by rule; every selected source must be justified by the question analysis and catalog.\n"
        "- source_name MUST be copied verbatim from the catalog's document_name field — never invent or paraphrase it.\n"
        "- tool_name MUST be exactly one of: document_rag | spreadsheet_semantic | spreadsheet_cell | circuit_query. Never combine two names.\n\n"
        "Call the emit_retrieval_plan tool with arguments matching this schema:\n"
        "{\n"
        '  "source_plan": [\n'
        '    {"source_name": "must exactly match catalog document_name", "reason": "visible reason", '
        '"tool_calls": [{"tool_name": "document_rag|spreadsheet_semantic|spreadsheet_cell|circuit_query", '
        '"query": "rewritten retrieval query", "reason": "visible reason", "top_k": 8}]}\n'
        "  ],\n"
        '  "skipped_sources": [{"source_name": "...", "reason": "..."}]\n'
        "}\n\n"
        f"User query:\n{state.get('user_query', '')}\n\n"
        f"Question analysis:\n{_json_for_prompt(state.get('question_analysis') or {}, limit=5000)}\n\n"
        f"Catalog:\n{_json_for_prompt(compact_sources, limit=14000)}"
    )
    plan_tool = _response_tool(
        "emit_retrieval_plan",
        "Emit the retrieval source plan and parallel tool calls for this query. Call exactly once.",
        {
            "source_plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_name": {"type": "string"},
                        "reason": {"type": "string"},
                        "tool_calls": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tool_name": {"type": "string"},
                                    "query": {"type": "string"},
                                    "reason": {"type": "string"},
                                    "top_k": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
            },
            "skipped_sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"source_name": {"type": "string"}, "reason": {"type": "string"}},
                },
            },
        },
        ["source_plan"],
    )
    try:
        payload = _chat_structured_streaming(
            llm_client,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            plan_tool,
            "source_planning",
            tool_choice="auto",
        )
        plan_items = []
        used_sources = set()
        for item in payload.get("source_plan") or []:
            if not isinstance(item, dict):
                continue
            source_name = str(item.get("source_name") or "").strip()
            source = source_by_name.get(source_name) or _resolve_catalog_source(visible_sources, source_name)
            if source is None:
                continue
            source_name = str(source.get("document_name") or source_name)
            processor = source.get("processor_kind", "")
            allowed_tools = (
                {"spreadsheet_semantic", "spreadsheet_cell"}
                if processor == "spreadsheet_table"
                else {"circuit_query"}
                if processor == "circuit_design"
                else {"document_rag"}
            )
            filters = {
                "source_name": source.get("document_name", ""),
                "record_id": source.get("record_id"),
            }
            compact_filters = {key: value for key, value in filters.items() if value}
            calls = []
            for call in item.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                tool_name = _normalize_tool_name(call.get("tool_name"), allowed_tools)
                if not tool_name:
                    continue
                calls.append(
                    ToolCallPlan(
                        tool_name=tool_name,
                        query=str(call.get("query") or state.get("user_query") or ""),
                        reason=str(call.get("reason") or item.get("reason") or ""),
                        top_k=max(1, min(20, int(call.get("top_k") or 8))),
                        filters=compact_filters,
                    )
                )
            if not calls:
                calls = _default_tool_calls_for_source(source, state.get("user_query", ""), compact_filters)
            plan_items.append(
                SourcePlanItem(
                    source_name=source_name,
                    processor_kind=processor,
                    reason=str(item.get("reason") or "LLM planner selected this source."),
                    tool_calls=calls,
                )
            )
            used_sources.add(source_name)

        if not plan_items:
            raise ValueError("LLM planner returned no usable source_plan items")

        skipped = []
        for item in payload.get("skipped_sources") or []:
            if not isinstance(item, dict):
                continue
            source_name = str(item.get("source_name") or "")
            if source_name in source_by_name and source_name not in used_sources:
                skipped.append({"source_name": source_name, "reason": str(item.get("reason") or "")})
        for source_name in source_by_name:
            if source_name not in used_sources and not any(item.get("source_name") == source_name for item in skipped):
                skipped.append({"source_name": source_name, "reason": "Planner did not select this source for the current query."})

        source_plan = _complete_required_source_plan(
            state,
            SourcePlan(source_plan=plan_items, skipped_sources=skipped),
        )
        return {
            **state,
            "source_plan": source_plan.model_dump(),
            "trace": _trace(
                state,
                "retrieval_planner_agent",
                "Search fanout planned by LLM agent",
                {"planned_sources": len(plan_items), "tool_calls": sum(len(item.tool_calls) for item in plan_items)},
            ),
        }
    except Exception as exc:
        _write_stream_event(
            {"type": "degraded", "stage": "plan_source_selection", "reason": f"LLM 规划失败，退回确定性默认计划：{str(exc)[:160]}"}
        )
        fallback = plan_source_selection(state)
        return {
            **fallback,
            "trace": _trace(
                fallback,
                "retrieval_planner_agent",
                "LLM planner failed; used deterministic fallback",
                {"error": str(exc)[:300]},
            ),
        }


def _default_tool_calls_for_source(source: dict[str, Any], query: str, filters: dict[str, Any]) -> list[ToolCallPlan]:
    if source.get("processor_kind") == "spreadsheet_table":
        return [
            ToolCallPlan(
                tool_name="spreadsheet_semantic",
                query=query,
                reason="Fallback spreadsheet semantic retrieval.",
                top_k=8,
                filters=filters,
            ),
            ToolCallPlan(
                tool_name="spreadsheet_cell",
                query=query,
                reason="Fallback spreadsheet exact-cell retrieval.",
                top_k=12,
                filters=filters,
            ),
        ]
    if source.get("processor_kind") == "circuit_design":
        return [
            ToolCallPlan(
                tool_name="circuit_query",
                query=query,
                reason="Fallback circuit-design retrieval.",
                top_k=8,
                filters=filters,
            )
        ]
    return [
        ToolCallPlan(
            tool_name="document_rag",
            query=query,
            reason="Fallback document retrieval.",
            top_k=8,
            filters=filters,
        )
    ]


def _allowed_tools_for_processor(processor_kind: str) -> set[str]:
    if processor_kind == "spreadsheet_table":
        return {"spreadsheet_semantic", "spreadsheet_cell"}
    if processor_kind == "circuit_design":
        return {"circuit_query"}
    return {"document_rag"}


def _normalize_tool_name(name: Any, allowed: set[str]) -> str:
    """Map a weak model's sloppy tool name to a valid one. E.g. the planner
    sometimes emits 'spreadsheet_semantic/cell' (concatenated) or 'semantic'."""
    n = str(name or "").strip().lower()
    if n in allowed:
        return n
    for a in sorted(allowed):
        if a in n:
            return a
    for a in sorted(allowed):
        if n in a:
            return a
    return ""


def _tool_for_evidence_type(evidence_type: str) -> str:
    if evidence_type == "spreadsheet_table":
        return "spreadsheet_semantic"
    if evidence_type == "circuit_design":
        return "circuit_query"
    return "document_rag"


def _retrievable_gap_queries(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic re-retrieval plan from ledger gaps: an uncovered sub-question
    with an unsearched source or a missing evidence type becomes a targeted query.
    Used as a hard guard so a weak judge LLM cannot wrongly skip a needed re-retrieval."""
    queries: list[dict[str, Any]] = []
    for item in ledger:
        if item.get("status") == "covered":
            continue
        sq = str(item.get("sub_question") or "").strip()
        if not sq:
            continue
        expected = item.get("expected_evidence") or []
        default_tool = _tool_for_evidence_type(str(next(iter(expected), "") or ""))
        for src in (item.get("unsearched_relevant_sources") or [])[:3]:
            queries.append(
                {
                    "query": sq,
                    "tool_name": default_tool,
                    "source_name": str(src),
                    "reason": f"补查未覆盖来源：{src}",
                }
            )
        for etype in (item.get("missing_evidence_types") or [])[:2]:
            et = str(etype)
            queries.append(
                {
                    "query": sq,
                    "tool_name": _tool_for_evidence_type(et) if et else default_tool,
                    "source_name": "",
                    "reason": f"缺少证据类型：{et}",
                }
            )
        if len(queries) >= 4:
            break
    return queries


def _dedupe_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for call in calls:
        filters = call.get("filters") or {}
        key = (
            str(call.get("tool_name") or ""),
            str(filters.get("source_name") or "").casefold(),
            str(call.get("query") or "").strip().casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(call)
    return result


def _normalize_sufficiency_status(status: Any) -> str:
    """Map a weak judge model's status value to the canonical enum, tolerating
    abbreviations / Chinese variants so its output still counts."""
    s = str(status or "").strip().lower()
    if s in {"sufficient", "enough", "yes", "ok", "充分", "充足", "够"}:
        return "sufficient"
    if s in {"partial_but_answerable", "partial", "partial_answerable", "can_answer", "部分", "部分可答", "可答"}:
        return "partial_but_answerable"
    if s in {"insufficient_need_more", "insufficient", "need_more", "not_enough", "not enough", "不足", "不充分", "不够"}:
        return "insufficient_need_more"
    return ""


def _deterministic_circuit_gap_calls(state: AgentState, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger = state.get("retrieval_ledger") or []
    unsearched = {
        str(source_name)
        for item in ledger
        if "circuit_design" in (item.get("missing_evidence_types") or [])
        for source_name in item.get("unsearched_relevant_sources") or []
        if source_name
    }
    if not unsearched:
        return []

    searched = {
        str((item.get("filters") or {}).get("source_name") or "")
        for item in state.get("retrieval_diagnostics") or []
        if item.get("tool_name") == "circuit_query"
    }
    if "" in searched:
        return []

    calls = []
    for source in sources:
        source_name = str(source.get("document_name") or "")
        if (
            source_name not in unsearched
            or source_name in searched
            or source.get("processor_kind") != "circuit_design"
            or source.get("status") not in _QUERYABLE_CIRCUIT_STATUSES
        ):
            continue
        filters = {
            key: value
            for key, value in {"source_name": source_name, "record_id": source.get("record_id")}.items()
            if value not in (None, "")
        }
        calls.append(
            {
                "tool_name": "circuit_query",
                "query": state.get("user_query", ""),
                "reason": "Retrieve unsearched indexed circuit evidence required by the retrieval ledger.",
                "top_k": 8,
                "filters": filters,
            }
        )
    return calls


def plan_next_retrieval(state: AgentState, llm_client: Any | None, catalog_tool: Any) -> AgentState:
    """多跳重规划：消费 judge_sufficiency 的 suggested_queries + 全量 catalog，
    产出下一轮 tool_calls（支持跨语料 cross-corpus）。

    失败处理（不退规则）：LLM 不可用/异常/解析失败/空 → next_retrieval_calls=[]，
    should_continue 见空自然终止迭代，用已有证据作答。
    """
    round_no = int(state.get("retrieval_round") or 0)
    suggested = (state.get("sufficiency") or {}).get("suggested_queries") or []

    def _empty(reason: str, error: str = "") -> AgentState:
        return {
            **state,
            "next_retrieval_calls": [],
            "trace": _trace(
                state,
                "plan_next_retrieval",
                reason,
                {"round": round_no, "suggested_queries": [], "error": error},
            ),
        }

    if not suggested:
        return _empty("无可执行的补检索查询，终止迭代。")

    # 用 catalog_tool 取全量源（支持跨语料），权限由后端 retrieve 侧收紧。
    try:
        catalog = catalog_tool.scan(state["kb_name"], state.get("_ctx_obj"))
    except Exception as exc:
        _write_stream_event(
            {"type": "degraded", "stage": "plan_next_retrieval", "reason": f"目录扫描失败，终止多跳：{str(exc)[:160]}"}
        )
        return _empty("目录扫描失败，终止迭代。", error=str(exc)[:300])
    sources = catalog.get("sources") or []
    source_by_name = {str(src.get("document_name") or ""): src for src in sources}
    deterministic_calls = _deterministic_circuit_gap_calls(state, sources)

    if llm_client is None:
        if deterministic_calls:
            return {
                **state,
                "next_retrieval_calls": deterministic_calls,
                "trace": _trace(
                    state,
                    "plan_next_retrieval",
                    "LLM unavailable; scheduled deterministic circuit gap retrieval.",
                    {"round": round_no, "suggested_queries": [call["query"] for call in deterministic_calls]},
                ),
            }
        _write_stream_event(
            {"type": "degraded", "stage": "plan_next_retrieval", "reason": "重规划 LLM 不可用，终止多跳"}
        )
        return _empty("重规划 LLM 不可用，终止迭代。", error="no_llm_client")

    compact_sources = [
        {
            "document_name": src.get("document_name"),
            "original_file_name": src.get("original_file_name"),
            "record_id": src.get("record_id"),
            "processor_kind": src.get("processor_kind"),
            "content_kind": src.get("content_kind"),
            "source_group": src.get("source_group"),
        }
        for src in sources[:80]
    ]
    previous_calls = [
        {
            "tool_name": item.get("tool_name"),
            "hit_count": item.get("hit_count"),
            "status": item.get("status"),
            "scoped": bool(item.get("filters")),
            "source_name": (item.get("filters") or {}).get("source_name", ""),
        }
        for item in state.get("retrieval_diagnostics") or []
    ]
    ledger = state.get("retrieval_ledger") or []
    used_sources = sorted(
        {
            str(item.get("source_name") or "")
            for item in state.get("merged_evidence") or state.get("evidence") or []
            if item.get("source_name")
        }
    )
    planned_sources = sorted(
        {
            str(item.get("source_name") or "")
            for item in (state.get("source_plan") or {}).get("source_plan", [])
            if item.get("source_name")
        }
    )
    unused_sources = [
        str(src.get("document_name") or "")
        for src in sources
        if src.get("document_name") and src.get("document_name") not in used_sources
    ][:40]
    user_prompt = (
        f"用户问题：{state.get('user_query', '')}\n\n"
        f"充分性判断器建议的补检索查询：\n{_json_for_prompt(suggested, limit=4000)}\n\n"
        f"第一轮/历史计划源：\n{_json_for_prompt(planned_sources, limit=2000)}\n\n"
        f"已经产生证据的来源：\n{_json_for_prompt(used_sources, limit=2000)}\n\n"
        f"尚未产生证据的可选来源：\n{_json_for_prompt(unused_sources, limit=4000)}\n\n"
        f"历史检索诊断（命中数、是否 scoped）：\n{_json_for_prompt(previous_calls[-16:], limit=5000)}\n\n"
        f"检索账本（按子问题列出 gap feedback 与未查相关来源）：\n{_json_for_prompt(ledger, limit=7000)}\n\n"
        f"知识库目录（全量，可跨语料）：\n{_json_for_prompt(compact_sources, limit=12000)}\n\n"
        "请调用 emit_next_retrieval_calls 工具产出下一轮 tool_calls。"
        "若当前缺口需要另一类证据或已有来源反复未补齐，请优先切换到目录中的其他相关数据源；"
        "只有当同一来源确有新查询价值时才继续查同一来源。"
    )
    next_tool = _response_tool(
        "emit_next_retrieval_calls",
        "Emit the next-round retrieval tool calls. Call exactly once; pass an empty tool_calls array if no useful call exists.",
        {
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string"},
                        "query": {"type": "string"},
                        "source_name": {"type": "string"},
                        "reason": {"type": "string"},
                        "top_k": {"type": "integer"},
                    },
                },
            },
        },
        ["tool_calls"],
    )
    try:
        payload = _chat_structured_streaming(
            llm_client,
            [
                {"role": "system", "content": PLAN_NEXT_RETRIEVAL_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            next_tool,
            "next_retrieval_planning",
        )
        calls = []
        for call in payload.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            q = str(call.get("query") or "").strip()
            tool_name = str(call.get("tool_name") or "").strip()
            if not q or tool_name not in {"document_rag", "spreadsheet_semantic", "spreadsheet_cell", "circuit_query"}:
                continue
            source_name = str(call.get("source_name") or "").strip()
            filters: dict[str, Any] = {}
            # 指定了 source_name 且在 catalog 中 → 跨语料精准检索；否则广搜（filters 空）。
            src = source_by_name.get(source_name) or _resolve_catalog_source(sources, source_name)
            if src:
                if tool_name not in _allowed_tools_for_processor(str(src.get("processor_kind") or "")):
                    continue
                source_name = str(src.get("document_name") or source_name)
                filters = {"source_name": source_name, "record_id": src.get("record_id")}
                filters = {k: v for k, v in filters.items() if v}
            calls.append(
                {
                    "tool_name": tool_name,
                    "query": q,
                    "reason": str(call.get("reason") or "").strip(),
                    "top_k": max(1, min(20, int(call.get("top_k") or 8))),
                    "filters": filters,
                }
            )
        calls = _dedupe_tool_calls([*deterministic_calls, *calls])
        return {
            **state,
            "next_retrieval_calls": calls,
            "trace": _trace(
                state,
                "plan_next_retrieval",
                f"多跳重规划产出 {len(calls)} 条新调用" if calls else "重规划未产出可用调用，终止迭代",
                {"round": round_no, "suggested_queries": [c.get("query", "") for c in calls]},
            ),
        }
    except Exception as exc:
        _write_stream_event(
            {"type": "degraded", "stage": "plan_next_retrieval", "reason": f"重规划 LLM 失败，终止多跳：{str(exc)[:160]}"}
        )
        return _empty("重规划 LLM 失败，终止迭代。", error=str(exc)[:300])


_CAPABILITY_TERMS = (
    "short circuit", "short-to-ground", "short-to-battery", "overcurrent", "current limit", "thermal shutdown", "ocp", "scp",
    "短路保护", "短地", "短电源", "过流保护", "限流", "热关断",
)


def _is_matching_datasheet_capability(source_name: str, content: str, part_number: str) -> bool:
    """Require both the discovered MPN and an explicit protection term."""
    part = str(part_number or "").strip().casefold()
    searchable = f"{source_name or ''}\n{content or ''}".casefold()
    return bool(part and part in searchable and any(term in searchable for term in _CAPABILITY_TERMS))


def _derived_datasheet_calls(
    question: str,
    circuit_hits: list[dict[str, Any]],
    verified_links: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Bounded datasheet lookups gated by VERIFIED component-datasheet links.

    Without a verified link (governed index, plan task 5b) this returns no
    calls at all — internal part numbers or topology candidates alone must
    never trigger open-ended ``document_rag`` searches.
    """
    if not analyze_circuit_question(question).requires_datasheet:
        return []
    links = [item for item in (verified_links or []) if str(item.get("part_number") or "").strip()]
    if not links:
        return []
    link_by_part = {
        str(item["part_number"]).strip().casefold(): item
        for item in links
        if str(item.get("part_number") or "").strip()
    }
    part_numbers: list[str] = []
    for hit in circuit_hits:
        metadata = hit.get("metadata") or {}
        if metadata.get("evidence_kind") != "derived_topology" or not metadata.get("capability_candidate"):
            continue
        for raw in metadata.get("part_numbers") or []:
            part = str(raw or "").strip()
            if (
                part
                and part.casefold() in link_by_part
                and part.casefold() not in {item.casefold() for item in part_numbers}
            ):
                part_numbers.append(part)
            if len(part_numbers) >= 4:
                break
        if len(part_numbers) >= 4:
            break
    calls = []
    for part in part_numbers:
        link = link_by_part[part.casefold()]
        record_ids = sorted({int(item) for item in link.get("record_ids") or [] if str(item).isdigit()})
        if not record_ids:
            continue
        calls.append(
            {
                "tool_name": "document_rag",
                "query": f"{part} datasheet short circuit protection OCP SCP short-to-ground short-to-battery thermal shutdown",
                "reason": f"Verified component-datasheet link for {part}; lookup restricted to linked records.",
                "top_k": 4,
                "filters": {"allowed_record_ids": record_ids},
                "part_number": part,
            }
        )
    return calls


def retrieve_evidence(state: AgentState, tools: dict[str, Any]) -> AgentState:
    evidence = list(state.get("evidence") or [])
    round_no = int(state.get("retrieval_round") or 0) + 1
    calls = []
    diagnostics = []
    # 第二轮起用 plan_next_retrieval 产出的动态新查询（多跳）；第一轮用 planner 的 source_plan。
    if round_no > 1 and state.get("next_retrieval_calls"):
        calls = list(state["next_retrieval_calls"])
    else:
        for item in (state.get("source_plan") or {}).get("source_plan", []):
            calls.extend(item.get("tool_calls", []))

    circuit_calls = [call for call in calls if call.get("tool_name") == "circuit_query"]
    other_calls = [call for call in calls if call.get("tool_name") != "circuit_query"]
    bounded_calls = [*circuit_calls, *other_calls[: max(0, 8 - len(circuit_calls))]]
    circuit_hits: list[dict[str, Any]] = []
    cancel_check = state.get("_cancel_check")

    def _run_call(call: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        started = time.monotonic()
        tool = tools.get(call.get("tool_name"))
        query = call.get("query") or state.get("user_query", "")
        filters = call.get("filters") or {}
        top_k = int(call.get("top_k") or 5)
        tool_name = str(call.get("tool_name") or "unknown")
        retriever_name = {
            "document_rag": "ragflow",
            "spreadsheet_semantic": "spreadsheet",
            "spreadsheet_cell": "spreadsheet",
            "spreadsheet_profile": "spreadsheet",
            "circuit_query": "circuit",
        }.get(tool_name, tool_name)
        if tool is None:
            record_retrieval(
                retriever=retriever_name,
                status="failed",
                duration_s=max(0.0, time.monotonic() - started),
                hit_count=0,
            )
            return (
                {
                    "tool_name": call.get("tool_name"),
                    "query": query,
                    "filters": filters,
                    "top_k": top_k,
                    "hit_count": 0,
                    "status": "missing_tool",
                    "latency_ms": int((time.monotonic() - started) * 1000),
                },
                [],
                None,
            )
        try:
            run_kwargs = {"top_k": top_k, "filters": filters}
            if cancel_check is not None and getattr(tool, "supports_cancellation", False):
                run_kwargs["should_cancel"] = cancel_check
            with observe.retriever(
                f"hdb.retrieve.{retriever_name}",
                retriever=retriever_name,
                **{
                    "hdb.retrieval.round": round_no,
                    "hdb.retrieval.top_k": top_k,
                },
            ) as observation:
                observation.set_input(query, content_kind="query")
                hits = tool.run(query, state["kb_name"], state.get("_ctx_obj"), **run_kwargs)
                observation.hit_count(len(hits))
                observation.set("hdb.retrieval.status", "success")
                observation.set_content(
                    "retrieval.documents",
                    [
                        {
                            "id": getattr(hit, "id", ""),
                            "source_name": getattr(hit, "source_name", ""),
                            "score": getattr(hit, "score", None),
                            "locator": getattr(hit, "locator", {}) or {},
                            "content_kind": getattr(hit, "content_kind", ""),
                            "content": getattr(hit, "content", "") or "",
                        }
                        for hit in hits
                    ],
                    content_kind="evidence",
                )
            record_retrieval(
                retriever=retriever_name,
                status="success",
                duration_s=max(0.0, time.monotonic() - started),
                hit_count=len(hits),
            )
            return (
                {
                    "tool_name": call.get("tool_name"),
                    "query": query,
                    "filters": filters,
                    "top_k": top_k,
                    "hit_count": len(hits),
                    "status": "ok",
                    "latency_ms": int((time.monotonic() - started) * 1000),
                },
                [hit.model_dump() for hit in hits],
                None,
            )
        except QueryCancelled:
            record_retrieval(
                retriever=retriever_name,
                status="cancelled",
                duration_s=max(0.0, time.monotonic() - started),
                hit_count=0,
            )
            raise
        except Exception as exc:
            record_retrieval(
                retriever=retriever_name,
                status="failed",
                duration_s=max(0.0, time.monotonic() - started),
                hit_count=0,
            )
            return (
                {
                    "tool_name": call.get("tool_name"),
                    "query": query,
                    "filters": filters,
                    "top_k": top_k,
                    "hit_count": 0,
                    "status": "failed",
                    "error": str(exc),
                    "latency_ms": int((time.monotonic() - started) * 1000),
                },
                [],
                {"tool": call.get("tool_name"), "error": str(exc)},
            )

    if bounded_calls:
        max_workers = min(8, len(bounded_calls))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for call in bounded_calls:
                # Signal the frontend a tool STARTED (spinner) before it finishes.
                _write_stream_event(
                    {
                        "type": "tool_started",
                        "round": round_no,
                        "tool_name": call.get("tool_name"),
                        "query": call.get("query", ""),
                    }
                )
                futures.append(submit_with_current_context(executor, _run_call, call))
            for future in as_completed(futures):
                diagnostic, hits, failure = future.result()
                diagnostics.append(diagnostic)
                evidence.extend(hits)
                if diagnostic.get("tool_name") == "circuit_query":
                    circuit_hits.extend(hits)
                _write_stream_event(
                    {
                        "type": "tool_result",
                        "round": round_no,
                        "tool_name": diagnostic.get("tool_name"),
                        "hit_count": diagnostic.get("hit_count"),
                        "status": diagnostic.get("status"),
                    }
                )
                if failure:
                    state["trace"] = _trace(
                        state,
                        "retrieve_evidence",
                        f"Tool failed: {failure.get('tool')}",
                        {"error": str(failure.get("error") or "")[:300]},
                    )

    if round_no == 1:
        verified_links = list(state.get("_verified_datasheet_links") or [])
        derived_calls = _derived_datasheet_calls(state.get("user_query", ""), circuit_hits, verified_links)
        if not derived_calls:
            # M1 gate: without verified links no supplemental document_rag call
            # is ever issued — even when EDF exposes candidate part numbers.
            if analyze_circuit_question(state.get("user_query", "")).requires_datasheet and circuit_hits:
                diagnostics.append(
                    {
                        "tool_name": "document_rag",
                        "status": "skipped_no_verified_datasheet_link",
                        "hit_count": 0,
                    }
                )
            derived_calls = []
        for call in derived_calls:
            tool = tools.get("document_rag")
            if tool is None:
                break
            started = time.monotonic()
            try:
                run_kwargs = {"top_k": call["top_k"], "filters": dict(call.get("filters") or {})}
                if cancel_check is not None and getattr(tool, "supports_cancellation", False):
                    run_kwargs["should_cancel"] = cancel_check
                with observe.retriever(
                    "hdb.retrieve.ragflow.supplemental",
                    retriever="ragflow",
                    **{
                        "hdb.retrieval.round": round_no,
                        "hdb.retrieval.top_k": call["top_k"],
                    },
                ) as observation:
                    observation.set_input(call["query"], content_kind="query")
                    hits = tool.run(call["query"], state["kb_name"], state.get("_ctx_obj"), **run_kwargs)
                    observation.hit_count(len(hits))
                    observation.set("hdb.retrieval.status", "success")
                    observation.set_content(
                        "retrieval.documents",
                        [
                            {
                                "id": getattr(hit, "id", ""),
                                "source_name": getattr(hit, "source_name", ""),
                                "score": getattr(hit, "score", None),
                                "locator": getattr(hit, "locator", {}) or {},
                                "content_kind": getattr(hit, "content_kind", ""),
                                "content": getattr(hit, "content", "") or "",
                            }
                            for hit in hits
                        ],
                        content_kind="evidence",
                    )
                record_retrieval(
                    retriever="ragflow",
                    status="success",
                    duration_s=max(0.0, time.monotonic() - started),
                    hit_count=len(hits),
                    supplemental=True,
                )
                for hit in hits:
                    payload = hit.model_dump()
                    evidence_kind = "datasheet_claim" if _is_matching_datasheet_capability(
                        hit.source_name, hit.content, call["part_number"]
                    ) else "datasheet_reference"
                    payload["metadata"] = {
                        **(payload.get("metadata") or {}),
                        "evidence_kind": evidence_kind,
                        "derived_from": "circuit_part_number",
                        "part_number": call["part_number"],
                    }
                    evidence.append(payload)
                diagnostics.append(
                    {
                        "tool_name": "document_rag",
                        "query": call["query"],
                        "filters": {},
                        "top_k": call["top_k"],
                        "hit_count": len(hits),
                        "status": "ok",
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "derived_from": "circuit_part_number",
                    }
                )
            except QueryCancelled:
                record_retrieval(
                    retriever="ragflow",
                    status="cancelled",
                    duration_s=max(0.0, time.monotonic() - started),
                    hit_count=0,
                    supplemental=True,
                )
                raise
            except Exception as exc:
                record_retrieval(
                    retriever="ragflow",
                    status="failed",
                    duration_s=max(0.0, time.monotonic() - started),
                    hit_count=0,
                    supplemental=True,
                )
                diagnostics.append(
                    {
                        "tool_name": "document_rag",
                        "query": call["query"],
                        "filters": {},
                        "top_k": call["top_k"],
                        "hit_count": 0,
                        "status": "failed",
                        "derived_from": "circuit_part_number",
                        "error": str(exc),
                        "latency_ms": int((time.monotonic() - started) * 1000),
                    }
                )

    trace_tool_calls = [
        {
            "tool_name": item.get("tool_name"),
            "top_k": item.get("top_k"),
            "hit_count": item.get("hit_count"),
            "status": item.get("status"),
            "scoped": bool(item.get("filters")),
        }
        for item in diagnostics
    ]
    return {
        **state,
        "retrieval_round": round_no,
        "evidence": evidence,
        "retrieval_diagnostics": list(state.get("retrieval_diagnostics") or []) + diagnostics,
        "trace": _trace(
            state,
            "retrieve_evidence",
            "Evidence retrieved",
            {"round": round_no, "total_evidence": len(evidence), "tool_calls": trace_tool_calls},
        ),
    }


def _followup_calls_in_confirmed_scope(state: AgentState, followups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 已废弃：多跳改由 plan_next_retrieval(LLM) 驱动，不再用 recommended_followups。
    # 保留仅为向后兼容；runtime 不再调用。
    scoped_calls = []
    confirmed_calls = [
        tool_call
        for item in (state.get("source_plan") or {}).get("source_plan", [])
        for tool_call in item.get("tool_calls", [])
    ]
    for followup in followups:
        if followup.get("filters"):
            scoped_calls.append(followup)
            continue

        matching_confirmed = [
            call for call in confirmed_calls
            if call.get("tool_name") == followup.get("tool_name") and call.get("filters")
        ]
        if not matching_confirmed:
            scoped_calls.append(followup)
            continue

        for confirmed in matching_confirmed:
            scoped = dict(followup)
            scoped["filters"] = dict(confirmed.get("filters") or {})
            scoped.setdefault("top_k", confirmed.get("top_k") or 5)
            scoped_calls.append(scoped)
    return scoped_calls


def merge_evidence(state: AgentState) -> AgentState:
    seen = set()
    merged = []
    for item in state.get("evidence") or []:
        key = item.get("id") or (item.get("source_name"), item.get("content", "")[:80])
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    merged.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return {
        **state,
        "merged_evidence": merged[:30],
        "trace": _trace(state, "merge_evidence", "Evidence merged", {"merged": len(merged[:30])}),
    }


def _direct_entity_tokens(question: str) -> tuple[set[str], set[str]]:
    text = str(question or "")
    refdes = {
        token.casefold()
        for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d+(?![A-Za-z0-9])", text)
    }
    nets = {
        token.casefold()
        for token in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", text)
    }
    return refdes, nets - refdes


def _has_direct_entity_support(question: str, evidence: dict[str, Any]) -> bool:
    refdes, nets = _direct_entity_tokens(question)
    if not refdes and not nets:
        return True
    searchable = " ".join(
        (
            str(evidence.get("content") or ""),
            str(evidence.get("source_name") or ""),
        )
    ).casefold()
    if refdes:
        return any(token in searchable for token in refdes)
    return any(token in searchable for token in nets)


def score_and_compare_evidence(state: AgentState) -> AgentState:
    analysis = state.get("question_analysis") or {}
    evidence = state.get("merged_evidence") or []
    claims = [Claim.model_validate(item) for item in analysis.get("claims") or []]
    claim_coverage = _claim_coverage(claims, evidence)
    followups = []
    coverage_items = []
    ledger_items = []
    quality_by_id = _score_evidence_quality(state, evidence)
    sources = (state.get("catalog") or {}).get("sources") or []
    diagnostics = state.get("retrieval_diagnostics") or []
    searched_sources_by_kind, searched_tools_by_kind = _searched_scope_by_kind(diagnostics)
    for sq in analysis.get("sub_questions") or []:
        sq_text = sq.get("question", "")
        expected = set(sq.get("expected_evidence") or [])
        sq_tokens = set(_tokens_for_scoring(sq_text))
        supporting = []
        supporting_sources = []
        covered_types = set()
        for item in evidence:
            content = f"{item.get('content', '')} {item.get('source_name', '')}".casefold()
            type_match = not expected or item.get("content_kind") in expected
            token_hits = sum(1 for token in sq_tokens if token in content)
            if type_match and _has_direct_entity_support(sq_text, item) and (token_hits > 0 or not sq_tokens):
                supporting.append(item.get("id", ""))
                if item.get("source_name") and item.get("source_name") not in supporting_sources:
                    supporting_sources.append(item.get("source_name", ""))
                if item.get("content_kind"):
                    covered_types.add(str(item.get("content_kind")))
        score = min(1.0, len(supporting) / 2) if supporting else 0.0
        if score >= 0.75:
            status = "covered"
        elif score >= 0.4:
            status = "partial"
        elif score > 0:
            status = "weak"
        else:
            status = "missing"
            for evidence_type in expected or ["document_text"]:
                if evidence_type == "spreadsheet_table":
                    tool_name = "spreadsheet_semantic"
                elif evidence_type == "circuit_design":
                    tool_name = "circuit_query"
                else:
                    tool_name = "document_rag"
                followups.append(
                    ToolCallPlan(
                        tool_name=tool_name,
                        query=sq_text,
                        reason=f"补查未覆盖子问题: {sq_text}",
                        top_k=8,
                    ).model_dump()
                )
        expected_types = list(expected or {"document_text", "spreadsheet_table", "circuit_design"})
        missing_types = [kind for kind in expected_types if kind not in covered_types]
        relevant_sources = _relevant_sources_for_subquestion(sources, sq, analysis)
        searched_sources = sorted(
            {
                source
                for kind in expected_types
                for source in searched_sources_by_kind.get(kind, set())
                if source
            }
        )
        searched_tools = sorted(
            {
                tool
                for kind in expected_types
                for tool in searched_tools_by_kind.get(kind, set())
                if tool
            }
        )
        unsearched_sources = [
            source.get("document_name", "")
            for source in relevant_sources
            if source.get("document_name")
            and source.get("document_name") not in searched_sources
            and source.get("document_name") not in supporting_sources
        ]
        if status == "covered":
            gap_feedback = ""
        elif unsearched_sources:
            gap_feedback = f"优先补查未覆盖来源: {', '.join(unsearched_sources[:4])}"
        elif missing_types:
            gap_feedback = f"缺少证据类型: {', '.join(missing_types)}"
        else:
            gap_feedback = "已有证据较弱，需要改写查询或扩大检索范围。"
        coverage_items.append(
            CoverageItem(
                sub_question_id=sq.get("id", ""),
                sub_question=sq_text,
                coverage_score=score,
                status=status,
                supporting_evidence_ids=supporting[:6],
                missing=[] if supporting else ["未找到直接支撑证据"],
            ).model_dump()
        )
        ledger_items.append(
            RetrievalLedgerItem(
                sub_question_id=sq.get("id", ""),
                sub_question=sq_text,
                expected_evidence=expected_types,
                status=status,
                searched_tools=searched_tools,
                searched_sources=searched_sources,
                supporting_evidence_ids=supporting[:6],
                supporting_sources=supporting_sources[:6],
                missing_evidence_types=missing_types,
                unsearched_relevant_sources=unsearched_sources[:8],
                gap_feedback=gap_feedback,
            ).model_dump()
        )
    conflicts = reportable_conflicts(evidence, state.get("user_query", ""))
    matrix = CoverageMatrix(coverage=coverage_items, conflicts=conflicts, recommended_followups=followups[:6])
    return {
        **state,
        "claim_coverage": [item.model_dump() for item in claim_coverage],
        "evidence_quality": [quality.model_dump() for quality in quality_by_id.values()],
        "retrieval_ledger": ledger_items,
        "coverage_matrix": matrix.model_dump(),
        "trace": _trace(
            state,
            "score_and_compare_evidence",
            "Coverage scored",
            {
                "followups": len(followups),
                "conflicts": len(conflicts),
                "quality_items": len(quality_by_id),
                "ledger_items": len(ledger_items),
            },
        ),
    }


def _searched_scope_by_kind(diagnostics: list[dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    sources_by_kind: dict[str, set[str]] = {"document_text": set(), "spreadsheet_table": set(), "circuit_design": set()}
    tools_by_kind: dict[str, set[str]] = {"document_text": set(), "spreadsheet_table": set(), "circuit_design": set()}
    for item in diagnostics:
        tool_name = str(item.get("tool_name") or "")
        if tool_name == "document_rag":
            kind = "document_text"
        elif tool_name in {"spreadsheet_semantic", "spreadsheet_cell"}:
            kind = "spreadsheet_table"
        elif tool_name == "circuit_query":
            kind = "circuit_design"
        else:
            continue
        tools_by_kind.setdefault(kind, set()).add(tool_name)
        filters = item.get("filters") or {}
        source_name = str(filters.get("source_name") or "")
        if source_name:
            sources_by_kind.setdefault(kind, set()).add(source_name)
    return sources_by_kind, tools_by_kind


def _relevant_sources_for_subquestion(
    sources: list[dict[str, Any]],
    sub_question: dict[str, Any],
    analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    expected = set(sub_question.get("expected_evidence") or [])
    entities = [str(entity).casefold() for entity in analysis.get("entities", []) if str(entity).strip()]
    result = []
    for source in sources:
        content_kind = str(source.get("content_kind") or "")
        processor = str(source.get("processor_kind") or "")
        if "spreadsheet_table" in expected and processor == "spreadsheet_table":
            result.append(source)
            continue
        if "circuit_design" in expected and processor == "circuit_design":
            result.append(source)
            continue
        if "document_text" in expected and content_kind == "document_text":
            result.append(source)
            continue
        source_text = " ".join(
            str(source.get(field) or "")
            for field in ("document_name", "original_file_name", "source_group")
        ).casefold()
        if any(entity and entity in source_text for entity in entities):
            result.append(source)
    return result


def _tokens_for_scoring(text: str) -> list[str]:
    return tokenize_hardware_query(text, max_tokens=16, include_cjk_ngrams=True)


def _score_evidence_quality(state: AgentState, evidence: list[dict[str, Any]]) -> dict[str, EvidenceQuality]:
    """Score evidence by query relevance, source scope, and evidence type.

    Retriever scores are not comparable across tools in this application.  In
    particular, spreadsheet-cell retrieval historically returned ``1.0`` for
    every hit, which made a generic template instruction look as useful as a
    row containing the requested part number.  Keep the score deterministic
    and make token overlap depend on the evidence body, not its file name.
    """

    boilerplate_markers = (
        "填写说明",
        "template instructions",
        "封面",
        "cover",
        "模板变更历史",
        "template change history",
    )
    analysis = state.get("question_analysis") or {}
    planned_sources = {
        str(item.get("source_name") or "")
        for item in (state.get("source_plan") or {}).get("source_plan", [])
        if item.get("source_name")
    }
    quality_by_id: dict[str, EvidenceQuality] = {}
    for index, item in enumerate(evidence, start=1):
        evidence_id = str(item.get("id") or f"evidence:{index}")
        source_name = str(item.get("source_name") or "")
        content = str(item.get("content") or "").casefold()
        reasons = []
        matched_sub_questions = []
        matched_tokens: set[str] = set()
        evidence_type_match = False
        for sq in analysis.get("sub_questions") or []:
            tokens = _tokens_for_scoring(sq.get("question", ""))
            hits = sum(1 for token in tokens if token in content)
            if hits:
                matched_sub_questions.append(sq.get("id", ""))
                matched_tokens.update(token for token in tokens if token in content)
                expected = set(sq.get("expected_evidence") or [])
                if not expected or item.get("content_kind") in expected:
                    evidence_type_match = True
        token_overlap = len(matched_tokens)
        source_scope_match = (
            not planned_sources
            or source_name in planned_sources
            or any(source_name and source_name in planned_source for planned_source in planned_sources)
        )
        if source_scope_match:
            reasons.append("source_in_confirmed_scope")
        if evidence_type_match:
            reasons.append("evidence_type_matches_question")
        if token_overlap:
            reasons.append("question_tokens_overlap")
        is_boilerplate = any(marker in content for marker in boilerplate_markers)
        if is_boilerplate:
            reasons.append("template_boilerplate_penalty")
        base_score = float(item.get("score") or 0.0)
        quality_score = min(
            1.0,
            max(
                0.0,
                (0.25 if source_scope_match else 0.0)
                + (0.15 if evidence_type_match else 0.0)
                + min(0.50, token_overlap * 0.10)
                + min(0.10, max(0.0, base_score) * 0.10)
                - (0.25 if is_boilerplate and token_overlap == 0 else 0.10 if is_boilerplate else 0.0),
            ),
        )
        quality_by_id[evidence_id] = EvidenceQuality(
            evidence_id=evidence_id,
            score=quality_score,
            source_scope_match=source_scope_match,
            evidence_type_match=evidence_type_match,
            token_overlap=token_overlap,
            matched_sub_questions=matched_sub_questions[:6],
            reasons=reasons,
        )
    return quality_by_id


def _detect_evidence_conflicts(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    value_pattern = re.compile(
        r"(?P<key>[A-Za-z][A-Za-z0-9_ /.-]{1,40})\s*(?:=|:|：)\s*"
        r"(?P<value>[A-Za-z0-9_.+-]+(?:\s?(?:V|A|mA|uF|nF|ohm|Ω|%))?)"
    )
    values_by_key: dict[str, dict[str, set[str]]] = {}
    for item in evidence:
        content = str(item.get("content") or "")
        source_name = str(item.get("source_name") or "")
        for match in value_pattern.finditer(content[:3000]):
            key = " ".join(match.group("key").strip().casefold().split())
            value = match.group("value").strip().casefold()
            if len(key) < 2 or len(value) < 1:
                continue
            values_by_key.setdefault(key, {}).setdefault(value, set()).add(source_name)
    conflicts = []
    for key, values in values_by_key.items():
        if len(values) <= 1:
            continue
        conflicts.append(
            {
                "field": key,
                "values": [
                    {"value": value, "sources": sorted(source for source in sources if source)}
                    for value, sources in sorted(values.items())
                ],
                "reason": "不同证据对同一字段给出了不同取值",
            }
        )
    return conflicts[:10]


def judge_sufficiency(state: AgentState, llm_client: Any | None = None) -> AgentState:
    """对标 Google Agentic RAG 的 Sufficient Context：纯 LLM 判断证据是否够答。

    LLM 看子问题 + 当前证据 + 中间草稿 + 覆盖度/冲突 hint，输出 sufficient / partial /
    insufficient + missing + suggested_queries（多跳的关键）。失败不退规则：LLM 不可用或解析失败
    → 一律判 partial_but_answerable，终止迭代，用已有证据作答。
    """
    round_no = int(state.get("retrieval_round") or 0)
    coverage = state.get("coverage_matrix") or {}
    conflicts = coverage.get("conflicts") or []
    sub_questions = (state.get("question_analysis") or {}).get("sub_questions") or []
    ledger = state.get("retrieval_ledger") or []
    evidence = state.get("merged_evidence") or []
    intermediate_answer = state.get("intermediate_answer") or ""

    def _partial(reason: str, error: str = "") -> AgentState:
        _write_stream_event(
            {"type": "degraded", "stage": "judge_sufficiency", "reason": f"充分性判断降级：{reason}"}
        )
        decision = SufficiencyDecision(
            status="partial_but_answerable",
            reason=reason,
            missing=[],
            suggested_queries=[],
        )
        meta = {"round": round_no, "status": decision.status, "missing": [], "error": error}
        return {
            **state,
            "sufficiency": decision.model_dump(),
            "trace": _trace(state, "judge_sufficiency", reason, meta),
        }

    if llm_client is None:
        return _partial("充分性 LLM 不可用，基于已有证据作答。", error="no_llm_client")

    # 证据摘要：只给 LLM 看 content 片段 + 来源 + 分数，控制 token。
    evidence_brief = [
        {
            "id": item.get("id", ""),
            "source": item.get("source_name", ""),
            "score": item.get("score", 0.0),
            "content": (item.get("content") or "")[:300],
            "content_kind": item.get("content_kind", ""),
        }
        for item in evidence[:20]
    ]
    user_prompt = (
        f"用户问题：{state.get('user_query', '')}\n\n"
        f"子问题：\n{_json_for_prompt(sub_questions, limit=4000)}\n\n"
        f"当前证据：\n{_json_for_prompt(evidence_brief, limit=8000)}\n\n"
        f"中间草稿（由当前证据生成，可能有缺失标记）：\n{intermediate_answer[:4000]}\n\n"
        f"覆盖度 hint（规则计算的辅助信号）：\n{_json_for_prompt(coverage.get('coverage') or [], limit=3000)}\n\n"
        f"检索账本（每个子问题已查来源、未查相关来源、缺失证据类型、gap feedback）：\n"
        f"{_json_for_prompt(ledger, limit=7000)}\n\n"
        f"已检测到的证据冲突：\n{_json_for_prompt(conflicts, limit=2000)}\n\n"
        "请判断中间草稿是否已经被当前证据充分支撑并覆盖所有子问题。"
        "若某个子问题在草稿中缺失，且检索账本显示存在 unsearched_relevant_sources 或 missing_evidence_types，"
        "请返回 insufficient_need_more，并基于对应 gap_feedback 给出 suggested_queries。"
        "请调用 report_sufficiency 工具返回结果。"
    )
    sufficiency_tool = _response_tool(
        "report_sufficiency",
        "Report whether current evidence is sufficient to answer all sub-questions. Call exactly once.",
        {
            "status": {"type": "string", "enum": ["sufficient", "partial_but_answerable", "insufficient_need_more"]},
            "reason": {"type": "string"},
            "missing": {"type": "array", "items": {"type": "string"}},
            "suggested_queries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "tool_name": {"type": "string"},
                        "source_name": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
        ["status", "reason"],
    )
    try:
        payload = _chat_structured_streaming(
            llm_client,
            [
                {"role": "system", "content": SUFFICIENCY_JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            sufficiency_tool,
            "sufficiency_judge",
        )
        status = _normalize_sufficiency_status(payload.get("status"))
        if status not in {"sufficient", "partial_but_answerable", "insufficient_need_more"}:
            return _partial("充分性 LLM 返回状态非法，基于已有证据作答。", error=f"bad_status:{payload.get('status')}")
        suggested = []
        for item in payload.get("suggested_queries") or []:
            if not isinstance(item, dict):
                continue
            q = str(item.get("query") or "").strip()
            if not q:
                continue
            suggested.append(
                {
                    "query": q,
                    "tool_name": str(item.get("tool_name") or "document_rag").strip(),
                    "source_name": str(item.get("source_name") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                }
            )
        decision = SufficiencyDecision(
            status=status,
            reason=str(payload.get("reason") or "").strip() or "LLM 充分性判断完成。",
            missing=[str(m) for m in (payload.get("missing") or []) if str(m).strip()],
            suggested_queries=suggested,
        )
        # 确定性兜底（先查看账本再判断）：「partial_but_answerable」表示有缺口但能答，
        # 若账本显示存在可补查的缺口（未查来源 / 缺失证据类型），升级为 insufficient，
        # 强制进入重新检索，保证「证据不足 → 多跳」这条设计成立。完整的 sufficient 尊重 LLM。
        if decision.status == "partial_but_answerable" and round_no < config.settings.AGENT_MAX_RETRIEVAL_ROUNDS:
            gap_queries = _retrievable_gap_queries(ledger)
            if gap_queries:
                decision = SufficiencyDecision(
                    status="insufficient_need_more",
                    reason="账本显示存在可补查的检索缺口，强制进入下一轮检索。",
                    missing=decision.missing,
                    suggested_queries=gap_queries,
                )
                suggested = gap_queries
        meta = {
            "round": round_no,
            "status": decision.status,
            "missing": decision.missing,
            "suggested_queries": [q.get("query", "") for q in suggested],
        }
        return {
            **state,
            "sufficiency": decision.model_dump(),
            "trace": _trace(state, "judge_sufficiency", decision.reason, meta),
        }
    except Exception as exc:
        return _partial("充分性 LLM 判断失败，基于已有证据作答。", error=str(exc)[:300])


def should_continue(state: AgentState) -> str:
    if state.get("final_response"):
        return "end"
    status = (state.get("sufficiency") or {}).get("status")
    round_no = int(state.get("retrieval_round") or 0)
    # insufficient 且未达轮次上限 → 进入 plan_next_retrieval 产出新调用。
    # 是否真有新调用由 runner 在 plan_next_retrieval 之后判断并决定是否继续。
    if status == "insufficient_need_more" and round_no < config.settings.AGENT_MAX_RETRIEVAL_ROUNDS:
        return "retrieve_more"
    return "answer"


def should_route_to_retrieval(state: AgentState) -> str:
    return "retrieve" if (state.get("route_decision") or {}).get("needs_retrieval", True) else "direct"


def should_retrieve_more_after_planning(state: AgentState) -> str:
    return "retrieve" if state.get("next_retrieval_calls") else "answer"


def _streaming_node(node_name: str, node_func):
    def wrapped(state: AgentState):
        _write_stage_event(node_name, "running")
        return node_func(state)

    return wrapped


def build_multi_source_graph(nodes: dict[str, Any]):
    if StateGraph is None:
        raise RuntimeError("langgraph is not installed; install langgraph to build the compiled agent graph.")
    graph = StateGraph(AgentState)
    graph.add_node("route_query", _streaming_node("route_query", nodes.get("route_query", route_query)))
    graph.add_node(
        "compose_direct_answer",
        _streaming_node("compose_direct_answer", nodes.get("compose_direct_answer", compose_direct_answer)),
    )
    graph.add_node("analyze_question", _streaming_node("analyze_question", nodes.get("analyze_question", analyze_question)))
    graph.add_node("scan_kb_catalog", _streaming_node("scan_kb_catalog", nodes["scan_kb_catalog"]))
    graph.add_node(
        "plan_source_selection",
        _streaming_node("plan_source_selection", nodes.get("plan_source_selection", plan_source_selection)),
    )
    graph.add_node("retrieve_evidence", _streaming_node("retrieve_evidence", nodes["retrieve_evidence"]))
    graph.add_node("merge_evidence", _streaming_node("merge_evidence", merge_evidence))
    graph.add_node("score_and_compare_evidence", _streaming_node("score_and_compare_evidence", score_and_compare_evidence))
    graph.add_node("draft_intermediate_answer", _streaming_node("draft_intermediate_answer", nodes["draft_intermediate_answer"]))
    graph.add_node("judge_sufficiency", _streaming_node("judge_sufficiency", nodes.get("judge_sufficiency", judge_sufficiency)))
    graph.add_node(
        "plan_next_retrieval",
        _streaming_node("plan_next_retrieval", nodes.get("plan_next_retrieval", plan_next_retrieval)),
    )
    graph.add_node("compose_answer", _streaming_node("compose_answer", nodes["compose_answer"]))
    graph.add_node("verify_grounding", _streaming_node("verify_grounding", nodes["verify_grounding"]))

    graph.set_entry_point("route_query")
    graph.add_conditional_edges(
        "route_query",
        should_route_to_retrieval,
        {
            "retrieve": "analyze_question",
            "direct": "compose_direct_answer",
        },
    )
    graph.add_edge("compose_direct_answer", END)
    graph.add_edge("analyze_question", "scan_kb_catalog")
    graph.add_edge("scan_kb_catalog", "plan_source_selection")
    graph.add_edge("plan_source_selection", "retrieve_evidence")
    graph.add_edge("retrieve_evidence", "merge_evidence")
    graph.add_edge("merge_evidence", "score_and_compare_evidence")
    graph.add_edge("score_and_compare_evidence", "draft_intermediate_answer")
    graph.add_edge("draft_intermediate_answer", "judge_sufficiency")
    # 多跳：sufficiency 不足 → plan_next_retrieval（动态重规划）；
    # 如果重规划产出新调用则回到 retrieve_evidence，否则直接基于现有证据作答。
    graph.add_conditional_edges(
        "judge_sufficiency",
        should_continue,
        {
            "retrieve_more": "plan_next_retrieval",
            "answer": "compose_answer",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "plan_next_retrieval",
        should_retrieve_more_after_planning,
        {
            "retrieve": "retrieve_evidence",
            "answer": "compose_answer",
        },
    )
    graph.add_edge("compose_answer", "verify_grounding")
    graph.add_edge("verify_grounding", END)
    return graph.compile()
