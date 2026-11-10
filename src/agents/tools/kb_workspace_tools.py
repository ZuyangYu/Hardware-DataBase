"""Typed KB tools for the in-process agent.

Documents, tables and circuits have different parse/recall paths, so the tool
surface is split by type:

* ``list_knowledge_files``  -> directory across all types (with ``kind``)
* ``search_documents``      -> document chunks: RAGFlow semantic + local grep
* ``read_document``         -> document chunk text
* ``query_table`` / ``query_circuit`` remain the structured tools

The shared ``knowledge_retrieval`` module provides the lexical/grep/read
primitives used by both this adapter and the MCP bridge.
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


def _kind(record: Any) -> str:
    processor = str(getattr(record, "processor_kind", "") or "").casefold()
    if "spreadsheet" in processor:
        return "table"
    if "circuit" in processor:
        return "circuit"
    return "document"


def make_kb_workspace_tools(
    rt: Any,
    rag_backend: Any,
    document_store: Any,
    document_manager: Any = None,
    semantic_search: Any = None,
) -> list[Any]:
    """Return list/search/read document tools bound to the current KB scope."""

    chunk_backend = document_manager or rag_backend
    chunk_cache: dict[str, list[dict[str, Any]]] = {}

    def _records() -> list[Any]:
        return [
            record
            for record in _scoped_documents(rt, document_store)
            if str(getattr(record, "status", "") or "") in {"parsed", "indexed"}
        ]

    def _documents() -> list[Any]:
        return [record for record in _records() if _kind(record) == "document"]

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
                metadata = dict(getattr(chunk, "metadata", {}) or {})
                chunks.append(
                    {
                        "index": int(getattr(chunk, "index", 0) or 0),
                        "content": str(getattr(chunk, "content", "") or ""),
                        "metadata": metadata,
                        "rag_id": str(metadata.get("ragflow_chunk_id", "") or ""),
                    }
                )
        except Exception:
            chunks = []
        chunk_cache[key] = chunks
        return chunks

    def _chunks_by_document() -> dict[tuple[str, str], list[dict[str, Any]]]:
        return {
            (str(getattr(record, "id", "") or ""), _doc_name(record)): _chunks(record)
            for record in _documents()
        }

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

    def _semantic_results(query: str, top_k: int) -> list[dict[str, Any]]:
        runner = getattr(semantic_search, "run", None)
        if not callable(runner):
            return []
        items = runner(query, max(1, min(int(top_k or 8), 20)))
        results: list[dict[str, Any]] = []
        for item in items or []:
            metadata = dict(getattr(item, "metadata", {}) or {})
            locator = dict(getattr(item, "locator", {}) or {})
            results.append(
                {
                    "file_id": str(
                        metadata.get("local_record_id")
                        or metadata.get("document_id")
                        or ""
                    ),
                    "file_name": str(getattr(item, "source_name", "") or ""),
                    "chunk_index": int(
                        locator.get("chunk_index") or metadata.get("chunk_index") or 0
                    ),
                    "rag_id": str(metadata.get("chunk_id") or ""),
                    "locator": locator,
                    "score": float(getattr(item, "score", 0.0) or 0.0),
                    "snippet": str(getattr(item, "content", "") or "")[:400],
                }
            )
        return results

    def search_documents(
        query: str,
        mode: str = "auto",
        top_k: int = 8,
        file_id: str = "",
    ) -> str:
        """检索文档资料（Word/PDF/文本等），不检索表格/网表。

        mode="auto" 自动识别位号/型号/网络名并走精确匹配，否则走 RAGFlow
        语义检索；mode="exact" 强制本地 grep；mode="semantic" 强制 RAGFlow；
        返回 file_id、file_name、locator、chunk_index、score 和 snippet。
        """

        def _search() -> str:
            needle = str(query or "").strip()
            if not needle:
                return _json({"error": "query is required", "results": []})
            plane = str(mode or "auto").strip().lower()
            exact = plane == "exact" or (
                plane == "auto" and looks_like_exact_term(needle)
            )
            if exact:
                grouped = _chunks_by_document()
                if file_id:
                    grouped = {
                        key: value
                        for key, value in grouped.items()
                        if key[0] == str(file_id) or str(file_id) in key[1]
                    }
                results = grep_chunks(
                    grouped,
                    [needle],
                    max_hits=max(1, min(int(top_k or 8), 50)),
                )
                used = "exact"
            elif plane in {"semantic", "auto"}:
                results = _semantic_results(
                    needle, max(1, min(int(top_k or 8), 50))
                )
                used = "semantic"
                if not results:
                    results = lexical_search(
                        _chunks_by_document(),
                        needle,
                        top_k=max(1, min(int(top_k or 8), 50)),
                        file_id=file_id,
                    )
                    used = "lexical"
            else:
                results = lexical_search(
                    _chunks_by_document(),
                    needle,
                    top_k=max(1, min(int(top_k or 8), 50)),
                    file_id=file_id,
                )
                used = "lexical"
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
                }
            )

        return _run("search_documents", f"{mode} | {file_id} | {query}", _search)

    def read_document(
        file_id: str,
        start_index: int = 0,
        max_chunks: int = 20,
        query: str = "",
    ) -> str:
        """读取文档的解析 chunk；file_id 可传 id、文件名或虚拟路径。"""

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
                other = next(
                    (
                        item
                        for item in _records()
                        if _kind(item) != "document"
                        and (
                            str(getattr(item, "id", "")) == raw
                            or raw in _doc_name(item)
                        )
                    ),
                    None,
                )
                if other is not None:
                    return _json(
                        {
                            "error": "not a document; use query_table/query_circuit",
                            "kind": _kind(other),
                            "file_id": str(getattr(other, "id", "") or ""),
                        }
                    )
                return _json({"error": f"document not found: {raw}"})
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
                        int(picked[-1]["index"]) + 1
                        if len(picked) >= max_chunks
                        else None
                    ),
                }
            )

        return _run("read_document", f"{file_id} @{start_index}", _read)

    def list_knowledge_files(pattern: str = "**/*", kind: str = "") -> str:
        """列出知识库文件；pattern 支持通配符，kind 可限定 document/table/circuit。"""

        def _list() -> str:
            wanted = str(kind or "").strip().lower()
            records = [
                record
                for record in _records()
                if not wanted or _kind(record) == wanted
            ]
            files = list_files(records, pattern=pattern)
            for row in files:
                record = next(
                    (
                        item
                        for item in records
                        if str(getattr(item, "id", "")) == row["file_id"]
                    ),
                    None,
                )
                row["kind"] = _kind(record) if record is not None else row.get("kind", "")
            return _json(
                {
                    "pattern": str(pattern or "**/*"),
                    "kind": wanted,
                    "files": files,
                    "count": len(files),
                }
            )

        return _run("list_knowledge_files", f"{kind} | {pattern}", _list)

    search_documents.__name__ = "search_documents"
    read_document.__name__ = "read_document"
    list_knowledge_files.__name__ = "list_knowledge_files"
    return [search_documents, read_document, list_knowledge_files]


__all__ = ["make_kb_workspace_tools"]
