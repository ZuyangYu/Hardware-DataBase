"""Unified KB tools for the in-process agent.

The contract matches the external MCP bridge: ``search_knowledge`` returns
ranked chunks, ``read_knowledge`` returns parsed chunk text, and
``list_knowledge_files`` lists the virtual KB file tree.  ``grep`` is folded
into ``search_knowledge(mode="exact"/"auto")`` instead of being a third
overlapping tool.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.agents.tools.grep_tools import _scoped_documents
from src.services.knowledge_retrieval import (
    grep_chunks,
    lexical_search,
    list_files,
    looks_like_exact_term,
    read_chunks,
)

_PARSE_DEADLINE_SECONDS = 20.0


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _doc_name(record: Any) -> str:
    return str(
        getattr(record, "original_file_name", "")
        or getattr(record, "document_name", "")
        or getattr(record, "id", "")
        or "未命名文档"
    )


def make_kb_workspace_tools(
    rt: Any,
    rag_backend: Any,
    document_store: Any,
    document_manager: Any = None,
) -> list[Any]:
    """Return search/read/list tools bound to the current KB scope."""

    chunk_backend = document_manager or rag_backend
    chunk_cache: dict[str, list[dict[str, Any]]] = {}

    def _documents() -> list[Any]:
        return [
            record
            for record in _scoped_documents(rt, document_store)
            if str(getattr(record, "status", "") or "") in {"parsed", "indexed"}
        ]

    def _chunks(record: Any) -> list[dict[str, Any]]:
        key = str(getattr(record, "id", "") or getattr(record, "document_id", ""))
        cached = chunk_cache.get(key)
        if cached is not None:
            return cached
        chunks: list[dict[str, Any]] = []
        try:
            result = chunk_backend.get_parse_result(
                str(getattr(record, "kb_name", "")),
                f"ragflow:{getattr(record, 'id', 0)}",
                ctx=getattr(rt, "ctx", None),
                deadline=time.monotonic() + _PARSE_DEADLINE_SECONDS,
                should_cancel=getattr(rt, "should_cancel", None),
            )
            for chunk in getattr(result, "chunks", []) or []:
                chunks.append(
                    {
                        "index": int(getattr(chunk, "index", 0) or 0),
                        "content": str(getattr(chunk, "content", "") or ""),
                        "metadata": dict(getattr(chunk, "metadata", {}) or {}),
                        "rag_id": str(
                            (getattr(chunk, "metadata", {}) or {}).get(
                                "ragflow_chunk_id", ""
                            )
                            or ""
                        ),
                    }
                )
        except Exception:
            chunks = []
        chunk_cache[key] = chunks
        return chunks

    def _chunks_by_file() -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in _documents():
            fid = str(getattr(record, "id", "") or "")
            grouped[(fid, _doc_name(record))] = _chunks(record)
        return grouped

    def _run(name: str, query: str, fn):
        if rt is None:
            return fn()
        call_id = rt.next_invocation_id()
        rt.tool_started(
            call_id=call_id,
            tool_name=name,
            tool_kind="retrieval",
            query=str(query or "")[:200],
        )
        try:
            result = fn()
        except Exception as exc:
            rt.tool_result(
                call_id=call_id,
                tool_name=name,
                status="failed",
                tool_kind="retrieval",
                empty_reason=str(exc)[:200],
            )
            raise
        rt.tool_result(
            call_id=call_id,
            tool_name=name,
            status="success",
            tool_kind="retrieval",
        )
        return result

    def search_knowledge(
        query: str,
        mode: str = "auto",
        top_k: int = 8,
        file_id: str = "",
        path: str = "",
    ) -> str:
        """检索知识库解析 chunk。

        mode="auto" 会自动识别位号/型号/网络名并走精确 grep；mode="exact"
        强制精确匹配；mode="semantic"/"lexical" 走词法排序。结果包含
        file_id、locator、chunk_index、score 和上下文 snippet。需要正文时再调
        read_knowledge。
        """

        def _search() -> str:
            needle = str(query or "").strip()
            if not needle:
                return _json({"error": "query is required", "results": []})
            plane = str(mode or "auto").strip().lower()
            grouped = _chunks_by_file()
            exact = plane == "exact" or (
                plane == "auto" and looks_like_exact_term(needle)
            )
            if exact:
                results = grep_chunks(
                    grouped,
                    [needle],
                    path=path or file_id,
                    max_hits=max(1, min(int(top_k or 8), 50)),
                )
                used = "exact"
            else:
                results = lexical_search(
                    grouped,
                    needle,
                    top_k=max(1, min(int(top_k or 8), 50)),
                    file_id=file_id,
                )
                used = "semantic" if plane == "semantic" else "lexical"
            note_progress = getattr(rt, "note_progress", None)
            note_no_progress = getattr(rt, "note_no_progress", None)
            if results:
                if callable(note_progress):
                    note_progress()
            elif callable(note_no_progress):
                note_no_progress()
            return _json(
                {
                    "query": needle,
                    "mode": used,
                    "results": results,
                    "result_count": len(results),
                    "files_searched": len(grouped),
                    "chunks_searched": sum(len(items) for items in grouped.values()),
                }
            )

        return _run("search_knowledge", f"{mode} | {file_id} | {query}", _search)

    def read_knowledge(
        file_id: str,
        start_index: int = 0,
        max_chunks: int = 20,
        query: str = "",
    ) -> str:
        """读取知识库文件的解析 chunk；file_id 可传 id、文件名或虚拟路径。"""

        def _read() -> str:
            raw = str(file_id or "").strip()
            if not raw:
                return _json({"error": "file_id is required"})
            record = next(
                (
                    item
                    for item in _documents()
                    if str(getattr(item, "id", "")) == raw
                    or raw in _doc_name(item)
                    or raw in f"/kb/{getattr(item, 'kb_name', '')}/{_doc_name(item)}"
                ),
                None,
            )
            if record is None:
                return _json({"error": f"file not found: {raw}"})
            chunks = _chunks(record)
            picked = read_chunks(
                chunks,
                start_index=start_index,
                max_chunks=max_chunks,
                query=query,
            )
            if picked and callable(getattr(rt, "note_progress", None)):
                rt.note_progress()
            return _json(
                {
                    "file_id": str(getattr(record, "id", "") or ""),
                    "file_name": _doc_name(record),
                    "start_index": max(0, int(start_index or 0)),
                    "chunk_count": len(chunks),
                    "chunks": picked,
                    "next_index": (
                        int(picked[-1]["index"]) + 1 if len(picked) >= max_chunks else None
                    ),
                }
            )

        return _run("read_knowledge", f"{file_id} @{start_index}", _read)

    def list_knowledge_files(pattern: str = "**/*") -> str:
        """列出知识库文件；pattern 支持通配符，例如 **/*TCN2*。"""

        def _list() -> str:
            records = _documents()
            files = list_files(records, pattern=pattern)
            return _json(
                {
                    "pattern": str(pattern or "**/*"),
                    "files": files,
                    "count": len(files),
                }
            )

        return _run("list_knowledge_files", pattern, _list)

    search_knowledge.__name__ = "search_knowledge"
    read_knowledge.__name__ = "read_knowledge"
    list_knowledge_files.__name__ = "list_knowledge_files"
    return [search_knowledge, read_knowledge, list_knowledge_files]


__all__ = ["make_kb_workspace_tools"]
