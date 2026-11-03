"""Capability-layer retrieval: one call, many queries and source types.

The domain still exposes source-specific adapters internally, but the main
agent should not have to orchestrate every low-level search.  This tool fans
out a small batch of queries across the requested sources, keeps the
candidate-only evidence contract, and returns a compact coverage line so the
agent can decide the next hop from gaps rather than from raw tool payloads.
"""

from __future__ import annotations

from contextlib import nullcontext
import time
from typing import Any, Callable, Iterable

_MAX_QUERIES = 6
_MAX_TOP_K = 10
_DOCUMENT_BATCH = 4
_MAX_CIRCUIT_QUERIES = 3
_MAX_OPTIONAL_QUERIES = 2

_DEFAULT_SOURCES = ("document", "grep", "circuit")


def _clean_queries(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values or ():
        text = " ".join(str(value or "").split())
        if text and text not in result:
            result.append(text)
        if len(result) >= _MAX_QUERIES:
            break
    return result


def _clean_sources(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values or ():
        text = str(value or "").strip().casefold()
        if text and text not in result:
            result.append(text)
    return result


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _render_section(label: str, text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    return f"### {label}\n{value}"


def make_knowledge_search(
    rt: Any,
    *,
    document_search_batch: Callable[..., str] | None = None,
    document_search: Callable[..., str] | None = None,
    kb_grep: Callable[..., str] | None = None,
    circuit_search: Callable[..., str] | None = None,
    conversation_search: Callable[..., str] | None = None,
    wiki_search: Callable[..., str] | None = None,
    spreadsheet_row_search: Callable[..., str] | None = None,
) -> Callable[..., str]:
    """Build the capability-layer retrieval tool for this turn."""

    available = {
        "document": document_search_batch or document_search,
        "grep": kb_grep,
        "circuit": circuit_search,
        "conversation": conversation_search,
        "wiki": wiki_search,
        "spreadsheet": spreadsheet_row_search,
    }
    available = {name: tool for name, tool in available.items() if callable(tool)}

    def knowledge_search(
        queries: list[str],
        source_types: list[str] | None = None,
        top_k: int = 6,
    ) -> str:
        """一次检索多个查询和多个来源，返回候选指针与覆盖情况。

        queries 传 1-6 个相互独立的查询/型号/位号；source_types 可选
        document、grep、circuit、conversation、wiki、spreadsheet。
        默认检索 document/grep/circuit。该工具只产生候选，正文仍需
        evidence_read；适合需要多来源跳检的场景，避免逐个调用底层工具。
        """
        rt.check_cancel()
        requested_queries = _clean_queries(queries)
        call_id = ""
        started = time.monotonic()
        try:
            call_id = rt.next_invocation_id()
            rt.tool_started(
                call_id=call_id,
                tool_name="knowledge_search",
                tool_kind="retrieval",
                query="；".join(requested_queries),
            )
        except Exception:
            call_id = ""
        if not requested_queries:
            return "（未提供有效查询）"
        requested_sources = _clean_sources(source_types) or list(_DEFAULT_SOURCES)
        limit = max(1, min(int(top_k or 6), _MAX_TOP_K))
        sections: list[str] = [
            f"查询（{len(requested_queries)}）：" + "；".join(requested_queries),
            "来源：" + "、".join(requested_sources),
        ]
        executed: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        pre_candidates = set(getattr(rt, "candidate_evidence", {}) or {})
        pre_evidence = {
            getattr(item, "id", "")
            for item in (getattr(rt, "evidence", []) or [])
        }
        suppress_progress = getattr(rt, "suppress_progress", None)
        progress_context = (
            suppress_progress()
            if callable(suppress_progress)
            else nullcontext()
        )

        with progress_context:
          for source in requested_sources:
              tool = available.get(source)
              if tool is None:
                  skipped.append(source)
                  continue
              try:
                  if source == "document":
                      chunks = list(_chunks(requested_queries, _DOCUMENT_BATCH))
                      if document_search_batch is not None:
                          for group in chunks:
                              rendered = tool(group, top_k=limit)
                              section = _render_section(
                                  "document: " + " / ".join(group), rendered
                              )
                              if section:
                                  sections.append(section)
                      else:
                          for query in requested_queries[:_MAX_OPTIONAL_QUERIES]:
                              rendered = tool(query, top_k=limit)
                              section = _render_section(f"document: {query}", rendered)
                              if section:
                                  sections.append(section)
                  elif source == "grep":
                      rendered = tool(requested_queries, max_hits=max(10, limit * 3))
                      section = _render_section("grep", rendered)
                      if section:
                          sections.append(section)
                  elif source == "circuit":
                      for query in requested_queries[:_MAX_CIRCUIT_QUERIES]:
                          rendered = tool(query, top_k=limit)
                          section = _render_section(f"circuit: {query}", rendered)
                          if section:
                              sections.append(section)
                  else:
                      for query in requested_queries[:_MAX_OPTIONAL_QUERIES]:
                          rendered = tool(query, top_k=limit)
                          section = _render_section(f"{source}: {query}", rendered)
                          if section:
                              sections.append(section)
                  executed.append(source)
              except Exception as exc:  # noqa: BLE001 - one source must not sink the batch
                  failed.append(f"{source}: {type(exc).__name__}")

        with getattr(rt, "_lock", nullcontext()):
            new_candidates = bool(
                set(getattr(rt, "candidate_evidence", {}) or {}) - pre_candidates
            )
            new_evidence = any(
                getattr(item, "id", "") not in pre_evidence
                for item in (getattr(rt, "evidence", []) or [])
            )
        note_progress = getattr(rt, "note_progress", None)
        note_no_progress = getattr(rt, "note_no_progress", None)
        if callable(note_progress) and callable(note_no_progress):
            if new_candidates or new_evidence:
                note_progress()
            else:
                note_no_progress()

        coverage = ["【检索覆盖】", "- 已执行：" + ("、".join(executed) or "无")]
        if skipped:
            coverage.append("- 未配置/不可用：" + "、".join(skipped))
        if failed:
            coverage.append("- 执行失败：" + "；".join(failed))
        not_requested = [name for name in available if name not in requested_sources]
        if not_requested:
            coverage.append("- 本轮未请求：" + "、".join(not_requested))
        coverage.append("- 候选需经 evidence_read 才能成为正式证据")
        sections.append("\n".join(coverage))
        if call_id:
            try:
                rt.tool_result(
                    call_id=call_id,
                    tool_name="knowledge_search",
                    status="done",
                    tool_kind="retrieval",
                    hit_count=len(executed),
                    candidate_count=len(
                        set(getattr(rt, "candidate_evidence", {}) or {})
                        - pre_candidates
                    ),
                    sources=executed,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception:
                pass
        return "\n\n".join(section for section in sections if section)

    knowledge_search.__name__ = "knowledge_search"
    return knowledge_search


__all__ = ["make_knowledge_search"]
