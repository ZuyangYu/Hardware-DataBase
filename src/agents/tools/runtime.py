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
from typing import Any, Callable, Iterable

from src.agents.schemas import Evidence
from src.contracts.agent_events import build_agent_event
from src.core.cancellation import QueryCancelled
from src.pipelines.document_rag.schemas import RequestContext
from src.services.evidence_sources import (
    KNOWLEDGE_SOURCE_TYPES,
    source_type_for_metadata,
    with_source_metadata,
)
from src.services.knowledge_scope_models import KnowledgeScopeSnapshot

MAX_EVIDENCE_CONTENT_CHARS = 1200
# Comparison rows intentionally carry both sides plus a short inline diff.
# Truncating them to the generic single-chunk cap would silently drop the
# right side and the diff before the model ever sees them.
MAX_COMPARISON_CONTENT_CHARS = 4800

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

    ``failure`` carries a tool/backend failure (timeout, unavailable service,
    permission error).  It must reach the model as a distinct outcome: a
    failed retrieval is **not** evidence that the knowledge base has no such
    material, and the model must not answer with a confident negative.
    """

    budget_blocked: bool = False
    # Multi-KB retrieval attaches source-level execution diagnostics and the
    # fusion audit to the list returned by a single tool call.  Keeping these
    # attributes on the list preserves the legacy ``(items, adds_nothing)``
    # contract while preventing concurrent calls from mixing diagnostics.
    source_statuses: list[dict[str, Any]] = []
    fusion_summary: dict[str, Any] = {}
    # A tool/backend failure is a fact about this execution, not a zero-hit
    # fact about the knowledge base.  Keep both fields on every result list.
    failure: str = ""
    tool_name: str = ""
    # no_match / budget_blocked / out_of_scope / tool_error
    empty_reason: str = ""
    # Search hits that were deliberately kept out of the citation ledger until
    # the model asks for them through evidence_read.
    candidate_rows: list[dict[str, Any]] = []
    candidate_count: int = 0


class CitationAdmission(list):
    """Citation numbers and the registry observed in the admission lock."""

    def __init__(self, numbers=(), *, previously_registered=()):
        super().__init__(numbers)
        self.previously_registered = set(previously_registered)


@dataclass
class AdmissionResult:
    evidence: list[Evidence] = field(default_factory=list)
    numbers: list[int | None] = field(default_factory=list)
    target_coverage: dict[str, Any] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    source_statuses: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolRuntime:
    kb_name: str
    ctx: RequestContext | None
    document_store: Any | None = None
    knowledge_scope: KnowledgeScopeSnapshot | None = None
    # Server-resolved explicit file/sheet/pin targets for this turn. Tools
    # share this object; model-supplied IDs never replace it.
    turn_retrieval_targets: Any | None = None
    turn_work_items: tuple[Any, ...] = ()
    turn_evidence_plan: Any | None = None
    field_evidence_plan: Any | None = None
    # Multi-KB retrieval is injected by the authenticated execution boundary.
    # Keeping both objects request-scoped prevents a tool from widening a
    # persisted scope or creating an independent budget for each source.
    multi_kb_retrieval: Any | None = None
    retrieval_budget: Any | None = None
    # Source-aware formal-evidence accounting.  Tools may read the snapshot for
    # diagnostics but must never keep an independent copy of the budget.
    evidence_budget_coordinator: Any | None = None
    evidence_policy: Any | None = None
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
    invocations: list[dict[str, Any]] = field(default_factory=list)
    candidate_pool: dict[str, Evidence] = field(default_factory=dict)
    _candidate_pool_order: list[str] = field(default_factory=list)
    # Two-stage retrieval: search hits stay as non-citable candidates; only
    # evidence_read promotes selected rows into the citation ledger.
    candidate_mode: bool = False
    candidate_store_limit: int = 60
    candidate_evidence: dict[str, Evidence] = field(default_factory=dict)
    candidate_order: list[str] = field(default_factory=list)
    _candidate_seq: int = 0
    # A productive loop may run longer; repeated/no-new-result loops stop early
    # instead of burning the full recursion budget.
    no_progress_limit: int = 4
    no_progress_streak: int = 0
    progress_events: int = 0
    loop_stop_reason: str = ""
    seen_sources: set[str] = field(default_factory=set)
    session_recall_items: list[Evidence] = field(default_factory=list)
    session_recall_gaps: list[str] = field(default_factory=list)
    prior_gaps: list[str] = field(default_factory=list)
    last_rejection_reasons: dict[str, int] = field(default_factory=dict)
    _invocation_seq: int = 0
    multi_kb_source_outcomes: list[dict[str, Any]] = field(default_factory=list)
    multi_kb_fusions: list[dict[str, Any]] = field(default_factory=list)
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
            self.candidate_pool.clear()
            self._candidate_pool_order.clear()
            self.candidate_evidence.clear()
            self.candidate_order.clear()

    def check_cancel(self) -> None:
        if self.should_cancel is not None and self.should_cancel():
            self.mark_ended()
            raise QueryCancelled("用户取消查询")

    def note_progress(self) -> None:
        """A tool call added new evidence, candidates or a new source."""

        with self._lock:
            self.no_progress_streak = 0
            self.progress_events += 1

    def note_no_progress(self) -> None:
        """Stop a loop that keeps returning the same material."""

        with self._lock:
            if self.loop_stop_reason:
                return
            self.no_progress_streak += 1
            if self.no_progress_streak >= max(1, int(self.no_progress_limit)):
                self.loop_stop_reason = "no_progress"

    def should_stop_for_no_progress(self) -> bool:
        with self._lock:
            return bool(self.loop_stop_reason)

    def emit(self, evt_type: str, payload: dict[str, Any]) -> None:
        event = build_agent_event(evt_type, **payload)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass

    def tool_started(
        self,
        *,
        call_id: str,
        tool_name: str,
        tool_kind: str = "retrieval",
        query: str = "",
        server: str = "",
        tool: str = "",
        transport: str = "",
        skill_ref: str = "",
        task_id: str = "",
        automatic: bool = False,
    ) -> None:
        self.emit(
            "tool_started",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "tool_kind": tool_kind,
                "query": query,
                "server": server,
                "tool": tool,
                "transport": transport,
                "skill_ref": skill_ref,
                "task_id": task_id,
                "automatic": automatic,
            },
        )

    def tool_result(
        self,
        *,
        call_id: str,
        tool_name: str,
        status: str,
        tool_kind: str = "retrieval",
        hit_count: int = 0,
        evidence_count: int = 0,
        candidate_count: int = 0,
        sources: list[str] | None = None,
        citation_numbers: list[int] | None = None,
        empty_reason: str = "",
        latency_ms: int | None = None,
        result_bytes: int = 0,
        truncated: bool = False,
        error: str = "",
        server: str = "",
        tool: str = "",
        transport: str = "",
        skill_ref: str = "",
        task_id: str = "",
        automatic: bool = False,
    ) -> None:
        self.emit(
            "tool_result",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "tool_kind": tool_kind,
                "status": status,
                "hit_count": hit_count,
                "evidence_count": evidence_count,
                "candidate_count": candidate_count,
                "sources": list(sources or []),
                "citation_numbers": list(citation_numbers or []),
                "empty_reason": empty_reason,
                "latency_ms": latency_ms,
                "result_bytes": result_bytes,
                "truncated": truncated,
                "error": error,
                "server": server,
                "tool": tool,
                "transport": transport,
                "skill_ref": skill_ref,
                "task_id": task_id,
                "automatic": automatic,
            },
        )

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

    def _remember_candidate_locked(self, item: Evidence) -> None:
        key = str(item.id or f"{item.source_name}:{len(self.candidate_pool)}")
        if key not in self.candidate_pool:
            self._candidate_pool_order.append(key)
        self.candidate_pool[key] = item
        import src.settings as settings

        limit = max(1, int(getattr(settings, "MULTI_KB_MAX_SOURCES", 4))
                    * int(getattr(settings, "MULTI_KB_PER_SOURCE_CANDIDATES",
                                getattr(settings, "MULTI_KB_PER_SOURCE_TOP_K", 8))))
        while len(self.candidate_pool) > limit and self._candidate_pool_order:
            oldest = self._candidate_pool_order.pop(0)
            self.candidate_pool.pop(oldest, None)
        byte_budget = 2 * int(getattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES",
                                       getattr(settings, "MULTI_KB_CONTEXT_MAX_TOKENS", 8000)))
        while self._candidate_pool_order:
            total = sum(_evidence_context_cost(item) for item in self.candidate_pool.values())
            if total <= byte_budget:
                break
            oldest = self._candidate_pool_order.pop(0)
            self.candidate_pool.pop(oldest, None)

    def promote_pooled_candidates(self) -> list[Evidence]:
        """Admit previously bounded candidates once room becomes available."""
        plan = getattr(self, "turn_evidence_plan", None)
        if plan is None or not self.candidate_pool:
            return []
        import src.settings as settings

        promoted: list[Evidence] = []
        with self._lock:
            room = max(0, int(self.evidence_budget) - len(self.evidence)) if self.evidence_budget > 0 else 10**9
            coordinator = getattr(self, "evidence_budget_coordinator", None)
            source_aware = bool(coordinator is not None and getattr(coordinator, "enabled", False))
            if not source_aware:
                room = min(room, max(0, int(getattr(settings, "MULTI_KB_EVIDENCE_MAX_ITEMS", 24)) - len(self.evidence)))
            byte_limit = int(getattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES",
                                     getattr(settings, "MULTI_KB_CONTEXT_MAX_TOKENS", 8000)))
            remaining_bytes = max(0, byte_limit - sum(_evidence_context_cost(item) for item in self.evidence))
            if source_aware:
                remaining_bytes = 10**9
            ordered = sorted(
                self.candidate_pool.values(),
                key=lambda item: (
                    0 if plan.evaluate(item) else 1,
                    -float(getattr(item, "score", 0.0) or 0.0),
                    str(item.id or ""),
                ),
            )
            for item in ordered:
                if len(promoted) >= room:
                    break
                item.metadata = with_source_metadata(
                    item.metadata, content=item.content, locator=item.locator,
                )
                matched = plan.evaluate(item)
                if not matched or not item.id or item.id in self.evidence_index:
                    continue
                cost = _evidence_context_cost(item)
                if cost > remaining_bytes:
                    continue
                if coordinator is not None:
                    decision = coordinator.try_admit(
                        source_type_for_metadata(item.metadata), cost, stage="candidate_promotion",
                    )
                    if not decision.admitted:
                        self.last_rejection_reasons[decision.reason] = (
                            self.last_rejection_reasons.get(decision.reason, 0) + 1
                        )
                        plan.mark_blocked(
                            decision.reason, source_type=decision.source_type,
                        )
                        continue
                item.metadata = {**item.metadata, "citation_number": len(self.evidence) + 1}
                self.evidence.append(item)
                self.evidence_index[item.id] = len(self.evidence)
                if coordinator is not None:
                    coordinator.note_admitted(
                        source_type_for_metadata(item.metadata), cost,
                    )
                plan.mark_admitted(matched, item.id)
                self._apply_field_verification(item)
                promoted.append(item)
                remaining_bytes -= cost
                self.candidate_pool.pop(item.id, None)
                if item.id in self._candidate_pool_order:
                    self._candidate_pool_order.remove(item.id)
        return promoted

    def _apply_field_verification(self, item: Evidence) -> None:
        plan = getattr(self, "field_evidence_plan", None)
        if plan is not None:
            try:
                plan.apply(plan.evaluate(item))
            except Exception:
                pass

    def add_evidence(
        self,
        items: list[Evidence],
        *,
        epoch: int | None = None,
    ) -> CitationAdmission:
        """Register evidence rows and return their 1-based citation numbers.

        ``None`` marks items rejected by the evidence budget: they were never
        registered, so callers must not render or cite them. 超出预算时按
        与问题的词法相关性准入;已编号的证据永不淘汰,保证历史消息里
        的 [n] 引用不悬空。

        When ``epoch`` is given (the tool-call epoch from ``begin_tool_call``),
        registration is refused for calls that already ended or were revoked by
        cancellation, so a late result cannot mutate the evidence set.
        """
        with self._lock:
            numbers = CitationAdmission(previously_registered=self.evidence_index)
            if epoch is not None and int(epoch) not in self._active_call_epochs:
                self._record_late_evidence_rejected()
                return CitationAdmission([None for _ in items], previously_registered=self.evidence_index)
            fresh: list[int] = []
            for position, item in enumerate(items):
                item.metadata = with_source_metadata(
                    item.metadata, content=item.content, locator=item.locator,
                )
                if item.id and item.id in self.evidence_index:
                    numbers.append(self.evidence_index[item.id])
                    plan = getattr(self, "turn_evidence_plan", None)
                    if plan is not None:
                        plan.mark_admitted(plan.evaluate(item), item.id)
                    self._apply_field_verification(item)
                    continue
                fresh.append(position)
                numbers.append(None)
            self.last_rejection_reasons = {
                "call_quota": 0, "item_limit": 0, "byte_limit": 0, "token_limit": 0,
                "source_budget_exhausted": 0, "global_budget_exhausted": 0,
            }
            budget_room = (
                max(0, self.evidence_budget - len(self.evidence))
                if self.evidence_budget > 0
                else len(fresh)
            )
            room = budget_room
            if self.knowledge_scope is not None:
                import src.settings as settings

                coordinator = getattr(self, "evidence_budget_coordinator", None)
                source_aware = bool(coordinator is not None and getattr(coordinator, "enabled", False))
                if not source_aware:
                    item_room = max(0, int(settings.MULTI_KB_EVIDENCE_MAX_ITEMS) - len(self.evidence))
                    room = min(room, item_room)

                remaining_tokens = max(0, int(settings.MULTI_KB_CONTEXT_MAX_TOKENS)) - sum(
                    _evidence_context_cost(item) for item in self.evidence
                )
                remaining_bytes = max(0, int(getattr(
                    settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES", settings.MULTI_KB_CONTEXT_MAX_TOKENS,
                ))) - sum(_evidence_context_cost(item) for item in self.evidence)
                if source_aware:
                    # The coordinator owns the global cap and source reserves;
                    # the legacy global byte/token counters must not double-block
                    # a source that still has its own promised capacity.
                    remaining_tokens = 10**9
                    remaining_bytes = 10**9
                plan = getattr(self, "turn_evidence_plan", None)

                def source_budget_blocks(position: int, cost: int) -> bool:
                    if coordinator is None:
                        return False
                    item = items[position]
                    source_type = source_type_for_metadata(item.metadata)
                    decision = coordinator.try_admit(source_type, cost)
                    if decision.admitted:
                        return False
                    reason = decision.reason or "source_budget_exhausted"
                    self.last_rejection_reasons[reason] = (
                        self.last_rejection_reasons.get(reason, 0) + 1
                    )
                    if plan is not None:
                        plan.mark_blocked(reason, source_type=source_type)
                    self._remember_candidate_locked(item)
                    try:
                        from src.observability.metrics import counter

                        counter(
                            "hdb.evidence.budget_blocked",
                            attributes={"source_type": source_type, "reason": reason},
                            description="Formal evidence rejected by a source-aware budget",
                        )
                    except Exception:
                        pass
                    return True

                def admission_priority(position: int) -> tuple:
                    item = items[position]
                    fusion_rank = int(item.metadata.get("fusion_rank") or position + 1)
                    if plan is None:
                        return (1, fusion_rank)
                    matched = plan.evaluate(item)
                    pending = any(plan.states[requirement_id].state == "pending" for requirement_id in matched)
                    return (0 if pending else 1, fusion_rank)

                fresh.sort(key=admission_priority)
                required = {source.kb_id for source in self.knowledge_scope.sources} if self.source_scope != "attachment_only" else set()
                if plan is not None and plan.targeted_kb_ids:
                    required = set(plan.targeted_kb_ids)
                covered = {item.metadata.get("kb_id") for item in self.evidence}
                kept = set()
                if plan is not None:
                    # Target/query obligations get first claim on the turn
                    # budget. Background chunks wait until every obligation is
                    # admitted, so a fast generic batch cannot crowd them out.
                    outstanding = {
                        requirement_id for requirement_id, state in plan.states.items()
                        if state.state in {"pending", "candidate"}
                    }
                    covered_by_fresh = set()
                    for position in fresh:
                        covered_by_fresh.update(plan.evaluate(items[position]))
                    reserve_ids = outstanding - covered_by_fresh
                    reserve_cost = 0
                    if reserve_ids:
                        cheapest = min(_evidence_context_cost(items[position]) for position in fresh) if fresh else 1200
                        reserve_cost = len(reserve_ids) * cheapest
                    per_requirement: dict[str, int] = {}
                    for position in fresh:
                        matched = plan.evaluate(items[position])
                        if len(kept) >= room:
                            self.last_rejection_reasons["call_quota" if room == budget_room else "item_limit"] += 1
                            if matched:
                                self._remember_candidate_locked(items[position])
                            continue
                        if not matched:
                            continue
                        usable = [requirement_id for requirement_id in matched
                                  if per_requirement.get(requirement_id, 0) < 2]
                        if not usable:
                            continue
                        cost = _evidence_context_cost(items[position])
                        if source_budget_blocks(position, cost):
                            continue
                        if cost > remaining_tokens:
                            self.last_rejection_reasons["token_limit"] += 1
                            self._remember_candidate_locked(items[position])
                            continue
                        if cost + reserve_cost > remaining_bytes:
                            self.last_rejection_reasons["byte_limit"] += 1
                            self._remember_candidate_locked(items[position])
                            continue
                        kept.add(position)
                        if coordinator is not None:
                            coordinator.note_admitted(
                                source_type_for_metadata(items[position].metadata), cost,
                            )
                        for requirement_id in usable:
                            per_requirement[requirement_id] = per_requirement.get(requirement_id, 0) + 1
                        remaining_tokens -= cost
                        remaining_bytes -= cost
                else:
                    batch_has_kb = any(
                        source_type_for_metadata(items[position].metadata) in KNOWLEDGE_SOURCE_TYPES
                        for position in fresh
                    )
                    for position in fresh:
                        if len(kept) >= room:
                            self.last_rejection_reasons["call_quota" if room == budget_room else "item_limit"] += 1
                            break
                        kb_id = items[position].metadata.get("kb_id")
                        missing_after = (
                            required - covered - {kb_id} if batch_has_kb else set()
                        )
                        if room - len(kept) - 1 < len(missing_after):
                            self.last_rejection_reasons["item_limit"] += 1
                            continue
                        cost = _evidence_context_cost(items[position])
                        if source_budget_blocks(position, cost):
                            continue
                        if cost > remaining_tokens:
                            self.last_rejection_reasons["token_limit"] += 1
                            continue
                        if cost > remaining_bytes:
                            self.last_rejection_reasons["byte_limit"] += 1
                            continue
                        kept.add(position)
                        if coordinator is not None:
                            coordinator.note_admitted(
                                source_type_for_metadata(items[position].metadata), cost,
                            )
                        covered.add(kb_id)
                        remaining_tokens -= cost
                        remaining_bytes -= cost
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
                self.last_rejection_reasons[
                    "call_quota" if room == budget_room else "item_limit"
                ] += max(0, len(fresh) - len(kept))
            else:
                kept = set(fresh)
            for position in fresh:
                if position not in kept:
                    plan = getattr(self, "turn_evidence_plan", None)
                    if plan is not None:
                        matched = plan.evaluate(items[position])
                        if matched:
                            self._remember_candidate_locked(items[position])
                        plan.mark_candidate(matched, items[position].id or "")
                    continue
                item = items[position]
                key = item.id or f"{item.source_name}:{len(self.evidence)}"
                self.evidence.append(item)
                self.evidence_index[key] = len(self.evidence)
                numbers[position] = len(self.evidence)
                plan = getattr(self, "turn_evidence_plan", None)
                if plan is not None:
                    plan.mark_admitted(plan.evaluate(item), key)
                self._apply_field_verification(item)
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

    def admit_candidate_batch(self, batch, *, epoch: int | None = None,
                              source_statuses: list[dict[str, Any]] | None = None) -> AdmissionResult:
        """Admit a ranked candidate batch through the one runtime lock."""
        items = list(batch)
        numbers = self.add_evidence(items, epoch=epoch)
        admitted = []
        for item, number in zip(items, numbers):
            if number is None:
                continue
            item.metadata = {**item.metadata, "citation_number": number}
            admitted.append(item)
        plan = getattr(self, "turn_evidence_plan", None)
        return AdmissionResult(
            evidence=admitted, numbers=list(numbers),
            target_coverage=plan.coverage_snapshot() if plan is not None else {},
            rejected={"budget_discarded": sum(number is None for number in numbers),
                      **dict(getattr(self, "last_rejection_reasons", {}) or {})},
            source_statuses=list(source_statuses or getattr(batch, "source_statuses", []) or []),
        )

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

    def _candidate_key(self, item: Evidence) -> str:
        value = str(getattr(item, "id", "") or "").strip()
        if value:
            return value
        self._candidate_seq += 1
        source = str(getattr(item, "source_name", "") or "candidate")
        return f"candidate:{self._candidate_seq}:{source}"

    def stash_candidates(self, items: list[Evidence]) -> list[dict[str, Any]]:
        """Keep search hits as non-citable candidates and return a bounded index."""

        rows: list[dict[str, Any]] = []
        with self._lock:
            for index, item in enumerate(items):
                key = self._candidate_key(item)
                if key not in self.candidate_evidence:
                    self.candidate_evidence[key] = item
                    self.candidate_order.append(key)
                locator = item.locator or {}
                locator_text = ", ".join(
                    f"{name}={value}"
                    for name, value in locator.items()
                    if value not in (None, "", [], {})
                )
                rows.append(
                    {
                        "id": key,
                        "source": str(item.source_name or ""),
                        "locator": locator_text[:160],
                        "score": float(item.score or 0.0),
                        "snippet": " ".join(str(item.content or "").split())[:180],
                    }
                )
            while len(self.candidate_order) > max(1, int(self.candidate_store_limit)):
                stale = self.candidate_order.pop(0)
                self.candidate_evidence.pop(stale, None)
        return rows

    def read_candidates(
        self, candidate_ids: Iterable[str]
    ) -> tuple[list[Evidence], list[str]]:
        """Resolve candidate IDs; no admission happens here."""

        selected: list[Evidence] = []
        missing: list[str] = []
        with self._lock:
            for raw in candidate_ids:
                key = str(raw or "").strip()
                if not key:
                    continue
                item = self.candidate_evidence.get(key)
                if item is None:
                    missing.append(key)
                else:
                    selected.append(item)
        return selected, missing

    def drop_candidates(self, candidate_ids: Iterable[str]) -> None:
        """Forget candidates that were promoted into formal evidence."""

        with self._lock:
            for raw in candidate_ids:
                key = str(raw or "").strip()
                if not key:
                    continue
                self.candidate_evidence.pop(key, None)
                if key in self.candidate_order:
                    self.candidate_order.remove(key)

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

    def next_invocation_id(self) -> str:
        with self._lock:
            self._invocation_seq += 1
            return f"inv-{self._invocation_seq}"

    def record_invocation(self, diagnostic: dict[str, Any]) -> None:
        with self._lock:
            self.invocations.append(dict(diagnostic))

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

    def source_budget_snapshot(self) -> dict[str, Any]:
        coordinator = getattr(self, "evidence_budget_coordinator", None)
        if coordinator is None or not hasattr(coordinator, "snapshot"):
            return {}
        try:
            return dict(coordinator.snapshot())
        except Exception:
            return {}

    def record_multi_kb_result(
        self,
        source_statuses: list[dict[str, Any]],
        fusion_summary: dict[str, Any] | None = None,
    ) -> None:
        """Keep bounded source/fusion diagnostics for the final audit summary."""

        with self._lock:
            self.multi_kb_source_outcomes.extend(dict(item) for item in source_statuses)
            if fusion_summary:
                self.multi_kb_fusions.append(dict(fusion_summary))


def _query_fingerprint(tool_name: str, query: str) -> str:
    import hashlib

    return hashlib.sha256(f"{tool_name}\0{query}".encode("utf-8")).hexdigest()[:16]


def timed_tool_call(
    rt: ToolRuntime,
    tool_name: str,
    query: str,
    filters: dict[str, Any] | None,
    fn: Callable[[], list[Evidence]],
    *,
    candidate_only: bool = False,
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
    invocation_id = rt.next_invocation_id()
    was_repeat = rt.has_query(tool_name, query)
    with rt._lock:
        pre_registered = set(rt.evidence_index)
        pre_candidates = set(rt.candidate_evidence)
    rt.tool_started(
        call_id=invocation_id,
        tool_name=tool_name,
        query=query,
    )
    started = time.monotonic()
    call_epoch = rt.begin_tool_call()
    phase_timings: dict[str, int] = {}
    source_statuses: list[dict[str, Any]] = []
    try:
        rt.promote_pooled_candidates()
    except Exception:
        pass

    def invocation_diagnostic(status: str, *, admitted: int = 0, error: str = "") -> dict[str, Any]:
        budget = getattr(rt, "retrieval_budget", None)
        remaining_callable = getattr(budget, "remaining_seconds", None)
        calls_used = getattr(budget, "calls_used", 0)
        diagnostics = [dict(item.get("diagnostics") or {}) for item in source_statuses]
        return {
            "invocation_id": invocation_id,
            "tool_name": tool_name,
            "query_fingerprint": _query_fingerprint(tool_name, query),
            "filters": dict(filters or {}),
            "status": status,
            "error": error[:300],
            "admitted_items": admitted,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "phase_timings": dict(phase_timings),
            "source_operations": {
                "source_calls": len(source_statuses),
                "candidate_queries": sum(int(item.get("candidate_queries") or 0) for item in diagnostics),
                "validation_queries": sum(int(item.get("validation_queries") or 0) for item in diagnostics),
                "records": sum(int(item.get("records") or 0) for item in diagnostics),
                "remote_http_calls": sum(int(item.get("remote_http_calls") or 0) for item in diagnostics),
            },
            "budget_calls_used": int(calls_used or 0) if not callable(calls_used) else int(calls_used()),
            "budget_remaining_seconds": (round(float(remaining_callable()), 3)
                                         if callable(remaining_callable) else None),
        }
    try:
        from src.core.concurrency import governor_acquire

        governor_started = time.monotonic()
        with governor_acquire("tool", interactive=True):
            phase_timings["queue_wait_ms"] = int((time.monotonic() - governor_started) * 1000)
            execution_started = time.monotonic()
            items = fn()
            phase_timings["tool_execution_ms"] = int((time.monotonic() - execution_started) * 1000)
        # Late results from a cancelled run must never be registered as
        # evidence (P0-007); re-check cancellation after the call returns.
        rt.check_cancel()
    except QueryCancelled:
        raise
    except Exception as exc:
        # A live ACL revocation is a terminal execution condition for a
        # multi-KB turn.  Do not turn it into an apparently empty result that
        # lets the model continue with a partial, unauthorized scope.
        from src.services.knowledge_scope import KnowledgeScopeDenied

        if isinstance(exc, KnowledgeScopeDenied):
            rt.mark_ended()
            raise
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
        rt.tool_result(
            call_id=invocation_id,
            tool_name=tool_name,
            hit_count=0,
            evidence_count=0,
            status="failed",
            empty_reason="tool_error",
            latency_ms=int((time.monotonic() - started) * 1000),
            error=str(exc)[:300],
        )
        rt.emit(
            "degraded",
            {
                "stage": f"tool_{tool_name}",
                "reason": friendly_error_message(exc),
            },
        )
        rt.end_tool_call(call_epoch)
        rt.record_invocation(invocation_diagnostic("failed", error=str(exc)))
        failed = AdmittedEvidence()
        failed.failure = friendly_error_message(exc)
        failed.tool_name = tool_name
        rt.note_no_progress()
        return failed, False
    source_statuses = [dict(item) for item in getattr(items, "source_statuses", [])]
    fusion_summary = dict(getattr(items, "fusion_summary", {}) or {})
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        from src.observability.metrics import record_agent_tool_call

        record_agent_tool_call(tool=tool_name, status="ok")
    except Exception:
        pass
    rt.log_query(tool_name, query)
    navigation_only = bool(items) and all(
        str((item.metadata or {}).get("evidence_role") or "") == "navigation" for item in items
    )
    candidate_rows: list[dict[str, Any]] = []
    admission_items = items
    if navigation_only:
        for item in items:
            item.metadata = {**item.metadata, "citation_number": 0}
        numbers = CitationAdmission([None for _ in items], previously_registered=rt.evidence_index)
        admitted_items = AdmittedEvidence(items)
    elif candidate_only or (rt.candidate_mode and tool_name in {
        "document_search",
        "document_search_batch",
        "circuit_search",
        "conversation_search",
        "wiki_search",
        "spreadsheet_row_search",
        "spreadsheet_cell_lookup",
        "spreadsheet_schema_lookup",
        "spreadsheet_sql_query",
        "attachment_search",
        "attachment_table_query",
        "attachment_circuit_search",
    }):
        # Pointer-first: every search hit stays non-citable until evidence_read.
        candidate_rows = rt.stash_candidates(items)
        numbers = CitationAdmission([None for _ in items], previously_registered=rt.evidence_index)
        admitted_items = AdmittedEvidence()
    else:
        numbers = rt.add_evidence(
            admission_items,
            epoch=call_epoch,
        )
        admitted_items = AdmittedEvidence()
    rt.end_tool_call(call_epoch)
    admitted_items.source_statuses = source_statuses
    admitted_items.fusion_summary = fusion_summary
    admitted_items.previous_evidence_ids = numbers.previously_registered
    admitted_items.candidate_rows = candidate_rows
    admitted_items.candidate_count = len(candidate_rows)
    if not navigation_only:
        for item, number in zip(admission_items, numbers):
            if number is None:
                continue
            item.metadata = {**item.metadata, "citation_number": number}
            admitted_items.append(item)
    rt.record_invocation(invocation_diagnostic(
        "ok", admitted=0 if navigation_only else len(admitted_items),
        error="budget_discarded" if items and not admitted_items and not candidate_rows else "",
    ))
    if fusion_summary:
        with rt._lock:
            candidate_ids = {str(row.get("id") or "") for row in candidate_rows}
            fusion_summary["runtime_admission"] = {
                "admitted_items": len(admitted_items),
                "candidates_staged": len(candidate_rows),
                "budget_discarded": sum(
                    number is None and str(item.id or "") not in candidate_ids
                    for item, number in zip(admission_items, numbers)
                ),
                "rejection_reasons": dict(getattr(rt, "last_rejection_reasons", {}) or {}),
                "coverage": {
                    source.kb_id: any(item.metadata.get("kb_id") == source.kb_id for item in admitted_items)
                    for source in rt.knowledge_scope.sources
                },
                "turn_context_used": sum(_evidence_context_cost(item) for item in rt.evidence),
            }
            plan = getattr(rt, "turn_evidence_plan", None)
            if plan is not None:
                fusion_summary["runtime_admission"]["target_coverage"] = plan.coverage_snapshot()
    try:
        from collections import Counter

        from src.observability.metrics import counter

        candidate_counts = Counter(
            source_type_for_metadata(item.metadata) or "unknown" for item in items
        )
        admitted_counts = Counter(
            source_type_for_metadata(item.metadata) or "unknown"
            for item in admitted_items
        )
        for source_type, count in candidate_counts.items():
            counter(
                "hdb.evidence.candidate",
                attributes={"source_type": source_type},
                value=int(count),
                description="Retrieval candidates returned by a tool",
            )
        for source_type, count in admitted_counts.items():
            counter(
                "hdb.evidence.admitted",
                attributes={"source_type": source_type},
                value=int(count),
                description="Candidates admitted as formal evidence",
            )
    except Exception:
        pass
    if source_statuses:
        rt.record_multi_kb_result(source_statuses, fusion_summary)
    # Distinguish "the source has nothing", "the current turn scope filtered
    # it", and "the run already holds its full evidence budget". Only the last
    # one is an execution limit; labeling every empty admission as budget-full
    # makes a simple out-of-scope lookup look like a quota problem.
    rejection_reasons = dict(getattr(rt, "last_rejection_reasons", {}) or {})
    budget_reasons = {
        "call_quota",
        "item_limit",
        "byte_limit",
        "token_limit",
        "source_budget_exhausted",
        "global_budget_exhausted",
    }
    admitted_items.budget_blocked = bool(items) and not admitted_items and any(
        int(rejection_reasons.get(reason) or 0) > 0 for reason in budget_reasons
    )
    if not admitted_items:
        if admitted_items.budget_blocked:
            admitted_items.empty_reason = "budget_blocked"
        elif candidate_rows:
            # The call did return usable hits; they are deliberately not
            # registered yet, so this is not a retrieval "no match".
            admitted_items.empty_reason = ""
        elif items and getattr(rt, "turn_evidence_plan", None) is not None:
            admitted_items.empty_reason = "out_of_scope"
        else:
            admitted_items.empty_reason = "no_match"
    rt.record_diagnostic(
        ToolDiagnostics(
            tool_name=tool_name,
            hit_count=len(admitted_items),
            latency_ms=latency_ms,
            filters=dict(filters or {}),
        )
    )
    source_names = list(
        dict.fromkeys(
            str(item.source_name or "").strip()
            for item in admitted_items
            if str(item.source_name or "").strip()
        )
    )[:3]
    if not source_names:
        source_names = list(
            dict.fromkeys(
                str(row.get("source") or "").strip()
                for row in candidate_rows
                if str(row.get("source") or "").strip()
            )
        )[:3]
    new_evidence = any(
        number is not None and item.id not in pre_registered
        for item, number in zip(admission_items, numbers)
    )
    with rt._lock:
        new_candidates = bool(set(rt.candidate_evidence) - pre_candidates)
        new_sources = bool(set(source_names) - rt.seen_sources)
        rt.seen_sources.update(source_names)
    if new_evidence or new_sources or (new_candidates and not admitted_items.budget_blocked):
        rt.note_progress()
    else:
        rt.note_no_progress()
    citation_numbers = [
        int(item.metadata.get("citation_number") or 0)
        for item in admitted_items
        if int(item.metadata.get("citation_number") or 0) > 0
    ][:8]
    empty_reason = str(getattr(admitted_items, "empty_reason", "") or "")
    rt.tool_result(
        call_id=invocation_id,
        tool_name=tool_name,
        hit_count=len(admitted_items),
        evidence_count=len(admitted_items),
        candidate_count=len(candidate_rows),
        sources=source_names,
        citation_numbers=citation_numbers,
        empty_reason=empty_reason,
        status="done",
        latency_ms=latency_ms,
    )
    adds_nothing = bool(admitted_items) and all(
        item.id and item.id in (numbers.previously_registered if rt.knowledge_scope is not None else pre_registered)
        for item in admitted_items
    )
    return admitted_items, (was_repeat or rt.knowledge_scope is not None) and adds_nothing


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
        content_limit = (
            MAX_COMPARISON_CONTENT_CHARS
            if str(item.metadata.get("evidence_role") or "") == "comparison"
            else MAX_EVIDENCE_CONTENT_CHARS
        )
        content = str(item.content or "")[:content_limit]
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


def compress_evidence_for_context(items: list[Evidence], *, max_bytes: int) -> str:
    """Render a bounded evidence context while keeping stable identities.

    The compressed form never rewrites the underlying evidence: each block
    keeps its citation number, evidence id, source, locator and hash metadata,
    and truncates only the displayed body.  Full text remains available to
    citation readback by evidence id.
    """

    rendered = format_evidence_for_llm(items)
    budget = max(0, int(max_bytes or 0))
    if len(rendered.encode("utf-8")) <= budget:
        return rendered

    def _block(item: Evidence, content_limit: int, *, compressed: bool) -> str:
        number = int(item.metadata.get("citation_number") or 0)
        locator = item.locator or {}
        locator_text = ", ".join(
            f"{key}={value}" for key, value in locator.items() if value not in (None, "", [], {})
        )
        content = str(item.content or "")[: max(0, int(content_limit))]
        note = "\n（正文已压缩；完整原文可按 evidence id 回读。）" if compressed else ""
        return (
            f'<evidence id="{number}" source="{item.source_name}"'
            + (' compressed="true"' if compressed else "")
            + ">\n"
            f"[{number}] 来源：{item.source_name} | 位置：{locator_text or '未提供'}\n"
            f"{content}{note}\n"
            "</evidence>"
        )

    # Keep one source-aware representative as intact as possible.  A short
    # attachment (e.g. a one-part BOM table) must not be cut to its first rows
    # just because the other sources are long; per-source quota is more useful
    # than a global first-N truncation.
    groups: dict[str, list[Evidence]] = {}
    for item in items:
        groups.setdefault(str(item.source_name or ""), []).append(item)
    quota = max(240, budget // max(1, len(groups))) if budget else 0
    blocks: list[str] = []
    used = 0
    emitted: set[int] = set()
    for group in groups.values():
        first = group[0]
        full_block = _block(first, MAX_EVIDENCE_CONTENT_CHARS, compressed=False)
        full_size = len(full_block.encode("utf-8"))
        metadata = first.metadata or {}
        is_attachment = bool(
            metadata.get("attachment_id")
            or str(metadata.get("source_type") or "").lower() == "attachment"
            or (first.locator or {}).get("attachment_id")
        )
        # Short sources (especially a one-part BOM/requirements attachment) are
        # cheap to keep intact and carry high task value; do not cut them just
        # because a long PDF shares the budget.
        if (full_size <= 800 or (is_attachment and full_size <= 1200)) or (
            budget and full_size <= quota
        ):
            block = full_block
        else:
            block = _block(first, 280, compressed=True)
        encoded = len(block.encode("utf-8"))
        if budget and used + encoded > budget and blocks:
            break
        blocks.append(block)
        emitted.add(id(first))
        used += encoded
    for item in items:
        if id(item) in emitted:
            continue
        block = _block(item, 280, compressed=True)
        encoded = len(block.encode("utf-8"))
        if budget and used + encoded > budget and blocks:
            blocks.append("（其余已登记证据的正文已压缩，可按引用编号回读。）")
            break
        blocks.append(block)
        used += encoded
    return "\n\n".join(blocks)


def evidence_budget_message(rt: ToolRuntime) -> str:
    """Explain a tool call that was dropped because the evidence budget is full."""

    budget = int(getattr(rt, "evidence_budget", 0) or 0)
    held = len(getattr(rt, "evidence", []) or [])
    if rt.knowledge_scope is not None:
        source_status = rt.source_budget_snapshot()
        blocked = {
            source: snapshot.get("status")
            for source, snapshot in (source_status.get("slices") or {}).items()
            if snapshot.get("status") == "source_budget_exhausted"
        }
        blocked_text = (
            "；当前来源预算状态：" + "、".join(f"{key}={value}" for key, value in blocked.items())
            if blocked else ""
        )
        return (
            f"本轮证据条数或上下文预算已不足（当前 {held} 条）{blocked_text}，"
            "本次调用的新内容未纳入证据集；这不代表知识库中没有相关内容。"
            "请基于已登记证据作答，并说明仍缺失的信息。"
        )
    return (
        f"本轮可登记的证据已达上限（{budget} 条，当前 {held} 条），"
        "本次调用返回的新内容因超出预算未纳入证据集。"
        "这是单轮执行预算限制，不代表知识库或附件中没有相关内容。"
        "请勿继续调用检索/读取类工具翻找；请基于已获得的证据直接作答，"
        "并在回答中说明当前结论的证据范围与仍缺失的信息。"
    )


def tool_failure_message(items: list[Evidence]) -> str:
    """Render a tool/backend failure for the model.

    The distinction matters: "the source returned nothing" is a fact about the
    knowledge base, while "the tool failed" is a fact about this execution.
    Conflating them makes the model answer "没有找到" with full confidence when
    the backend was simply down.
    """
    failure = str(getattr(items, "failure", "") or "").strip()
    if not failure:
        return ""
    if failure == "turn_tool_budget_exhausted":
        return "（本轮工具调用预算已耗尽，本次调用未执行。这是执行预算限制，不代表来源为空。）"
    tool_name = str(getattr(items, "tool_name", "") or "工具").strip()
    return (
        f"（本次调用 {tool_name} 失败：{failure}。"
        "这是工具或后端故障，不代表知识库/附件中没有相关内容。"
        "请不要据此断言“没有找到”；如需该信息，可重试，"
        "或如实告知用户本次检索未完成。）"
    )


def empty_result_message(
    rt: ToolRuntime,
    default: str,
    *,
    budget_blocked: bool = False,
    items: list[Evidence] | None = None,
) -> str:
    """Return the accurate reason for an empty tool result."""

    if items is not None:
        failure_message = tool_failure_message(items)
        if failure_message:
            return failure_message
    if budget_blocked:
        return evidence_budget_message(rt)
    return default


def evidence_context_cost(item: Evidence) -> int:
    """Return the UTF-8 rendered byte cost used by every budget path.

    The field name is deliberately explicit: this is a conservative byte
    estimate, never a provider-reported token count.
    """

    # Count the actual tool rendering as well as the fusion estimate.  The
    # small margin covers citation-number growth after atomic admission.
    rendered_cost = len(format_evidence_for_llm([item]).encode("utf-8")) + 16
    return max(rendered_cost, int(item.metadata.get("context_token_estimate") or 0))


def _evidence_context_cost(item: Evidence) -> int:
    """Backwards-compatible private alias for existing callers."""

    return evidence_context_cost(item)


def format_navigation_for_llm(items: list[Evidence]) -> str:
    return "\n\n".join(
        f'<kb_navigation source="{item.source_name}">\n{str(item.content or "")[:MAX_EVIDENCE_CONTENT_CHARS]}\n</kb_navigation>'
        for item in items
    )


def format_candidates_for_llm(rows: list[dict[str, Any]], *, limit: int = 12) -> str:
    """Render the non-citable candidate index without dumping full content.

    The first ``limit`` rows keep their snippet; the rest stay addressable as
    compact pointers so ``evidence_read`` can still promote them. Hiding their
    ids entirely made large candidate sets unusable.
    """

    if not rows:
        return ""
    visible = list(rows)[: max(1, int(limit))]
    remaining = list(rows)[max(1, int(limit)):]
    lines = [
        "候选证据（尚未登记，不能直接引用；需要正文时调用 evidence_read）："
    ]
    for row in visible:
        score = float(row.get("score") or 0.0)
        lines.append(
            f"- id={row.get('id')} | {row.get('source') or '未知来源'}"
            f" | {row.get('locator') or '无位置'} | score={score:.2f}"
            f" | {row.get('snippet') or ''}"
        )
    if remaining:
        lines.append("其余候选指针（仅列 id/来源/位置，同样可 evidence_read）：")
        for row in remaining:
            lines.append(
                f"- id={row.get('id')} | {row.get('source') or '未知来源'}"
                f" | {row.get('locator') or '无位置'}"
            )
    return "\n".join(lines)


def _format_tool_result_inner(rt: ToolRuntime, adds_nothing: bool, items: list[Evidence]) -> str:
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
    statuses = [
        item for item in getattr(items, "source_statuses", [])
        if item.get("status") not in {"success_with_hits", "success_empty"}
    ]

    candidate_rows = list(getattr(items, "candidate_rows", []) or [])

    def with_status_message(rendered: str) -> str:
        if not statuses:
            return rendered
        rows = "\n".join(
            f"- kb:{item.get('kb_id')} {item.get('status')}: "
            f"{item.get('error_code') or item.get('status')}"
            for item in statuses
        )
        return f"{rendered}\n\n来源检索未完成（部分结果不可用）：\n{rows}"

    def with_candidates(rendered: str) -> str:
        candidate_text = format_candidates_for_llm(candidate_rows)
        return f"{rendered}\n\n{candidate_text}" if candidate_text else rendered

    if not items:
        if candidate_rows and not bool(getattr(items, "budget_blocked", False)):
            return with_status_message(with_candidates("（已返回候选，尚未登记证据。）"))
        if getattr(items, "empty_reason", "") == "out_of_scope":
            return with_status_message(
                "（本次检索结果被本轮来源范围或目标过滤；这不代表知识库没有相关内容。"
                "请检查来源范围，或明确说明当前范围无法覆盖该问题。）"
            )
        return with_status_message(empty_result_message(
            rt,
            "（未找到相关内容）",
            budget_blocked=bool(getattr(items, "budget_blocked", False)),
            items=items,
        ))
    if all(str((item.metadata or {}).get("evidence_role") or "") == "navigation" for item in items):
        return with_status_message(format_navigation_for_llm(items))
    if adds_nothing:
        with rt._lock:
            numbers = sorted({int(rt.evidence_index[item.id]) for item in items})
        cited = " ".join(f"[{number}]" for number in numbers)
        # 事实陈述，不带指令：是否继续引用 [n]、换关键词还是再次检索，
        # 由模型根据当前对话自行判断（策略在 SYSTEM_PROMPT，不在这里）。
        if candidate_rows:
            return with_status_message(with_candidates(
                f"本次调用返回的正式证据与历史消息中已注册的 {cited} 完全相同，无新增正式证据。"
            ))
        return with_status_message(
            f"本次调用返回的证据与历史消息中已注册的 {cited} 完全相同，无新增内容。"
        )
    if rt.knowledge_scope is not None:
        previous_ids = getattr(items, "previous_evidence_ids", set())
        previous = [item for item in items if item.id in previous_ids]
        if previous:
            fresh = [item for item in items if item.id not in previous_ids]
            cited = " ".join(f"[{item.metadata['citation_number']}]" for item in previous)
            return with_status_message(with_candidates(
                f"{format_evidence_for_llm(fresh)}\n\n已注册证据 {cited} 的正文见此前检索结果。"
            ))
    return with_status_message(with_candidates(format_evidence_for_llm(items)))


def format_tool_result(rt: ToolRuntime, adds_nothing: bool, items: list[Evidence]) -> str:
    rendered = _format_tool_result_inner(rt, adds_nothing, items)
    plan = getattr(rt, "turn_evidence_plan", None)
    if plan is not None:
        try:
            coverage = plan.coverage_snapshot()
        except Exception:
            coverage = {}
        if coverage.get("complete"):
            rendered += (
                "\n\n（本轮所有目标/查询义务均已取得已编号证据；"
                "请停止继续检索，直接基于现有证据作答。）"
            )
    return rendered
