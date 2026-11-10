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

import hashlib
import json
import time
from typing import Any

from src.agents.schemas import Evidence
from src.agents.tools.grep_tools import _scoped_documents
from src.services.evidence_sources import (
    KNOWLEDGE_BASE_DOCUMENT,
    with_source_metadata,
)
from src.services.pipeline_directories import (
    PipelineDirectoryService,
    document_relative_path,
)
from src.services.knowledge_retrieval import (
    grep_chunks,
    lexical_search,
    list_files,
    looks_like_exact_term,
    read_chunks,
)

_PARSE_DEADLINE_SECONDS = 20.0
_LOCATOR_KEYS = ("page_num", "page", "sheet_name", "row_index", "chunk_index")


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


def _chunk_locator(metadata: dict[str, Any], fallback: int) -> dict[str, Any]:
    locator = {
        key: metadata[key]
        for key in _LOCATOR_KEYS
        if metadata.get(key) not in (None, "", [], {})
    }
    return locator or {"chunk_index": int(fallback or 0)}


def _evidence_from_chunk(
    record: Any,
    chunk: dict[str, Any],
    *,
    score: float = 0.0,
    relative_path: str = "",
) -> Evidence:
    """Build one formal Evidence row from a parsed chunk and its record."""

    metadata = dict(chunk.get("metadata") or {})
    content = str(chunk.get("content") or "")
    index = int(chunk.get("index") or 0)
    chunk_id = str(
        chunk.get("rag_id")
        or metadata.get("ragflow_chunk_id")
        or metadata.get("chunk_id")
        or index
    )
    locator = _chunk_locator(metadata, index)
    kb_id = int(getattr(record, "kb_id", 0) or 0)
    evidence_metadata = with_source_metadata(
        {
            "source_type": KNOWLEDGE_BASE_DOCUMENT,
            "kb_id": kb_id,
            "kb_name": str(getattr(record, "kb_name", "") or ""),
            "local_record_id": int(getattr(record, "id", 0) or 0),
            "document_id": str(getattr(record, "document_id", "") or ""),
            "chunk_id": chunk_id,
            "source_content_hash": str(getattr(record, "content_hash", "") or ""),
            "evidence_text_hash": hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest(),
            "source_group": str(getattr(record, "source_group", "") or ""),
            "status": str(getattr(record, "status", "") or ""),
            "processor_kind": str(getattr(record, "processor_kind", "") or ""),
            "content_kind": str(getattr(record, "content_kind", "") or ""),
            "retrieval_tool": "kb_workspace",
            "relative_path": relative_path or _doc_name(record),
        },
        content=content,
        locator=locator,
    )
    return Evidence(
        id=f"doc:{getattr(record, 'id', 0)}:{chunk_id}",
        content=content,
        source_name=relative_path or _doc_name(record),
        content_kind=str(getattr(record, "content_kind", "") or "document_text"),
        processor_kind=str(getattr(record, "processor_kind", "") or "ragflow"),
        score=float(score or 0.0),
        locator=locator,
        metadata=evidence_metadata,
    )


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

    def _documents(allowed: set[int] | None = None) -> list[Any]:
        return [
            record
            for record in _records()
            if _kind(record) == "document"
            and (
                allowed is None
                or int(getattr(record, "directory_id", 0) or 0) in allowed
            )
        ]

    def _relative_name(record: Any) -> str:
        """Return the directory-aware path used in evidence and listings."""

        try:
            value = document_relative_path(document_store, record)
        except Exception:
            value = ""
        return str(value or _doc_name(record))

    def _allowed_directory_ids(directory_id: int, recursive: bool) -> set[int] | None:
        """Resolve a directory filter; ``None`` means the whole knowledge base."""

        target = int(directory_id or 0)
        if target <= 0:
            return None
        if not recursive or document_store is None:
            return {target}
        metadata = dict(getattr(getattr(rt, "ctx", None), "metadata", {}) or {})
        department_id = str(
            metadata.get("resource_department_id")
            or metadata.get("department_id")
            or ""
        )
        try:
            return PipelineDirectoryService(store=document_store).descendant_ids(
                kb_name=str(getattr(rt, "kb_name", "") or ""),
                department_id=department_id,
                directory_id=target,
            )
        except Exception:
            # A stale/deleted id must not silently widen to the whole KB.
            return set()

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

    def _chunks_by_document(
        allowed: set[int] | None = None,
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        return {
            (str(getattr(record, "id", "") or ""), _relative_name(record)): _chunks(record)
            for record in _documents(allowed)
        }

    def _run(
        name: str,
        query: str,
        fn,
        *,
        stats: dict[str, Any] | None = None,
    ):
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
        values = dict(stats or {})
        rt.tool_result(
            call_id=call_id,
            tool_name=name,
            status="success",
            tool_kind="retrieval",
            hit_count=int(values.get("hit_count", 0) or 0),
            evidence_count=int(values.get("evidence_count", 0) or 0),
            sources=list(values.get("sources", []) or []),
            citation_numbers=list(values.get("citation_numbers", []) or []),
            empty_reason=(
                "no_match"
                if int(values.get("hit_count", 0) or 0) == 0
                else ""
            ),
        )
        return result

    def _resolve_row(row: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
        """Resolve a search hit back to its record and full local chunk."""

        raw_id = str(row.get("file_id") or "")
        file_name = str(row.get("file_name") or "")
        record = next(
            (
                item
                for item in _documents()
                if str(getattr(item, "id", "")) == raw_id
            ),
            None,
        )
        if record is None and file_name:
            record = next(
                (
                    item
                    for item in _documents()
                    if file_name in {_relative_name(item), _doc_name(item)}
                ),
                None,
            )
        if record is None:
            return None, None
        wanted_rag = str(row.get("rag_id") or "")
        wanted_index = int(row.get("chunk_index") or 0)
        chunks = _chunks(record)
        chunk = next(
            (
                item
                for item in chunks
                if wanted_rag and str(item.get("rag_id") or "") == wanted_rag
            ),
            None,
        )
        if chunk is None:
            chunk = next(
                (
                    item
                    for item in chunks
                    if int(item.get("index") or 0) == wanted_index
                ),
                None,
            )
        return record, chunk

    def _admit_search_results(
        rows: list[dict[str, Any]],
    ) -> tuple[int, list[int], list[str]]:
        """Register search hits as formal evidence and stamp citation numbers."""

        pairs: list[tuple[dict[str, Any], Evidence]] = []
        for row in rows:
            record, chunk = _resolve_row(row)
            if record is None:
                continue
            if chunk is None:
                # Semantic backends can return a chunk that is not present in
                # the local parse result.  Keep the snippet citable rather than
                # dropping the hit, but mark the reduced body.
                chunk = {
                    "index": int(row.get("chunk_index") or 0),
                    "content": str(row.get("snippet") or ""),
                    "metadata": dict(row.get("locator") or {}),
                    "rag_id": str(row.get("rag_id") or ""),
                }
                chunk["metadata"]["evidence_role"] = "search_snippet"
            pairs.append(
                (
                    row,
                    _evidence_from_chunk(
                        record,
                        chunk,
                        score=float(row.get("score") or 0.0),
                        relative_path=_relative_name(record),
                    ),
                )
            )
        if not pairs or rt is None:
            return 0, [], []
        numbers = rt.add_evidence([item for _, item in pairs])
        admitted = 0
        citations: list[int] = []
        sources: list[str] = []
        for (row, item), number in zip(pairs, numbers):
            if number is None:
                row["citation_number"] = None
                continue
            row["citation_number"] = int(number)
            admitted += 1
            citations.append(int(number))
            if item.source_name not in sources:
                sources.append(item.source_name)
        if admitted:
            note_progress = getattr(rt, "note_progress", None)
            if callable(note_progress):
                note_progress()
        return admitted, citations, sources

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
        directory_id: int = 0,
        recursive: bool = True,
    ) -> str:
        """检索文档资料（Word/PDF/文本等），不检索表格/网表。

        mode="auto" 自动识别位号/型号/网络名并走精确匹配，否则走 RAGFlow
        语义检索；mode="exact" 强制本地 grep；mode="semantic" 强制 RAGFlow；
        directory_id + recursive 可把检索限定到某个目录及其后代。
        返回 file_id、file_name、locator、chunk_index、score 和 snippet。
        """

        stats: dict[str, Any] = {}
        allowed_directory_ids = _allowed_directory_ids(directory_id, recursive)

        def _search() -> str:
            needle = str(query or "").strip()
            if not needle:
                return _json({"error": "query is required", "results": []})
            plane = str(mode or "auto").strip().lower()
            exact = plane == "exact" or (
                plane == "auto" and looks_like_exact_term(needle)
            )
            if exact:
                grouped = _chunks_by_document(allowed_directory_ids)
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
                        _chunks_by_document(allowed_directory_ids),
                        needle,
                        top_k=max(1, min(int(top_k or 8), 50)),
                        file_id=file_id,
                    )
                    used = "lexical"
            else:
                results = lexical_search(
                    _chunks_by_document(allowed_directory_ids),
                    needle,
                    top_k=max(1, min(int(top_k or 8), 50)),
                    file_id=file_id,
                )
                used = "lexical"
            if allowed_directory_ids is not None:
                filtered: list[dict[str, Any]] = []
                for row in results:
                    record, _chunk = _resolve_row(row)
                    if record is not None and int(
                        getattr(record, "directory_id", 0) or 0
                    ) in allowed_directory_ids:
                        filtered.append(row)
                results = filtered
            admitted, citations, sources = _admit_search_results(results)
            stats.update(
                hit_count=len(results),
                evidence_count=admitted,
                citation_numbers=citations,
                sources=sources,
            )
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
                    "evidence_count": admitted,
                }
            )

        return _run(
            "search_documents",
            f"{mode} | {file_id} | {query}",
            _search,
            stats=stats,
        )

    def read_document(
        file_id: str,
        start_index: int = 0,
        max_chunks: int = 20,
        query: str = "",
    ) -> str:
        """读取文档的解析 chunk；file_id 可传 id、文件名或虚拟路径。"""

        stats: dict[str, Any] = {}

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
            by_index = {
                int(item.get("index") or 0): item for item in chunks
            }
            evidence_items = []
            for item in picked:
                source_chunk = by_index.get(int(item.get("index") or 0)) or {
                    "index": item.get("index"),
                    "content": item.get("content") or "",
                    "metadata": item.get("metadata") or {},
                    "rag_id": item.get("rag_id") or "",
                }
                evidence_items.append(
                    _evidence_from_chunk(
                        record,
                        source_chunk,
                        score=1.0,
                        relative_path=_relative_name(record),
                    )
                )
            citations: list[int] = []
            if evidence_items and rt is not None:
                numbers = rt.add_evidence(evidence_items)
                for item, number in zip(picked, numbers):
                    item["citation_number"] = (
                        int(number) if number is not None else None
                    )
                    if number is not None:
                        citations.append(int(number))
            stats.update(
                hit_count=len(picked),
                evidence_count=len(citations),
                citation_numbers=citations,
                sources=[_doc_name(record)] if picked else [],
            )
            if picked and callable(getattr(rt, "note_progress", None)):
                rt.note_progress()
            return _json(
                {
                    "file_id": str(getattr(record, "id", "") or ""),
                    "file_name": _relative_name(record),
                    "start_index": max(0, int(start_index or 0)),
                    "chunk_count": len(chunks),
                    "chunks": picked,
                    "evidence_count": len(citations),
                    "next_index": (
                        int(picked[-1]["index"]) + 1
                        if len(picked) >= max_chunks
                        else None
                    ),
                }
            )

        return _run(
            "read_document",
            f"{file_id} @{start_index}",
            _read,
            stats=stats,
        )

    def list_knowledge_files(
        pattern: str = "**/*",
        kind: str = "",
        directory_id: int = 0,
        recursive: bool = True,
    ) -> str:
        """列出知识库文件；pattern 支持通配符，kind 可限定 document/table/circuit。

        directory_id + recursive 可把列表限定到某个目录及其后代。
        """

        stats: dict[str, Any] = {}
        allowed_directory_ids = _allowed_directory_ids(directory_id, recursive)

        def _list() -> str:
            wanted = str(kind or "").strip().lower()
            records = [
                record
                for record in _records()
                if (not wanted or _kind(record) == wanted)
                and (
                    allowed_directory_ids is None
                    or int(getattr(record, "directory_id", 0) or 0)
                    in allowed_directory_ids
                )
            ]
            files = list_files(
                records, pattern=pattern, path_for=_relative_name
            )
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
            stats["hit_count"] = len(files)
            return _json(
                {
                    "pattern": str(pattern or "**/*"),
                    "kind": wanted,
                    "directory_id": int(directory_id or 0),
                    "recursive": bool(recursive),
                    "files": files,
                    "count": len(files),
                }
            )

        return _run(
            "list_knowledge_files",
            f"{kind} | {pattern}",
            _list,
            stats=stats,
        )

    search_documents.__name__ = "search_documents"
    read_document.__name__ = "read_document"
    list_knowledge_files.__name__ = "list_knowledge_files"
    return [search_documents, read_document, list_knowledge_files]


__all__ = ["make_kb_workspace_tools"]
