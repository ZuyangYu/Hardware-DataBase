"""Per-request runtime shared by all agent tools.

A ``ToolRuntime`` is created for every ``MultiSourceAgentRunner.stream`` call
and closed over by the tool function factories, so request-scoped values
(kb_name / RequestContext / cancellation / event callback) never leak between
concurrent sessions. It also collects evidence rows, tool diagnostics and the
query ledger that feed ``get_last_retrieval_summary`` and the observability
footer after the stream finishes.
"""

from __future__ import annotations
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from src.agents.schemas import Evidence
from src.core.cancellation import QueryCancelled
from src.pipelines.document_rag.schemas import RequestContext
from src.services.knowledge_scope_models import KnowledgeScopeSnapshot

MAX_EVIDENCE_CONTENT_CHARS = 1200

TOOL_LABELS = {
    "document_search": "文档检索",
    "circuit_search": "电路检索",
    "spreadsheet_row_search": "表格行检索",
    "spreadsheet_cell_lookup": "单元格检索",
    "memory_search": "长期记忆检索",
    "spreadsheet_schema_lookup": "表格SQL结构",
    "spreadsheet_sql_query": "表格SQL查询",
    "list_kb_sources": "读取知识库目录",
    "attachment_list": "附件清单",
    "attachment_search": "附件检索",
    "attachment_read": "附件精读",
    "attachment_table_query": "附件表格查询",
    "attachment_circuit_search": "附件电路查询",
    "attachment_visual_analyze": "附件视觉分析",
    "request_attachment_release": "附件发布申请",
    "attachment_revision_history": "附件修订历史",
    "wiki_search": "Wiki 检索",
}

MAX_MEMORY_CONTEXT_CONTENT_CHARS = 2_000


def memory_context_item(row: dict[str, Any]) -> dict[str, Any]:
    """Return the privacy-safe fields shown for a retrieved memory.

    Long-term memory is intentionally kept separate from formal evidence. A
    response may still expose a compact, user-visible record of the memory
    context that was available to the agent, without leaking Store namespaces
    or the full Catalog record.
    """

    raw_content = row.get("content")
    if isinstance(raw_content, dict):
        title = str(
            row.get("title") or raw_content.get("title") or row.get("subject") or "未命名记忆"
        ).strip()
        content = str(raw_content.get("content") or raw_content.get("title") or "").strip()
        memory_type = str(
            row.get("type") or raw_content.get("memory_type") or row.get("kind") or ""
        ).strip()
    else:
        title = str(row.get("title") or row.get("subject") or "未命名记忆").strip()
        content = str(raw_content or "").strip()
        memory_type = str(row.get("type") or row.get("kind") or "").strip()

    try:
        source_count = max(0, int(row.get("source_count") or 0))
    except (TypeError, ValueError):
        source_count = 0

    return {
        "id": str(row.get("id") or row.get("memory_id") or ""),
        "scope": str(row.get("scope") or "").strip(),
        "status": str(row.get("status") or "candidate").strip(),
        "type": memory_type,
        "title": title[:200],
        "content": content[:MAX_MEMORY_CONTEXT_CONTENT_CHARS],
        "source_count": source_count,
        "has_provenance": bool(row.get("has_provenance")),
        "score": row.get("score"),
    }


def tool_label(tool_name: str) -> str:
    """Human label for a tool; external MCP tools get a server-qualified name."""

    name = str(tool_name or "")
    if name.startswith("mcp__") and "__" in name[5:]:
        slug, tool = name[5:].split("__", 1)
        if slug and tool:
            return f"外部插件 · {slug} · {tool}"
    if name.startswith("skill__") or name == "skill_task_status":
        return "沙箱 Skill"
    return TOOL_LABELS.get(name, name)


_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_NON_LATIN_RE = re.compile(r"[A-Za-z0-9_\s]+")


def _evidence_tokens(text: str) -> set[str]:
    """词法标记:拉丁词根 + CJK 二元组,用于问题-证据相关性估算。"""

    tokens: set[str] = set()
    for match in _LATIN_TOKEN_RE.findall(text or ""):
        tokens.add(match.casefold())
    cleaned = _NON_LATIN_RE.sub("", text or "")
    for index in range(len(cleaned) - 1):
        tokens.add(cleaned[index : index + 2])
    return tokens


def _evidence_relevance(question_tokens: set[str], item: Evidence) -> float:
    if not question_tokens:
        return 0.0
    content_tokens = _evidence_tokens(
        f"{item.source_name or ''} \n{item.content or ''}"
    )
    if not content_tokens:
        return 0.0
    return (
        sum(1 for token in question_tokens if token in content_tokens)
        / len(question_tokens)
    )


@dataclass
class ToolDiagnostics:
    tool_name: str
    hit_count: int = 0
    status: str = "ok"
    error: str = ""
    latency_ms: int = 0
    filters: dict[str, Any] = field(default_factory=dict)


class AdmittedEvidence(list):
    """Admitted evidence rows plus the reason an empty result is empty.

    ``timed_tool_call`` returns a plain list-like value so existing callers can
    keep unpacking ``(items, adds_nothing)``.  Carrying the budget flag on the
    result itself (instead of on the shared runtime) keeps concurrent parallel
    tool calls from attributing another call's budget rejection to themselves.
    """

    budget_blocked: bool = False


@dataclass
class ToolRuntime:
    kb_name: str
    ctx: RequestContext | None
    # Server-normalized, owner-bound document context.  It is intentionally
    # opaque to ordinary retrieval tools; only the explicitly gated document
    # tool factory consumes it.
    document_context: Any | None = None
    knowledge_scope: KnowledgeScopeSnapshot | None = None
    chat_session_id: str = ""
    top_k: int = 6
    query_mode: str = "deep"
    question: str = ""
    evidence_budget: int = 30
    should_cancel: Callable[[], bool] | None = None
    on_event: Callable[[dict], None] | None = None

    # Server-resolved chat attachment scope (design §11.1).  The refs are
    # frozen after ACL verification in the turn path; tools derive every
    # allowed asset id from this list and can never widen it.
    attachment_refs: list[Any] = field(default_factory=list)
    source_scope: str = "auto"
    # Numeric identities are copied from the authenticated request into the
    # runtime so attachment tools can re-check ownership on every invocation.
    # Missing identities intentionally fail closed in the tool layer.
    attachment_user_id: int | None = None
    attachment_session_id: int | None = None
    attachment_service: Any | None = None
    # Explicit write gate for legacy/document-flow tool assembly.  The
    # default keeps direct tool tests and older callers backwards compatible;
    # the runner binds it to the authenticated turn's document-write policy.
    attachment_edit_enabled: bool = True
    attachment_retrieval: Any | None = None
    attachment_visual_analyzer: Any | None = None

    evidence: list[Evidence] = field(default_factory=list)
    evidence_index: dict[str, int] = field(default_factory=dict)
    memory_context: list[dict[str, Any]] = field(default_factory=list)
    memory_context_index: set[str] = field(default_factory=set)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    # External MCP plugins keep their own diagnostics and per-turn call budget:
    # external calls must not change retrieval rounds, the evidence set or the
    # grounding footer.
    mcp_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    mcp_call_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # P0-007: per-call epochs make late evidence registration impossible once
    # a call (or the whole cancellation path) has ended; checks and appends
    # share ``_lock`` so there is no check-then-register window.
    _call_epoch: int = 0
    _active_call_epochs: set[int] = field(default_factory=set)

    def begin_tool_call(self) -> int:
        with self._lock:
            self._call_epoch += 1
            self._active_call_epochs.add(self._call_epoch)
            return self._call_epoch

    def end_tool_call(self, epoch: int) -> None:
        with self._lock:
            self._active_call_epochs.discard(int(epoch))

    def mark_ended(self) -> None:
        """Revoke every outstanding call epoch (cancellation/run teardown)."""
        with self._lock:
            self._active_call_epochs.clear()

    def check_cancel(self) -> None:
        if self.should_cancel is not None and self.should_cancel():
            self.mark_ended()
            raise QueryCancelled("用户取消查询")

    def emit(self, evt_type: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event({"type": evt_type, "payload": payload})
            except Exception:
                pass

    def log_query(self, tool_name: str, query: str) -> None:
        q = str(query or "").strip()
        if not q:
            return
        with self._lock:
            if not any(
                item["tool_name"] == tool_name and item["query"] == q
                for item in self.queries
            ):
                self.queries.append({"tool_name": tool_name, "query": q})

    def has_query(self, tool_name: str, query: str) -> bool:
        """Whether this exact (tool, query) pair was already executed this run."""
        q = str(query or "").strip()
        if not q:
            return False
        with self._lock:
            return any(
                item["tool_name"] == tool_name and item["query"] == q
                for item in self.queries
            )

    def add_evidence(self, items: list[Evidence], *, epoch: int | None = None) -> list[int | None]:
        """Register evidence rows and return their 1-based citation numbers.

        ``None`` marks items rejected by the evidence budget: they were never
        registered, so callers must not render or cite them. 超出预算时按
        与问题的词法相关性准入;已编号的证据永不淘汰,保证历史消息里
        的 [n] 引用不悬空。

        When ``epoch`` is given (the tool-call epoch from ``begin_tool_call``),
        registration is refused for calls that already ended or were revoked by
        cancellation, so a late result cannot mutate the evidence set.
        """
        numbers: list[int | None] = []
        with self._lock:
            if epoch is not None and int(epoch) not in self._active_call_epochs:
                self._record_late_evidence_rejected()
                return [None for _ in items]
            fresh: list[int] = []
            for position, item in enumerate(items):
                if item.id and item.id in self.evidence_index:
                    numbers.append(self.evidence_index[item.id])
                    continue
                fresh.append(position)
                numbers.append(None)
            room = (
                max(0, self.evidence_budget - len(self.evidence))
                if self.evidence_budget > 0
                else len(fresh)
            )
            if self.knowledge_scope is not None:
                fresh.sort(key=lambda position: int(items[position].metadata.get("fusion_rank") or position + 1))
                required = {source.kb_id for source in self.knowledge_scope.sources} if self.source_scope != "attachment_only" else set()
                covered = {item.metadata.get("kb_id") for item in self.evidence}
                kept = set()
                for position in fresh:
                    if len(kept) >= room:
                        break
                    kb_id = items[position].metadata.get("kb_id")
                    missing_after = required - covered - {kb_id}
                    if room - len(kept) - 1 < len(missing_after):
                        continue
                    kept.add(position)
                    covered.add(kb_id)
            elif len(fresh) > room:
                question_tokens = _evidence_tokens(self.question)
                # 检索器相似度优先(RAGFlow score 为真实语义分), 词法分仅作次键;
                # 纯词法排序会错杀"问题文本中不出现的要素"(如"谁编制"问题里的人名)。
                fresh.sort(
                    key=lambda position: (
                        items[position].score or 0.0,
                        _evidence_relevance(question_tokens, items[position]),
                    ),
                    reverse=True,
                )
                kept = set(fresh[:room])
            else:
                kept = set(fresh)
            for position in fresh:
                if position not in kept:
                    continue
                item = items[position]
                key = item.id or f"{item.source_name}:{len(self.evidence)}"
                self.evidence.append(item)
                self.evidence_index[key] = len(self.evidence)
                numbers[position] = len(self.evidence)
        return numbers

    @staticmethod
    def _record_late_evidence_rejected() -> None:
        try:
            from src.observability.metrics import counter

            counter(
                "hdb.retrieval.late_evidence_rejected",
                description="Evidence registration refused after its tool call ended",
            )
        except Exception:
            pass

    def finalize_evidence(self) -> None:
        """按问题相关性重排证据堆;citation_number 在 metadata 里,编号不变。"""

        with self._lock:
            if self.knowledge_scope is not None:
                return
            if len(self.evidence) < 2:
                return
            question_tokens = _evidence_tokens(self.question)
            self.evidence.sort(
                key=lambda item: _evidence_relevance(question_tokens, item),
                reverse=True,
            )

    def add_memory_context(self, items: list[dict[str, Any]]) -> None:
        """Keep unique long-term memory rows for the final UI summary."""

        with self._lock:
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or "").strip()
                if not key:
                    key = "|".join(
                        str(item.get(field) or "")
                        for field in ("scope", "title", "content")
                    )
                if key in self.memory_context_index:
                    continue
                self.memory_context_index.add(key)
                self.memory_context.append(dict(item))

    def reserve_mcp_call(self, limit: int) -> bool:
        """Atomically consume one external-call slot for this turn."""

        with self._lock:
            if limit > 0 and self.mcp_call_count >= limit:
                return False
            self.mcp_call_count += 1
            return True

    def record_mcp_diagnostic(self, diagnostic: dict[str, Any]) -> None:
        with self._lock:
            self.mcp_diagnostics.append(dict(diagnostic))

    def record_diagnostic(self, diag: ToolDiagnostics) -> None:
        with self._lock:
            self.diagnostics.append(
                {
                    "tool_name": diag.tool_name,
                    "hit_count": diag.hit_count,
                    "status": diag.status,
                    "error": diag.error,
                    "latency_ms": diag.latency_ms,
                    "filters": diag.filters,
                }
            )


def timed_tool_call(
    rt: ToolRuntime,
    tool_name: str,
    query: str,
    filters: dict[str, Any] | None,
    fn: Callable[[], list[Evidence]],
) -> tuple[AdmittedEvidence, bool]:
    """Shared wrapper: cancel-check, timing, diagnostics and event emission.

    Returns ``(items, adds_nothing)`` — ``adds_nothing`` marks a call that is
    an exact (tool, query) repeat AND returned only evidence ids that were
    already registered before this call, i.e. the rendered output would be a
    byte-for-byte repeat of something already in the message history. The
    snapshot is taken before ``add_evidence`` registers this call's items, so
    genuinely new evidence always falls through to a full render.

    ``items`` is an ``AdmittedEvidence`` list whose ``budget_blocked`` flag is
    set when the source returned rows but the run's evidence budget rejected
    all of them.
    """
    rt.check_cancel()
    was_repeat = rt.has_query(tool_name, query)
    with rt._lock:
        pre_registered = set(rt.evidence_index)
    rt.emit("tool_started", {"tool_name": tool_name, "query": query})
    started = time.monotonic()
    call_epoch = rt.begin_tool_call()
    try:
        from src.core.concurrency import governor_acquire

        with governor_acquire("tool", interactive=True):
            items = fn()
        # Late results from a cancelled run must never be registered as
        # evidence (P0-007); re-check cancellation after the call returns.
        rt.check_cancel()
    except QueryCancelled:
        raise
    except Exception as exc:
        from src.core.error_friendly import friendly_error_message

        try:
            from src.observability.metrics import record_agent_tool_call

            record_agent_tool_call(tool=tool_name, status="failed")
        except Exception:
            pass
        rt.record_diagnostic(
            ToolDiagnostics(
                tool_name=tool_name,
                status="failed",
                error=str(exc)[:300],
                latency_ms=int((time.monotonic() - started) * 1000),
                filters=dict(filters or {}),
            )
        )
        rt.emit(
            "tool_result",
            {
                "tool_name": tool_name,
                "hit_count": 0,
                "status": "failed",
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
        rt.emit(
            "degraded",
            {
                "stage": f"tool_{tool_name}",
                "reason": friendly_error_message(exc),
            },
        )
        rt.end_tool_call(call_epoch)
        return AdmittedEvidence(), False
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        from src.observability.metrics import record_agent_tool_call

        record_agent_tool_call(tool=tool_name, status="ok")
    except Exception:
        pass
    rt.log_query(tool_name, query)
    numbers = rt.add_evidence(items, epoch=call_epoch)
    rt.end_tool_call(call_epoch)
    admitted_items: AdmittedEvidence = AdmittedEvidence()
    for item, number in zip(items, numbers):
        if number is None:
            continue
        item.metadata = {**item.metadata, "citation_number": number}
        admitted_items.append(item)
    # Distinguish "the source has nothing" from "the run already holds its
    # full evidence budget": only the latter is an execution limit, and the
    # model must be told so it can answer with the evidence it already has.
    admitted_items.budget_blocked = bool(items) and not admitted_items
    rt.record_diagnostic(
        ToolDiagnostics(
            tool_name=tool_name,
            hit_count=len(admitted_items),
            latency_ms=latency_ms,
            filters=dict(filters or {}),
        )
    )
    rt.emit(
        "tool_result",
        {
            "tool_name": tool_name,
            "hit_count": len(admitted_items),
            "status": "done",
            "latency_ms": latency_ms,
        },
    )
    adds_nothing = bool(admitted_items) and all(
        item.id and item.id in pre_registered for item in admitted_items
    )
    return admitted_items, was_repeat and adds_nothing


_LEADING_CITATION_RE = re.compile(r"(?m)^(\[\d+\])")


def format_evidence_for_llm(items: list[Evidence]) -> str:
    """Render evidence as explicitly delimited blocks for the model.

    Each block is wrapped in ``<evidence id=... source=...>`` tags so chunk
    text can't be mistaken for scaffolding, and content-internal line-leading
    ``[n] 来源`` patterns are neutralised with backticks so forged citation
    numbers can't be confused with our own ``[n]`` numbering.
    """
    if not items:
        return "（未找到相关内容）"
    blocks = []
    for item in items:
        number = int(item.metadata.get("citation_number") or 0)
        content = str(item.content or "")[:MAX_EVIDENCE_CONTENT_CHARS]
        content = _LEADING_CITATION_RE.sub(r"`\1`", content)
        locator = item.locator or {}
        locator_text = ", ".join(f"{k}={v}" for k, v in locator.items() if v not in (None, ""))
        from src.services.evidence_fusion import source_label

        label = source_label(item.source_name, item.metadata)
        header = f"[{number}] 来源：{label} | 类型：{item.content_kind}"
        if locator_text:
            header += f" | 位置：{locator_text}"
        blocks.append(
            f'<evidence id="{number}" source="{item.source_name}">\n'
            f"{header}\n"
            f"{content}\n"
            f"</evidence>"
        )
    return "\n\n".join(blocks)


def evidence_budget_message(rt: ToolRuntime) -> str:
    """Explain a tool call that was dropped because the evidence budget is full."""

    budget = int(getattr(rt, "evidence_budget", 0) or 0)
    held = len(getattr(rt, "evidence", []) or [])
    return (
        f"本轮可登记的证据已达上限（{budget} 条，当前 {held} 条），"
        "本次调用返回的新内容因超出预算未纳入证据集。"
        "这是单轮执行预算限制，不代表知识库或附件中没有相关内容。"
        "请勿继续调用检索/读取类工具翻找；请基于已获得的证据直接作答，"
        "并在回答中说明当前结论的证据范围与仍缺失的信息。"
    )


def empty_result_message(
    rt: ToolRuntime, default: str, *, budget_blocked: bool = False
) -> str:
    """Return the accurate reason for an empty tool result."""

    if budget_blocked:
        return evidence_budget_message(rt)
    return default


def format_tool_result(rt: ToolRuntime, adds_nothing: bool, items: list[Evidence]) -> str:
    """Render a tool result for the model, collapsing no-op repeat calls.

    A repeated (tool, query) that returned only already-registered evidence
    adds no information — the full evidence blocks are already in the message
    history — so a short hint is returned instead of re-sending every block.
    This keeps per-round context growth bounded when the model re-issues
    identical retrieval calls. Any new evidence id falls through to the full
    render so fresh content always reaches the model.

    An empty render after a call that did fetch items means the evidence
    budget rejected all of them; that execution limit is reported explicitly
    instead of the generic "no content" text, which the model would otherwise
    misread as a retrieval failure and retry.
    """
    if not items:
        return empty_result_message(
            rt,
            "（未找到相关内容）",
            budget_blocked=bool(getattr(items, "budget_blocked", False)),
        )
    if adds_nothing:
        with rt._lock:
            numbers = sorted({int(rt.evidence_index[item.id]) for item in items})
        cited = " ".join(f"[{number}]" for number in numbers)
        # 事实陈述，不带指令：是否继续引用 [n]、换关键词还是再次检索，
        # 由模型根据当前对话自行判断（策略在 SYSTEM_PROMPT，不在这里）。
        return (
            f"本次调用返回的证据与历史消息中已注册的 {cited} 完全相同，无新增内容。"
        )
    return format_evidence_for_llm(items)
