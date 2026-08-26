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
    "list_kb_sources": "读取知识库目录",
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
