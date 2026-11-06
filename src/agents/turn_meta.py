"""Turn-level complexity budget, plan normalization and verification.

Extracted from the runner so the stream orchestration class stays within the
architecture-gate line budget.  Nothing here routes a turn or grants tools; the
helpers only size the loop, synthesize a partial answer from already-registered
evidence, and describe the deterministic/model verification result.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from src import settings
from src.agents.middleware.skill_read import SkillReadGateMiddleware
from src.agents.schemas import Evidence
from src.agents.tools.runtime import compress_evidence_for_context

_CITATION_RE = re.compile(r"\[(\d+)\]")
_CITATION_LABEL_RE = re.compile(
    r"[\[【]\s*(?:K|KB|知识库|A|ATT|附件)\s*[:：]?\s*(\d+)\s*[\]】]",
    re.IGNORECASE,
)


def _scrub_invalid_citations(
    text: str,
    evidence: list[Evidence],
    *,
    add_note: bool = True,
) -> tuple[str, list[int]]:
    """Remove citation numbers that do not exist in this turn's evidence.

    Citations are turn-local.  A follow-up model can otherwise reuse ``[n]``
    from a previous answer while the current turn has no matching evidence,
    producing a dangling marker and a broken citation panel.  The removed
    numbers are returned for the verification summary.
    """

    valid = {
        int(item.metadata.get("citation_number") or 0)
        for item in evidence
        if int(item.metadata.get("citation_number") or 0) > 0
    }
    numbers = {int(value) for value in _CITATION_RE.findall(str(text or ""))}
    invalid = sorted(numbers - valid)
    if not invalid:
        return str(text or ""), []
    cleaned = _CITATION_RE.sub(
        lambda match: match.group(0) if int(match.group(1)) in valid else "",
        str(text or ""),
    )
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned).strip()
    if add_note and cleaned:
        cleaned += (
            f"\n\n（注：已省略 {len(invalid)} 个不在本轮证据中的引用编号；"
            "如需追溯前轮结论，请重新检索后再引用。）"
        )
    return cleaned, invalid


def _normalize_citation_labels(text: str) -> str:
    """Map source-labelled citations to the canonical numeric marker.

    Models sometimes write ``[K6]``/``[A2]``/``【知识库:6】``.  The numeric part
    is the registered citation number; normalizing it keeps verification and
    clickable citation readback working while the source card still carries the
    KB/attachment distinction.
    """

    return _CITATION_LABEL_RE.sub(r"[\1]", str(text or ""))


# These are built-in agent behaviors.  Keep the switches as module constants
# so tests can exercise the fallbacks without exposing deployment knobs.
COMPLEXITY_BUDGET_ENABLED = True
RECURSION_LIMIT_MAX = int(os.getenv("AGENT_RECURSION_LIMIT_MAX", "120") or 120)
PARTIAL_SYNTHESIS_ENABLED = True
PARTIAL_SYNTHESIS_MAX_EVIDENCE_BYTES = int(
    os.getenv("AGENT_PARTIAL_SYNTHESIS_MAX_EVIDENCE_BYTES", "24000") or 24000
)
VERIFIER_ENABLED = True
VERIFIER_MIN_ANSWER_CHARS = int(os.getenv("AGENT_VERIFIER_MIN_ANSWER_CHARS", "600") or 0)
RESEARCH_SUBAGENT_ENABLED = True
PARTIAL_SYNTHESIS_MAX_TOKENS = int(
    os.getenv("AGENT_PARTIAL_SYNTHESIS_MAX_TOKENS", "8192") or 8192
)


_AGENT_ACTIVE = 0
_AGENT_ACTIVE_LOCK = threading.Lock()


def _agent_active(delta: int) -> None:
    """Publish the concurrent agent-run gauge (P2-002)."""

    global _AGENT_ACTIVE
    with _AGENT_ACTIVE_LOCK:
        _AGENT_ACTIVE = max(0, _AGENT_ACTIVE + int(delta))
        current = _AGENT_ACTIVE
    try:
        from src.observability.metrics import set_active_state

        set_active_state("hdb.agent.inflight_requests", "Concurrent agent runs", current)
    except Exception:
        pass


@dataclass
class TokenUsageSummary:
    """Aggregated token usage across all model calls of one stream."""

    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    max_prompt_tokens: int = 0

    @property
    def has_usage(self) -> bool:
        return self.call_count > 0


def _accumulate_usage(usage: TokenUsageSummary, chunk: Any) -> None:
    meta = getattr(chunk, "usage_metadata", None)
    if not meta:
        return
    usage.call_count += 1
    input_tokens = int(meta.get("input_tokens") or 0)
    usage.prompt_tokens += input_tokens
    usage.max_prompt_tokens = max(usage.max_prompt_tokens, input_tokens)
    usage.completion_tokens += int(meta.get("output_tokens") or 0)
    usage.total_tokens += int(meta.get("total_tokens") or 0)


def _extract_text_delta(chunk: Any) -> str:
    """Read plain text from an AIMessage-like object (local copy)."""

    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("text", "text_delta"):
                    parts.append(str(block.get("text") or block.get("delta") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


_RESEARCH_SUBAGENT_TOOLS = frozenset({
    "list_kb_sources",
    "list_knowledge_sources",
    "knowledge_search",
    "evidence_read",
    "session_recall",
    "document_search",
    "document_search_batch",
    "circuit_search",
    "spreadsheet_row_search",
    "spreadsheet_cell_lookup",
    "spreadsheet_schema_lookup",
    "spreadsheet_sql_query",
    "conversation_search",
    "attachment_list",
    "attachment_search",
    "attachment_read",
    "attachment_table_query",
    "attachment_circuit_search",
    "attachment_visual_analyze",
    "memory_search",
    "wiki_search",
})

_HARDWARE_REFDES_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:U|X|R|C|L|D|Q|J|TP|CN)\d{2,5}(?:-\d+)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_HARDWARE_TABLE_RE = re.compile(
    r"(?i)\bTable\s*\d+(?:-\d+)?\b|表\s*\d+(?:-\d+)?|第\s*\d+\s*[章节]"
)
_HARDWARE_TERMS = (
    "位号", "网表", "原理图", "连接器", "引脚", "管脚", "封装", "料号",
    "寄存器", "阈值", "电源采样", "拆除", "差分", "总线", "信号名",
    "表号", "WCCA", "BOM", "车规", "AEC", "ASIL", "PMIC", "ADC",
    "MCU", "SoC", "PHY", "CAN FD", "CAN0", "CAN1", "以太网", "电源",
    "采样", "复位", "时钟", "接口", "时序", "最小系统", "器件", "型号",
)
_HARDWARE_EVIDENCE_BUDGET = int(
    os.getenv("AGENT_HARDWARE_EVIDENCE_BUDGET", "80") or 80
)


def _hardware_profile(query: str) -> dict[str, Any]:
    """Detect hardware-engineering questions from identifiers and table refs."""

    text = str(query or "")
    refs = sorted({
        match.group(0).upper()
        for match in _HARDWARE_REFDES_RE.finditer(text)
    })
    tables = sorted({match.group(0) for match in _HARDWARE_TABLE_RE.finditer(text)})
    terms = [term for term in _HARDWARE_TERMS if term.lower() in text.lower()]
    is_hardware = bool(refs or tables or len(terms) >= 2)
    return {
        "is_hardware": is_hardware,
        "refdes": refs[:12],
        "tables": tables[:6],
        "terms": terms[:10],
        "score": min(8, len(refs) + len(tables) * 2 + max(0, len(terms) - 1)),
    }


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", "") or getattr(tool, "__name__", "")).strip()


def _complex_task_prompt(system_prompt: str, *, has_subagents: bool = False) -> str:
    """Append the deep-task delegation and hardware-search nudge to the prompt."""

    prompt = system_prompt.rstrip()
    if has_subagents:
        prompt += (
            "\n当检索面很宽（多库、多附件、表格/电路/文档混合）时，可将大范围证据收集"
            "委派给 task 的 research 子代理；要求它返回带 [n] 引用的简明事实摘要，"
            "主线程负责比较、决策和写作。"
        )
    prompt += (
        "\n## 硬件工程检索约束\n"
        "问题中出现位号、连接器、表号、信号名、寄存器或数值阈值时，先用精确查询"
        "（如位号、X 管脚、Table 编号、单元格）逐项核对，不要只做宽泛语义检索。"
        "如果只检索到“Table X”标题或引导句，必须继续用表号+列名/寄存器名读取表体；"
        "寄存器/阈值/配置类问题优先搜索“Table 编号 + 芯片名 + Configuration”，"
        "并把预算优先分配给表体/边界单元格而不是背景资料。"
        "表格行必须回读 min/max、上下限、状态等边界单元格；不同来源阈值或型号冲突时，"
        "同时列出两套数值及来源，不要静默选一个。"
    )
    return prompt


def _turn_complexity(
    *,
    query: str,
    query_mode: str,
    attachment_refs: list[Any] | None,
    knowledge_scope: Any | None,
) -> dict[str, Any]:
    """Score the *shape* of the request, not its topic.

    The score only sizes the agent-loop budget and decides whether optional
    quality machinery (planner/verifier/research subagent) is offered.  It
    never routes the request or selects a domain tool.
    """

    text = str(query or "")
    score = 0
    signals: list[str] = []
    length = len(text)
    if length >= 240:
        score += 1
        signals.append("long_query")
    if length >= 600:
        score += 1
    if length >= 1200:
        score += 1
    numbered = len(
        re.findall(r"(?m)^\s*(?:\d{1,2}[.、)]|[-*•])\s+\S", text)
    )
    if numbered >= 3:
        score += min(4, numbered // 2)
        signals.append(f"multi_part:{numbered}")
    deliverables = len(
        re.findall(
            r"必须|至少|要求|输出|列出|给出|表格|对比|风险|引用|章节|步骤|待确认",
            text,
        )
    )
    if deliverables >= 5:
        score += 2
        signals.append("explicit_deliverables")
    if deliverables >= 10:
        score += 1
    attachments = len(attachment_refs or [])
    if attachments:
        score += min(4, attachments * 2)
        signals.append(f"attachments:{attachments}")
    kb_count = len(getattr(knowledge_scope, "sources", []) or []) if knowledge_scope else 0
    if kb_count > 1:
        score += min(4, (kb_count - 1) * 2)
        signals.append(f"multi_kb:{kb_count}")
    if attachments and kb_count:
        score += 2
        signals.append("mixed_sources")
    hardware = _hardware_profile(text)
    if hardware["is_hardware"]:
        # Hardware questions are often short but multi-hop: exact refdes,
        # connector pins, table cells and thresholds all have to line up.
        score += 7 + min(4, int(hardware["score"]))
        signals.append("hardware_question")
    if query_mode == "fast":
        score = min(score, 6)
    level = "simple" if score <= 2 else "moderate" if score <= 6 else "complex"
    return {
        "score": score,
        "level": level,
        "signals": signals,
        "query_chars": length,
        "numbered_items": numbered,
        "attachments": attachments,
        "kb_count": kb_count,
        "hardware": hardware,
    }


# 用户在问题里声明的检索工具调用上限："最多 8 次检索工具调用" → 8。
# 关键词 lookahead 保证只在检索/调用语境匹配，避免"最多列出三行"这类误伤。
_USER_TOOL_CALL_CAP_RE = re.compile(
    r"(?:最多|不超过|至多|不要超过)\s*(\d{1,3})\s*次"
    r"(?=[^\n。!？;；]{0,12}(?:检索|工具|调用|查询))"
)


def parse_user_tool_call_cap(query: str) -> int | None:
    """Parse the per-turn tool-call cap declared by the user, if any.

    Returns ``None`` when the query does not declare a usable cap. Caps below
    1 are ignored and caps at or above the deep-mode base (72) are treated as
    "no extra constraint" so the regular budget stays authoritative.
    """

    match = _USER_TOOL_CALL_CAP_RE.search(str(query or ""))
    if match is None:
        return None
    try:
        cap = int(match.group(1))
    except ValueError:
        return None
    if cap < 1 or cap >= 72:
        return None
    return cap


def _agent_recursion_limit(
    *,
    query: str,
    query_mode: str,
    attachment_refs: list[Any] | None,
    knowledge_scope: Any | None,
) -> tuple[int, dict[str, Any]]:
    """Return the hard loop limit plus a persisted budget explanation."""

    rounds = max(1, int(settings.AGENT_MAX_RETRIEVAL_ROUNDS))
    if query_mode == "deep":
        base = max(72, rounds * 24)
    else:
        base = max(24, rounds * 8)
    complexity = _turn_complexity(
        query=query,
        query_mode=query_mode,
        attachment_refs=attachment_refs,
        knowledge_scope=knowledge_scope,
    )
    enabled = COMPLEXITY_BUDGET_ENABLED
    max_limit = max(base, RECURSION_LIMIT_MAX)
    if enabled:
        multiplier = 8 if query_mode == "deep" else 3
        limit = min(max_limit, base + int(complexity["score"]) * multiplier)
    else:
        limit = base
    budget: dict[str, Any] = {
        "mode": query_mode,
        "base_recursion_limit": base,
        "recursion_limit": limit,
        "complexity": complexity,
        "enabled": enabled,
    }
    cap = parse_user_tool_call_cap(query)
    if cap is not None:
        budget["tool_call_cap"] = cap
        budget["tool_call_cap_source"] = "user_query"
    return limit, budget


def _research_subagent_spec(
    *,
    tools: list[Any],
    model: Any,
    skill_root: str | None = None,
    skill_read_observer: Callable[..., None] | None = None,
) -> dict[str, Any] | None:
    """Build a read-only research subagent for delegated source exploration."""

    sub_tools = [tool for tool in tools if _tool_name(tool) in _RESEARCH_SUBAGENT_TOOLS]
    if not sub_tools:
        return None
    return {
        "name": "research",
        "description": (
            "Read-only retrieval specialist. Delegate broad or multi-source "
            "evidence gathering (knowledge bases, attachments, spreadsheets, "
            "circuits, prior conversations) when the main thread would "
            "otherwise spend many tool calls exploring."
        ),
        "system_prompt": (
            "You are a read-only evidence retrieval specialist. Use only the "
            "provided search/read tools. Do not write, edit, release, or create "
            "documents. Return a compact evidence digest: source name, locator, "
            "key facts, and the exact [n] citation numbers from tool output. "
            "State gaps explicitly. Never invent facts or citations."
        ),
        "tools": sub_tools,
        "model": model,
        "middleware": [
            # Subagents inherit the backend; keep read_file gated the same way.
            SkillReadGateMiddleware(
                enabled=bool(skill_root),
                observer=skill_read_observer,
            )
        ],
    }


def _general_subagent_spec(
    *,
    tools: list[Any],
    model: Any,
    skill_root: str | None = None,
    skill_read_observer: Callable[..., None] | None = None,
) -> dict[str, Any] | None:
    """Build a general-purpose subagent for complex, non-code workspace tasks."""

    if not tools:
        return None
    return {
        "name": "general-purpose",
        "description": (
            "General hardware workspace specialist. Delegate self-contained "
            "multi-step tasks that need file/workspace tools, attachments, "
            "knowledge-base retrieval, spreadsheets, or MCP tools. Do not "
            "delegate tasks that require user confirmation or shell/code."
        ),
        "system_prompt": (
            "You are a general-purpose hardware workspace subagent. Complete "
            "the delegated task end-to-end using only the provided tools. Do "
            "not run shell or arbitrary code, and do not access paths outside "
            "the workspace. Read attachments/workspace files before editing "
            "them. If a write tool returns proposed, report that it is pending "
            "user confirmation; never claim a file was created. Return a "
            "concise result with facts, filenames, and any remaining gaps; do "
            "not invent tool results."
        ),
        "tools": list(tools),
        "model": model,
        "middleware": [
            SkillReadGateMiddleware(
                enabled=bool(skill_root),
                observer=skill_read_observer,
            )
        ],
    }


def _agent_step_budget_answer(rt: Any, reason: str = "") -> str:
    """Explain a clean stop after a budgeted agent loop ended."""

    evidence = list(getattr(rt, "evidence", []) or [])
    sources = [
        name
        for name in dict.fromkeys(
            str(getattr(item, "source_name", "") or "").strip() for item in evidence
        )
        if name
    ]
    if reason == "time_limit":
        lines = ["本轮执行时间预算已到，为避免无限等待已停止继续检索。"]
    elif reason == "no_progress" and not evidence:
        queries = [
            str(item.get("query") or "").strip()
            for item in (getattr(rt, "queries", []) or [])
            if str(item.get("query") or "").strip()
        ]
        tools = [
            str(item.get("tool_name") or "").strip()
            for item in (getattr(rt, "diagnostics", []) or [])
            if str(item.get("tool_name") or "").strip()
        ]
        lines = ["未找到足够证据回答该问题。"]
        if queries:
            lines.append("已查关键词：" + "、".join(dict.fromkeys(queries))[:500])
        if tools:
            lines.append("已用来源：" + "、".join(dict.fromkeys(tools))[:300])
        lines.append("如果该问题本应有资料，请补充更精确的文档名、位号、表名或字段名后重试。")
    elif reason == "no_progress":
        lines = ["连续多次检索没有带来新证据，已停止继续检索。"]
    elif reason == "tool_call_limit":
        lines = ["本轮工具调用已达到你指定的次数上限，已停止继续检索。"]
    else:
        lines = ["本轮工具调用已达到单轮步数上限，为避免无限循环已停止继续检索。"]
    if evidence and sources:
        listed = "、".join(sources[:5]) + (" 等" if len(sources) > 5 else "")
        lines.append(f"已登记 {len(evidence)} 条证据（来源：{listed}），但尚未完成全部任务。")
    elif evidence:
        lines.append(f"已登记 {len(evidence)} 条证据，但尚未完成全部任务。")
    else:
        lines.append("本轮尚未登记可用证据。")
    lines.append(
        "请缩小任务范围后重试：例如直接指明目标章节标题/编号、要读取的附件序号范围，"
        "或先让系统只完成其中一个小节。"
    )
    return "\n".join(lines)


def _is_incomplete_answer(
    answer_text: str,
    *,
    show_turn_meta: bool,
) -> bool:
    """Detect a complex turn whose final text is only an intro/prefix.

    A 300-char uncited preface for a plan with five completed steps is not a
    deliverable; treating it as an empty answer lets the bounded synthesizer
    produce the actual partial report instead of persisting a truncated turn.
    """

    text = str(answer_text or "").strip()
    return bool(
        text
        and show_turn_meta
        and len(text) < 600
        and not _CITATION_RE.search(text)
    )


def _empty_answer_delivery(
    *,
    model: Any,
    query: str,
    evidence: list[Evidence],
    emit_event: Callable[[dict], None],
    budget_state: dict[str, Any],
) -> tuple[str, bool]:
    """Synthesize (or deterministically digest) an answer when the loop ended
    after tool calls without a final text message."""

    synthesized = _synthesize_partial_answer(
        model=model,
        query=query,
        evidence=evidence,
        partial_answer="",
        emit_event=emit_event,
        budget_state=budget_state,
    )
    if synthesized:
        budget_state.update(
            {
                "status": "partial",
                "partial_answer": True,
                "synthesis": "model",
                "stopped_reason": "empty_answer",
            }
        )
        emit_event(
            {
                "type": "partial_result",
                "payload": {
                    "reason": "empty_answer",
                    "synthesized": True,
                    "evidence_count": len(evidence),
                    "answer_chars": len(synthesized),
                },
            }
        )
        return synthesized, True
    digest = _evidence_digest(evidence)
    if digest:
        budget_state.update(
            {
                "status": "partial",
                "partial_answer": True,
                "synthesis": "digest",
                "stopped_reason": "empty_answer",
            }
        )
        emit_event(
            {
                "type": "partial_result",
                "payload": {
                    "reason": "empty_answer",
                    "synthesized": False,
                    "fallback": "evidence_digest",
                    "evidence_count": len(evidence),
                    "answer_chars": len(digest),
                },
            }
        )
        return digest, True
    return "", False


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _evidence_digest(evidence: list[Evidence], *, max_items: int = 16) -> str:
    """Deterministic fail-soft delivery when model synthesis is unavailable."""

    rows: list[str] = []
    for item in evidence:
        number = int(item.metadata.get("citation_number") or 0)
        if number <= 0:
            continue
        locator = ", ".join(
            f"{key}={value}"
            for key, value in (item.locator or {}).items()
            if value not in (None, "", [], {})
        )
        body = " ".join(str(item.content or "").split())[:280]
        rows.append(
            f"- [{number}] {item.source_name}"
            + (f" · {locator}" if locator else "")
            + (f"：{body}" if body else "")
        )
        if len(rows) >= max_items:
            break
    if not rows:
        return ""
    return (
        "本轮未生成完整回答，以下是已登记证据的摘要，可直接用于继续分析：\n\n"
        + "\n".join(rows)
        + "\n\n如需完整结论，可点击“继续完成”，或缩小到某一类器件/章节后重试。"
    )


def _synthesize_partial_answer(
    *,
    model: Any,
    query: str,
    evidence: list[Evidence],
    partial_answer: str,
    emit_event: Callable[[dict], None],
    budget_state: dict[str, Any] | None = None,
) -> str | None:
    """Write a bounded answer from evidence gathered before the hard stop.

    This is deliberately a single non-tool model call (with one shorter retry):
    the agent loop is out of budget, so re-entering it would just repeat the
    failure.  Every block keeps its registered citation number; the model is
    told to cite only those.
    """

    if not evidence or not hasattr(model, "invoke"):
        return None
    synthesis_model = model
    if hasattr(model, "bind"):
        try:
            synthesis_model = model.bind(max_tokens=PARTIAL_SYNTHESIS_MAX_TOKENS)
        except Exception:
            synthesis_model = model
    max_bytes = max(2000, PARTIAL_SYNTHESIS_MAX_EVIDENCE_BYTES)
    draft = str(partial_answer or "").strip()
    last_error = ""

    for attempt in range(2):
        attempt_bytes = min(max_bytes, 12000) if attempt == 0 else 6000
        evidence_context = compress_evidence_for_context(
            evidence, max_bytes=attempt_bytes
        )
        compressed_bytes = len(evidence_context.encode("utf-8"))
        if attempt == 0:
            if budget_state is not None:
                budget_state["context_compressed"] = {
                    "evidence_count": len(evidence),
                    "compressed_bytes": compressed_bytes,
                    "max_bytes": attempt_bytes,
                }
            emit_event(
                {
                    "type": "evidence_compressed",
                    "payload": {
                        "stage": "partial_synthesis",
                        "evidence_count": len(evidence),
                        "compressed_bytes": compressed_bytes,
                        "max_bytes": attempt_bytes,
                        "note": "本轮进入收尾，已登记证据被压缩为有界上下文。",
                    },
                }
            )
        query_limit = 6000 if attempt == 0 else 3000
        prompt = (
            "本轮 agent 工具调用预算已耗尽。请只使用下面已登记的证据，完成一个"
            "“部分结果”回答，而不是解释预算或要求用户重新提问。\n\n"
            f"用户原始任务：\n{str(query or '')[:query_limit]}\n\n"
            + (f"已经流出的草稿（可修订，不要原样重复无关内容）：\n{draft[:4000]}\n\n" if draft else "")
            + "要求：\n"
            "1. 先给出当前能确认的结论/表格/清单；每一条事实必须使用证据中的 [n] 引用。\n"
            "2. 明确标注“已完成部分”和“尚未完成/证据不足”的部分；不要编造型号、参数或引用。\n"
            "3. 如果用户要求多个类别/章节，按用户结构输出；缺失项写“待确认”。\n"
            "4. 结尾给出 2-4 条可执行的下一步（例如继续检索某章节、补充某来源）。\n"
            "5. 直接输出最终 Markdown；不要调用任何工具，不要输出思考过程或工具调用计划。\n"
            "6. 总长度控制在 1800 字以内，优先保留结论和表格。\n\n"
            f"已登记证据（只可引用这些编号）：\n{evidence_context}"
        )
        try:
            response = synthesis_model.invoke(
                [
                    SystemMessage(
                        content=(
                            "你是知识库助手。当前只能基于给定证据写部分交付；"
                            "引用必须使用证据块中的编号。"
                        )
                    ),
                    HumanMessage(content=prompt),
                ]
            )
            text = _extract_text_delta(response).strip()
        except Exception as exc:  # noqa: BLE001 - fail-soft, digest fallback follows
            last_error = str(exc)[:300]
            continue
        if not text:
            metadata = getattr(response, "response_metadata", None) or {}
            finish_reason = metadata.get("finish_reason") if isinstance(metadata, dict) else None
            tool_calls = getattr(response, "tool_calls", None) or []
            last_error = (
                "empty model response"
                f" (finish_reason={finish_reason or 'unknown'}, tool_calls={len(tool_calls)})"
            )
            continue
        # The synthesizer must not introduce citations that were never registered.
        valid_numbers = {
            int(item.metadata.get("citation_number") or 0)
            for item in evidence
            if int(item.metadata.get("citation_number") or 0) > 0
        }
        invalid = {int(num) for num in _CITATION_RE.findall(text)} - valid_numbers
        if invalid:
            for num in sorted(invalid):
                text = text.replace(f"[{num}]", "")
            text = text.rstrip() + "\n\n（注：已移除未登记证据的引用编号。）"
        metadata = getattr(response, "response_metadata", None) or {}
        if isinstance(metadata, dict) and metadata.get("finish_reason") == "length":
            text = text.rstrip() + "\n\n（注：收尾输出达到长度上限，部分表格/结论可能被截断。）"
        return _normalize_citation_labels(text.strip())

    if budget_state is not None:
        budget_state["synthesis_error"] = last_error or "synthesis unavailable"
    emit_event(
        {
            "type": "partial_synthesis_failed",
            "payload": {
                "reason": "synthesis_failed",
                "error": last_error or "synthesis unavailable",
                "evidence_count": len(evidence),
            },
        }
    )
    return None


def _semantic_verification(
    *,
    model: Any,
    query: str,
    answer_text: str,
    evidence: list[Evidence],
    complexity: dict[str, Any],
    budget_exhausted: bool,
    cited_numbers: set[int] | None = None,
) -> dict[str, Any] | None:
    """Run one separate rubric call; fail-soft to ``unknown``.

    This is a quality signal, not an authority claim: the UI labels it as a
    model check, and the deterministic checks remain visible alongside it.
    """

    if not VERIFIER_ENABLED:
        return None
    if not hasattr(model, "invoke") or not evidence:
        return None
    if (
        not budget_exhausted
        and str(complexity.get("level") or "simple") != "complex"
    ):
        return None
    min_chars = max(0, VERIFIER_MIN_ANSWER_CHARS)
    if len(str(answer_text or "").strip()) < min_chars:
        return None
    verifier_model = model
    if hasattr(model, "bind"):
        try:
            verifier_model = model.bind(max_tokens=4096)
        except Exception:
            verifier_model = model
    # The verifier must see the evidence the answer actually cites, otherwise a
    # correct value read from a late table row looks fabricated once the
    # compression budget drops that row.  Cited items go first and receive the
    # intact-content slot for their source.
    cited = {int(number) for number in (cited_numbers or set())}
    ordered_evidence = sorted(
        evidence,
        key=lambda item: (
            0
            if int(item.metadata.get("citation_number") or 0) in cited
            else 1
        ),
    )
    is_hardware = _hardware_profile(query)["is_hardware"]
    parsed: dict[str, Any] | None = None
    last_error = ""
    for attempt in range(2):
        evidence_bytes = (16000 if is_hardware else 8000) if attempt == 0 else 8000
        evidence_context = compress_evidence_for_context(
            ordered_evidence, max_bytes=evidence_bytes
        )
        answer_limit = 8000 if attempt == 0 else 6000
        prompt = (
            "你是独立的交付校验器。请根据用户任务、最终回答和证据清单，判断回答"
            "是否满足任务的结构与证据要求。不要评价文风，不要补写答案。\n\n"
            f"用户任务：\n{str(query or '')[:5000]}\n\n"
            f"最终回答：\n{str(answer_text or '')[:answer_limit]}\n\n"
            f"证据清单：\n{evidence_context}\n\n"
            "只输出一个 JSON 对象，不要代码围栏，不要额外解释：\n"
            "{\n"
            '  "status": "pass|warn|fail",\n'
            '  "checks": [{"key":"task_completion|evidence_grounding|gap_disclosure|budget_disclosure",'
            '"label":"中文短标签","status":"pass|warn|fail|unknown","detail":"不超过120字"}],\n'
            '  "gaps": ["具体缺口，最多5条"]\n'
            "}\n"
            "判断要点：\n"
            "- task_completion：用户明确要求的交付物/结构是否覆盖；未覆盖就 warn/fail。\n"
            "- evidence_grounding：关键结论是否都有有效 [n] 引用；是否出现证据中不存在的器件/参数。\n"
            "- gap_disclosure：资料不足处是否明确写“待确认/证据不足”。\n"
            "- budget_disclosure：如果是部分结果，是否说明了已完成和未完成部分。\n"
            "- 如果是故障注入/拆除题，必须核对拆除对象和受影响管脚是否与证据一致；"
            "仅凭拓扑猜测拆除对象应判 fail。\n"
            "不要因为回答较长就判 pass；也不要因为个别措辞判 fail。"
        )
        if attempt:
            prompt += "\n\n上一次输出无法解析为 JSON。这一次必须只输出一个合法 JSON 对象。"
        try:
            response = verifier_model.invoke(
                [
                    SystemMessage(
                        content=(
                            "你只输出结构化校验 JSON。不得输出隐藏推理、工具计划或与 JSON 无关的文本。"
                        )
                    ),
                    HumanMessage(content=prompt),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - verification must never fail a turn
            last_error = str(exc)[:200]
            continue
        parsed = _parse_json_object(_extract_text_delta(response))
        if parsed is not None:
            break
        metadata = getattr(response, "response_metadata", None) or {}
        finish_reason = metadata.get("finish_reason") if isinstance(metadata, dict) else None
        last_error = (
            "unparseable verifier output"
            f" (finish_reason={finish_reason or 'unknown'},"
            f" chars={len(_extract_text_delta(response))})"
        )
    if parsed is None:
        return {"status": "unknown", "checks": [], "gaps": [], "error": last_error or "verifier unavailable"}

    allowed = {"pass", "warn", "fail", "unknown"}
    status = str(parsed.get("status") or "unknown").strip().lower()
    if status not in allowed:
        status = "unknown"
    checks: list[dict[str, str]] = []
    raw_checks = parsed.get("checks")
    if isinstance(raw_checks, list):
        for raw in raw_checks[:8]:
            if not isinstance(raw, dict):
                continue
            check_status = str(raw.get("status") or "unknown").strip().lower()
            if check_status not in allowed:
                check_status = "unknown"
            key = str(raw.get("key") or "").strip()[:80]
            label = str(raw.get("label") or key or "语义校验").strip()[:80]
            if not key:
                key = f"semantic_{len(checks) + 1}"
            checks.append(
                {
                    "key": key,
                    "label": label,
                    "status": check_status,
                    "detail": str(raw.get("detail") or "").strip()[:200],
                    "source": "model_rubric",
                }
            )
    gaps: list[str] = []
    raw_gaps = parsed.get("gaps")
    if isinstance(raw_gaps, list):
        gaps = [str(item).strip()[:240] for item in raw_gaps if str(item).strip()][:5]
    return {"status": status, "checks": checks, "gaps": gaps, "source": "model_rubric"}


def _verification_check(
    key: str,
    label: str,
    status: str,
    detail: str = "",
    *,
    source: str = "deterministic",
) -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "source": source,
    }


def _build_turn_verification(
    *,
    answer_text: str,
    evidence: list[Evidence],
    query: str,
    model: Any,
    complexity: dict[str, Any],
    budget_exhausted: bool,
    require_evidence: bool,
    evidence_gaps: list[str] | None = None,
    partial_delivery: bool = False,
) -> dict[str, Any]:
    """Combine deterministic citation checks with an optional rubric call."""

    lexical = _build_citation_verification(answer_text, evidence)
    checks: list[dict[str, str]] = []
    gaps: list[str] = []
    answer = str(answer_text or "").strip()
    checks.append(
        _verification_check(
            "answer_present",
            "回答非空",
            "pass" if answer else "fail",
            f"{len(answer)} 字" if answer else "未生成回答",
        )
    )
    if require_evidence:
        checks.append(
            _verification_check(
                "evidence_present",
                "检索到证据",
                "pass" if evidence else "warn",
                f"{len(evidence)} 条证据" if evidence else "本轮未登记证据",
            )
        )
    citations = {int(num) for num in _CITATION_RE.findall(answer)}
    by_number = {
        int(item.metadata.get("citation_number") or 0): item
        for item in evidence
        if int(item.metadata.get("citation_number") or 0) > 0
    }
    valid_citations = citations & set(by_number)
    invalid_citations = sorted(citations - set(by_number))
    if require_evidence and evidence:
        checks.append(
            _verification_check(
                "citations_present",
                "结论带引用",
                "pass" if valid_citations else "fail",
                f"{len(valid_citations)} 个有效引用" if valid_citations else "回答未引用任何证据",
            )
        )
        checks.append(
            _verification_check(
                "citations_valid",
                "引用编号有效",
                "pass" if not invalid_citations else "fail",
                f"无效编号 {invalid_citations[:8]}" if invalid_citations else "全部引用可解析",
            )
        )
        attachment_numbers = {
            number
            for number, item in by_number.items()
            if (
                item.metadata.get("attachment_id")
                or item.metadata.get("source_scope") == "attachment"
                or str(item.metadata.get("source_type") or "").lower() == "attachment"
                or (item.locator or {}).get("attachment_id")
            )
        }
        kb_numbers = set(by_number) - attachment_numbers
        if (
            attachment_numbers
            and kb_numbers
            and re.search(r"附件|知识库|对比|区分|交叉", query or "")
        ):
            cited_attachment = bool(valid_citations & attachment_numbers)
            cited_kb = bool(valid_citations & kb_numbers)
            checks.append(
                _verification_check(
                    "source_separation",
                    "附件与知识库来源区分",
                    "pass" if cited_attachment and cited_kb else "warn",
                    (
                        f"附件引用 {len(valid_citations & attachment_numbers)} 个，"
                        f"知识库引用 {len(valid_citations & kb_numbers)} 个"
                    ),
                )
            )
    substantive_uncited = [
        paragraph
        for paragraph in re.split(r"\n\s*\n", answer)
        if len(paragraph.strip()) >= 40
        and not paragraph.lstrip().startswith(("#", "|", "```"))
        and not _CITATION_RE.search(paragraph)
    ]
    if substantive_uncited:
        checks.append(
            _verification_check(
                "uncited_paragraphs",
                "段落引用覆盖",
                "warn",
                f"{len(substantive_uncited)} 个实质段落没有引用编号",
            )
        )
    hardware = _hardware_profile(query)
    required_entities = [*hardware["refdes"], *hardware["tables"]]
    if required_entities:
        missing_entities = [
            entity for entity in required_entities
            if entity.lower() not in answer.lower()
        ]
        checks.append(
            _verification_check(
                "hardware_entities",
                "工程实体覆盖",
                "warn" if missing_entities else "pass",
                (
                    f"未在回答中覆盖：{'、'.join(missing_entities[:6])}"
                    if missing_entities else "问题中的位号/表号均已覆盖"
                ),
            )
        )
        if missing_entities:
            gaps.append(
                "问题中的工程实体未在回答中覆盖："
                + "、".join(missing_entities[:6])
            )
    if "拆除" in query and hardware["refdes"]:
        query_refs = set(hardware["refdes"])
        removal_sentences = [
            sentence for sentence in re.split(r"[。\n；;]", answer)
            if "拆除" in sentence
        ]
        removal_refs = {
            match.group(0).upper()
            for sentence in removal_sentences
            for match in _HARDWARE_REFDES_RE.finditer(sentence)
        }
        if removal_refs and not (removal_refs & query_refs):
            checks.append(
                _verification_check(
                    "removal_target",
                    "拆除对象",
                    "fail",
                    f"问题指定 {'、'.join(sorted(query_refs))}，"
                    f"回答拆除 {'、'.join(sorted(removal_refs))}",
                )
            )
            gaps.append("拆除对象与问题指定的位号不一致，需人工复核。")
    checks.append(
        _verification_check(
            "budget",
            "执行预算",
            "warn" if (budget_exhausted or partial_delivery) else "pass",
            (
                "工具步数预算耗尽，当前为部分交付"
                if budget_exhausted
                else "本轮为部分交付（未生成完整回答）"
                if partial_delivery
                else "未触发步数上限"
            ),
        )
    )
    if evidence_gaps:
        gaps.extend(evidence_gaps[:6])
        checks.append(
            _verification_check(
                "evidence_requirements",
                "目标证据覆盖",
                "warn",
                f"{len(evidence_gaps)} 个目标仍未满足",
            )
        )
    if budget_exhausted:
        gaps.append("本轮工具步数预算耗尽，复杂任务只完成了可交付部分。")
    elif partial_delivery:
        gaps.append("本轮未生成完整回答，当前结果由已登记证据收尾生成。")
    plan = VERIFIER_ENABLED
    semantic = _semantic_verification(
        model=model,
        query=query,
        answer_text=answer,
        evidence=evidence,
        complexity=complexity,
        budget_exhausted=budget_exhausted,
    ) if plan else None
    if semantic:
        semantic_checks = semantic.get("checks") or []
        if semantic_checks:
            checks.extend(semantic_checks)
        elif semantic.get("status") in {"warn", "fail"}:
            checks.append(
                _verification_check(
                    "semantic_verifier",
                    "独立模型校验",
                    semantic["status"],
                    "模型未给出逐项检查，仅返回总体结论",
                    source="model_rubric",
                )
            )
        gaps.extend(semantic.get("gaps") or [])
        if semantic.get("error"):
            checks.append(
                _verification_check(
                    "semantic_verifier",
                    "独立模型校验",
                    "unknown",
                    str(semantic["error"])[:160],
                )
            )
    statuses = {check["status"] for check in checks}
    if budget_exhausted or partial_delivery:
        status = "partial"
    elif "fail" in statuses:
        status = "fail"
    elif "warn" in statuses or "unknown" in statuses:
        status = "warn"
    else:
        status = "pass"
    return {
        **lexical,
        "status": status,
        "checks": checks,
        "gaps": gaps[:10],
        "semantic_verification": semantic,
        "budget_exhausted": bool(budget_exhausted),
        "partial_delivery": bool(partial_delivery),
        "verification_version": 2,
    }




def _build_evidence_quality(evidence: list[Evidence]) -> list[dict]:
    quality: list[dict] = []
    for item in evidence:
        try:
            score = float(item.score) if item.score is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        quality.append({"evidence_id": item.id, "score": score})
    return quality


def _build_claim_coverage(answer_text: str, evidence: list[Evidence]) -> list[dict]:
    """Map ``[n]`` citation markers in the answer to the evidence actually cited.

    Returns one entry per distinct cited index with status ``"citation_valid"`` and
    the matching evidence id. This is a lexical proxy for claim grounding (no
    LLM claim extraction) and feeds RAGAS' claim-aware context selection.
    """
    by_citation: dict[int, str] = {}
    for item in evidence:
        raw = (item.metadata or {}).get("citation_number")
        try:
            num = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            num = None
        if num is not None and num not in by_citation:
            by_citation[num] = item.id
    coverage: list[dict] = []
    for match in dict.fromkeys(_CITATION_RE.findall(answer_text or "")):
        num = int(match)
        evidence_id = by_citation.get(num)
        if evidence_id is None:
            continue
        coverage.append(
            {
                "claim_id": f"cite-{num}",
                "status": "citation_valid",
                "evidence_ids": [evidence_id],
            }
        )
    return coverage


def _build_citation_verification(answer_text: str, evidence: list[Evidence]) -> dict:
    """Lexical diagnostics; semantic support and sufficiency remain unevaluated."""
    coverage = _build_claim_coverage(answer_text, evidence)
    cited_ids = {item["evidence_ids"][0] for item in coverage}
    valid_numbers = {int(item["claim_id"].split("-", 1)[1]) for item in coverage}
    all_numbers = {int(item) for item in _CITATION_RE.findall(answer_text or "")}
    paragraphs = [text.strip() for text in re.split(r"\n\s*\n", answer_text or "") if text.strip()]
    return {
        "grounded": False if not evidence else None,
        "grounding_method": "citation_presence",
        "semantic_verification_status": "not_evaluated",
        "unsupported_claims": None,
        "weak_claims": None,
        "conflicts": None,
        "invalid_citations": sorted(all_numbers - valid_numbers),
        "uncited_paragraphs": [index for index, text in enumerate(paragraphs, 1) if not _CITATION_RE.search(text)],
        "citation_coverage": len(cited_ids) / len(evidence) if evidence else 0.0,
        "citation_coverage_basis": "distinct_cited_evidence/available_evidence",
    }


_GAP_MARKER_RE = re.compile(
    r"(?:未|没有)(?:找到|给出|出现|收录|提供|能确认|检索到|查到|明确)"
    r"|证据不足|资料(?:不足|缺失)|无法(?:确认|给出|确定|判断)"
    r"|缺少(?:相关)?(?:证据|数据|资料|数值)"
)


def answer_gap_lines(answer_text: str, limit: int = 5) -> list[str]:
    """Extract self-declared gap sentences so the next turn can resume them."""

    lines: list[str] = []
    for sentence in re.split(r"[。\n；;]", str(answer_text or "")):
        text = sentence.strip(" \t-#|")
        if len(text) < 6 or not _GAP_MARKER_RE.search(text):
            continue
        if text not in lines:
            lines.append(text[:240])
        if len(lines) >= limit:
            break
    return lines


def _agent_evidence_budget(attachment_refs: list[Any], question: str = "") -> int:
    """Return the per-run evidence budget for the assembled tool runtime.

    Long attachments and hardware-engineering questions (refdes, connector
    pins, table cells, thresholds) legitimately need more registered evidence
    than ordinary chat, so they use the larger budgets instead of stopping at
    30 chunks and losing the decisive row/cell.
    """

    base = int(getattr(settings, "AGENT_EVIDENCE_BUDGET", 30) or 30)
    if _hardware_profile(question)["is_hardware"]:
        base = max(base, _HARDWARE_EVIDENCE_BUDGET)
    if not attachment_refs:
        return base
    attachment_budget = int(
        getattr(settings, "AGENT_ATTACHMENT_EVIDENCE_BUDGET", 80) or 80
    )
    return max(base, attachment_budget)


def _build_messages(query: str, history: list[tuple[str, str]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for user_text, assistant_text in (history or [])[-6:]:
        if user_text:
            messages.append({"role": "user", "content": str(user_text)})
        if assistant_text:
            messages.append({"role": "assistant", "content": str(assistant_text)})
    messages.append({"role": "user", "content": query})
    return messages


def _scope_dependency_fields(scope: Any | None) -> dict[str, Any]:
    if scope is None:
        return {}
    return {
        "source_dependencies": {
            "version": 1,
            "kb_ids": sorted({int(source.kb_id) for source in scope.sources}),
            "inherited_turn_ids": [],
            "complete": True,
        }
    }


