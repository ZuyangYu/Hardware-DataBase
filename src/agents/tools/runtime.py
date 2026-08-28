"""Per-request runtime shared by all agent tools.

A ``ToolRuntime`` is created for every ``MultiSourceAgentRunner.stream`` call
and closed over by the tool function factories, so request-scoped values
(kb_name / RequestContext / cancellation / event callback) never leak between
concurrent sessions. It also collects evidence rows, tool diagnostics and the
query ledger that feed ``get_last_retrieval_summary`` and the observability
footer after the stream finishes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from src.agents.schemas import Evidence
from src.core.cancellation import QueryCancelled
from src.pipelines.document_rag.schemas import RequestContext

MAX_EVIDENCE_CONTENT_CHARS = 1200

TOOL_LABELS = {
    "document_search": "文档检索",
    "circuit_search": "电路检索",
    "spreadsheet_row_search": "表格行检索",
    "spreadsheet_cell_lookup": "单元格检索",
    "memory_search": "长期记忆检索",
    "list_kb_sources": "读取知识库目录",
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
    return TOOL_LABELS.get(tool_name, tool_name)


@dataclass
class ToolDiagnostics:
    tool_name: str
    hit_count: int = 0
    status: str = "ok"
    error: str = ""
    latency_ms: int = 0
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRuntime:
    kb_name: str
    ctx: RequestContext | None
    top_k: int = 6
    query_mode: str = "deep"
    should_cancel: Callable[[], bool] | None = None
    on_event: Callable[[dict], None] | None = None

    evidence: list[Evidence] = field(default_factory=list)
    evidence_index: dict[str, int] = field(default_factory=dict)
    memory_context: list[dict[str, Any]] = field(default_factory=list)
    memory_context_index: set[str] = field(default_factory=set)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check_cancel(self) -> None:
        if self.should_cancel is not None and self.should_cancel():
            raise QueryCancelled("用户取消查询")

    def emit(self, evt_type: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event({"type": evt_type, "payload": payload})
            except Exception:
                pass

    def log_query(self, tool_name: str, query: str) -> None:
        q = str(query or "").strip()
        if q and not any(item["tool_name"] == tool_name and item["query"] == q for item in self.queries):
            self.queries.append({"tool_name": tool_name, "query": q})

    def add_evidence(self, items: list[Evidence]) -> list[int]:
        """Register evidence rows and return their 1-based citation numbers."""
        numbers: list[int] = []
        with self._lock:
            for item in items:
                key = item.id or f"{item.source_name}:{len(self.evidence)}"
                if key in self.evidence_index:
                    numbers.append(self.evidence_index[key])
                    continue
                self.evidence.append(item)
                self.evidence_index[key] = len(self.evidence)
                numbers.append(len(self.evidence))
        return numbers

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

    def record_diagnostic(self, diag: ToolDiagnostics) -> None:
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
) -> list[Evidence]:
    """Shared wrapper: cancel-check, timing, diagnostics and event emission."""
    rt.check_cancel()
    rt.emit("tool_started", {"tool_name": tool_name, "query": query})
    started = time.monotonic()
    try:
        items = fn()
    except QueryCancelled:
        raise
    except Exception as exc:
        rt.record_diagnostic(
            ToolDiagnostics(
                tool_name=tool_name,
                status="failed",
                error=str(exc)[:300],
                latency_ms=int((time.monotonic() - started) * 1000),
                filters=dict(filters or {}),
            )
        )
        rt.emit("stage", {"key": "retrieve", "label": "多源硬件数据召回", "status": "error", "detail": f"{tool_label(tool_name)} 检索失败"})
        rt.emit("tool_result", {"tool_name": tool_name, "hit_count": 0, "status": "failed"})
        return []
    latency_ms = int((time.monotonic() - started) * 1000)
    rt.log_query(tool_name, query)
    rt.record_diagnostic(
        ToolDiagnostics(
            tool_name=tool_name,
            hit_count=len(items),
            latency_ms=latency_ms,
            filters=dict(filters or {}),
        )
    )
    numbers = rt.add_evidence(items)
    rt.emit(
        "stage",
        {
            "key": "retrieve",
            "label": "多源硬件数据召回",
            "status": "running",
            "detail": f"{tool_label(tool_name)} 返回 {len(items)} 条候选证据",
        },
    )
    rt.emit(
        "tool_result",
        {"tool_name": tool_name, "hit_count": len(items), "status": "running"},
    )
    # Citation numbers follow registration order so the LLM can cite [n].
    for item, number in zip(items, numbers):
        item.metadata = {**item.metadata, "citation_number": number}
    return items


def format_evidence_for_llm(items: list[Evidence]) -> str:
    if not items:
        return "（未找到相关内容）"
    blocks = []
    for item in items:
        number = int(item.metadata.get("citation_number") or 0)
        content = str(item.content or "")[:MAX_EVIDENCE_CONTENT_CHARS]
        locator = item.locator or {}
        locator_text = ", ".join(f"{k}={v}" for k, v in locator.items() if v not in (None, ""))
        header = f"[{number}] 来源：{item.source_name} | 类型：{item.content_kind}"
        if locator_text:
            header += f" | 位置：{locator_text}"
        blocks.append(f"{header}\n{content}")
    return "\n\n".join(blocks)
