"""Pointer-first lexical retrieval over parsed knowledge-base documents.

``kb_grep`` scans the parsed chunks of the scoped documents, returns only
``id/source/locator/snippet`` pointers, and never registers formal evidence.
The model promotes selected pointers through ``evidence_read``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import closing
from typing import Any

from src.agents.schemas import Evidence
from src.agents.tools.pointer_index import (
    PointerIndex,
    pointer_index_path,
    records_signature,
)
from src.agents.tools.runtime import format_tool_result, timed_tool_call
from src.services.pipeline_directories import (
    PipelineDirectoryService,
    document_relative_path,
)

_GREP_CACHE_TTL_SECONDS = 300.0
_GREP_MAX_CHUNKS_PER_SOURCE = 5000


def _scoped_documents(rt: Any, document_store: Any) -> list[Any]:
    if document_store is None:
        return []
    scopes: list[tuple[str, str, int]] = []
    knowledge_scope = getattr(rt, "knowledge_scope", None)
    if knowledge_scope is not None:
        for source in getattr(knowledge_scope, "sources", []) or []:
            name = str(getattr(source, "kb_name", "") or "").strip()
            department = str(getattr(source, "department_id", "") or "").strip()
            kb_id = int(getattr(source, "kb_id", 0) or 0)
            if name and department:
                scopes.append((name, department, kb_id))
    elif getattr(rt, "kb_name", ""):
        metadata = dict(getattr(getattr(rt, "ctx", None), "metadata", {}) or {})
        department = str(
            metadata.get("resource_department_id") or metadata.get("department_id") or ""
        ).strip()
        kb_id = int(metadata.get("kb_id") or 0)
        if department:
            scopes.append((str(rt.kb_name), department, kb_id))
    documents: list[Any] = []
    for kb_name, department, _ in scopes:
        try:
            documents.extend(document_store.list_documents(kb_name, department))
        except Exception:
            continue
    if not documents and getattr(rt, "kb_name", ""):
        # The worker context does not always carry the display department;
        # the runner/tool surface already established KB read access.
        try:
            documents.extend(document_store.list_documents_unscoped(str(rt.kb_name)))
        except Exception:
            pass
    return documents


def _chunk_locator(metadata: dict[str, Any], fallback: int) -> dict[str, Any]:
    locator: dict[str, Any] = {}
    for key in (
        "page_num", "page", "sheet_name", "row_index", "positions",
        "chunk_index", "section", "heading",
    ):
        value = metadata.get(key)
        if value not in (None, "", [], {}):
            locator[key] = value
    if not locator:
        locator["chunk_index"] = fallback
    return locator


def _chunk_evidence(record: Any, chunk: Any, kb_id: int, document_store: Any = None) -> Evidence:
    metadata = dict(getattr(chunk, "metadata", {}) or {})
    content = str(getattr(chunk, "content", "") or "")
    chunk_id = str(
        metadata.get("ragflow_chunk_id")
        or metadata.get("chunk_id")
        or getattr(chunk, "index", 0)
    )
    evidence_id = f"grep:{getattr(record, 'id', 0)}:{chunk_id}"
    locator = _chunk_locator(metadata, int(getattr(chunk, "index", 0) or 0))
    relative_path = document_relative_path(document_store, record) if document_store is not None else str(
        getattr(record, "document_name", "") or "未命名文档"
    )
    return Evidence(
        id=evidence_id,
        content=content,
        source_name=str(
            relative_path or getattr(record, "original_file_name", "") or "未命名文档"
        ),
        content_kind=str(getattr(record, "content_kind", "") or "document_text"),
        processor_kind=str(getattr(record, "processor_kind", "") or "ragflow"),
        score=float(metadata.get("score") or 0.0),
        locator=locator,
        metadata={
            "kb_id": int(kb_id or getattr(record, "kb_id", 0) or 0),
            "kb_name": str(getattr(record, "kb_name", "") or ""),
            "local_record_id": int(getattr(record, "id", 0) or 0),
            "directory_id": int(getattr(record, "directory_id", 0) or 0),
            "document_id": str(getattr(record, "document_id", "") or ""),
            "chunk_id": chunk_id,
            "source_content_hash": str(getattr(record, "content_hash", "") or ""),
            "evidence_text_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "source_group": str(getattr(record, "source_group", "") or ""),
            "status": str(getattr(record, "status", "") or ""),
            "processor_kind": str(getattr(record, "processor_kind", "") or "ragflow"),
            "content_kind": str(getattr(record, "content_kind", "") or "document_text"),
            "relative_path": relative_path,
        },
    )


def _xlsx_candidates(
    rt: Any,
    records: list[Any],
    spreadsheet_service: Any,
    patterns: list[re.Pattern[str]],
    *,
    regex: bool,
    limit: int,
    seen: set[str],
) -> list[tuple[int, Evidence]]:
    if spreadsheet_service is None or not hasattr(spreadsheet_service, "db_path"):
        return []
    rows: list[tuple[int, Evidence]] = []
    for record in records:
        if str(getattr(record, "processor_kind", "") or "") != "spreadsheet_table":
            continue
        if str(getattr(record, "status", "") or "") not in {"indexed", "parsed"}:
            continue
        try:
            db_path = spreadsheet_service.db_path(
                getattr(record, "department_id", ""), getattr(record, "kb_name", ""), create=False
            )
        except Exception:
            continue
        if not db_path:
            continue
        sql = (
            "SELECT sheet_name, row_index, semantic_text, raw_text "
            "FROM table_semantic_rows WHERE record_id = ?"
        )
        params: list[Any] = [int(getattr(record, "id", 0) or 0)]
        if not regex:
            sql += " AND (" + " OR ".join("semantic_text LIKE ? OR raw_text LIKE ?" for _ in patterns) + ")"
            for pattern in patterns:
                like = f"%{pattern.pattern}%"
                params.extend([like, like])
        sql += " LIMIT 2000"
        try:
            with closing(sqlite3.connect(db_path, timeout=10)) as conn:
                fetched = conn.execute(sql, params).fetchall()
        except Exception:
            continue
        relative_path = document_relative_path(getattr(rt, "document_store", None), record)
        for sheet_name, row_index, semantic_text, raw_text in fetched:
            content = str(semantic_text or raw_text or "")
            if not content or not any(pattern.search(content) for pattern in patterns):
                continue
            evidence_id = f"grep:xlsx:{getattr(record, 'id', 0)}:{sheet_name}:{row_index}"
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            rows.append(
                (
                    sum(len(pattern.findall(content)) for pattern in patterns),
                    Evidence(
                        id=evidence_id,
                        content=content,
                        source_name=relative_path or str(getattr(record, "document_name", "")),
                        content_kind=str(getattr(record, "content_kind", "") or "spreadsheet_table"),
                        processor_kind=str(getattr(record, "processor_kind", "") or "spreadsheet_table"),
                        score=1.0,
                        locator={"sheet_name": sheet_name, "row_index": row_index},
                        metadata={
                            "kb_id": int(getattr(record, "kb_id", 0) or 0),
                            "kb_name": str(getattr(record, "kb_name", "") or ""),
                            "local_record_id": int(getattr(record, "id", 0) or 0),
                            "document_id": str(getattr(record, "document_id", "") or ""),
                            "sheet_name": sheet_name,
                            "row_index": row_index,
                            "source_content_hash": str(getattr(record, "content_hash", "") or ""),
                            "evidence_text_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                            "processor_kind": "spreadsheet_table",
                            "content_kind": str(getattr(record, "content_kind", "") or "spreadsheet_table"),
                            "directory_id": int(getattr(record, "directory_id", 0) or 0),
                            "relative_path": relative_path,
                        },
                    ),
                )
            )
            if len(rows) >= limit:
                return rows
    return rows


def _circuit_candidates(
    rt: Any,
    records: list[Any],
    circuit_service: Any,
    patterns: list[re.Pattern[str]],
    *,
    limit: int,
    seen: set[str],
) -> list[tuple[int, Evidence]]:
    if circuit_service is None or not hasattr(circuit_service, "query"):
        return []
    kb_names = sorted({
        str(getattr(record, "kb_name", "") or "")
        for record in records
        if str(getattr(record, "processor_kind", "") or "") == "circuit_design"
        and str(getattr(record, "kb_name", "") or "")
    })
    rows: list[tuple[int, Evidence]] = []
    for kb_name in kb_names:
        try:
            items = circuit_service.query(
                kb_name=kb_name,
                query=" ".join(pattern.pattern for pattern in patterns),
                ctx=getattr(rt, "ctx", None),
                top_k=min(20, limit),
            )
        except Exception:
            continue
        for item in items or []:
            if not item.id or item.id in seen:
                continue
            content = str(item.content or "")
            if not content or not any(pattern.search(content) for pattern in patterns):
                continue
            seen.add(item.id)
            rows.append((sum(len(pattern.findall(content)) for pattern in patterns), item))
            if len(rows) >= limit:
                return rows
    return rows


def _all_circuit_items(
    records: list[Any], circuit_service: Any, document_store: Any = None, *, limit: int = 5000
) -> list[Evidence]:
    store = getattr(circuit_service, "store", None) if circuit_service is not None else None
    if store is None or not hasattr(store, "list_designs"):
        return []
    source_by_kb: dict[str, Any] = {}
    for record in records:
        if str(getattr(record, "processor_kind", "") or "") != "circuit_design":
            continue
        source_by_kb.setdefault(str(getattr(record, "kb_name", "") or ""), record)
    items: list[Evidence] = []
    for kb_name, record in source_by_kb.items():
        source_name = document_relative_path(document_store, record) or "电路网表"
        try:
            designs = store.list_designs(kb_name)
        except Exception:
            continue
        for design in designs:
            design_id = str(getattr(design, "design_id", "") or "")
            for net in getattr(design, "nets", []) or []:
                content = " ".join(
                    [str(getattr(net, "name", "") or "")]
                    + [
                        f"{getattr(conn, 'refdes', '')}.{getattr(conn, 'pin', '') or ''}"
                        for conn in (getattr(net, "connections", []) or [])[:12]
                    ]
                ).strip()
                if not content:
                    continue
                key = hashlib.sha1(f"{design_id}:net:{content}".encode("utf-8")).hexdigest()[:16]
                items.append(
                    Evidence(
                        id=f"grep:circuit:{key}",
                        content=content,
                        source_name=source_name,
                        content_kind="circuit_net",
                        processor_kind="circuit",
                        score=0.5,
                        locator={"design_id": design_id, "net": str(getattr(net, "name", "") or "")},
                        metadata={
                            "kb_id": int(getattr(record, "kb_id", 0) or 0),
                            "kb_name": kb_name,
                            "local_record_id": int(getattr(record, "id", 0) or 0),
                            "design_id": design_id,
                            "net_name": str(getattr(net, "name", "") or ""),
                            "processor_kind": "circuit",
                            "content_kind": "circuit_net",
                            "source_content_hash": str(getattr(record, "content_hash", "") or ""),
                        },
                    )
                )
            for instance in getattr(design, "instances", []) or []:
                content = " ".join(
                    str(value or "")
                    for value in (
                        getattr(instance, "refdes", ""),
                        getattr(instance, "part_number", ""),
                        getattr(instance, "value", ""),
                        getattr(instance, "library_cell", ""),
                    )
                ).strip()
                if not content:
                    continue
                key = hashlib.sha1(f"{design_id}:inst:{content}".encode("utf-8")).hexdigest()[:16]
                items.append(
                    Evidence(
                        id=f"grep:circuit:{key}",
                        content=content,
                        source_name=source_name,
                        content_kind="circuit_instance",
                        processor_kind="circuit",
                        score=0.5,
                        locator={"design_id": design_id, "refdes": str(getattr(instance, "refdes", "") or "")},
                        metadata={
                            "kb_id": int(getattr(record, "kb_id", 0) or 0),
                            "kb_name": kb_name,
                            "local_record_id": int(getattr(record, "id", 0) or 0),
                            "design_id": design_id,
                            "refdes": str(getattr(instance, "refdes", "") or ""),
                            "processor_kind": "circuit",
                            "content_kind": "circuit_instance",
                            "source_content_hash": str(getattr(record, "content_hash", "") or ""),
                        },
                    )
                )
                if len(items) >= limit:
                    return items
            if len(items) >= limit:
                return items
    return items


def make_kb_grep(rt: Any, rag_backend: Any, document_store: Any, spreadsheet_service: Any = None, circuit_service: Any = None):
    """Return a multi-pattern lexical search tool that returns pointers only."""

    cache: dict[tuple[str, str], tuple[float, list[Evidence]]] = {}

    def _allowed_directory_ids(directory_id: int, recursive: bool) -> set[int] | None:
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
            return set()

    def _document_chunks(record: Any, kb_id: int) -> list[Evidence]:
        key = (str(getattr(record, "kb_name", "")), str(getattr(record, "document_id", "")))
        now = time.monotonic()
        cached = cache.get(key)
        if cached is not None and now - cached[0] <= _GREP_CACHE_TTL_SECONDS:
            return cached[1]
        result = rag_backend.get_parse_result(
            str(getattr(record, "kb_name", "")),
            f"ragflow:{getattr(record, 'id', 0)}",
            ctx=getattr(rt, "ctx", None),
            deadline=now + 20,
            should_cancel=getattr(rt, "should_cancel", None),
        )
        chunks = list(getattr(result, "chunks", []) or [])[:_GREP_MAX_CHUNKS_PER_SOURCE]
        items = [
            _chunk_evidence(record, chunk, kb_id, document_store)
            for chunk in chunks
        ]
        cache[key] = (now, items)
        return items

    def kb_grep(
        patterns: list[str],
        max_hits: int = 20,
        regex: bool = False,
        directory_id: int = 0,
        recursive: bool = True,
    ) -> str:
        """在已解析文档、表格行和电路网表中做精确/正则检索，只返回候选指针。

        patterns 可一次传多个型号、位号、信号名或表号；命中只返回
        id/来源/位置/短摘要。需要正文时用 evidence_read 读取候选。
        """
        raw_patterns = patterns if isinstance(patterns, list) else [patterns]
        values = [str(item or "").strip() for item in raw_patterns if str(item or "").strip()]
        if not values:
            return "（未提供检索模式）"
        compiled: list[re.Pattern[str]] = []
        for value in values[:12]:
            try:
                compiled.append(re.compile(value if regex else re.escape(value), re.IGNORECASE))
            except re.error as exc:
                return f"（正则表达式无效：{value}：{exc}）"
        limit = max(1, min(int(max_hits or 20), 50))
        documents = _scoped_documents(rt, document_store)
        candidates: list[tuple[int, Evidence]] = []
        seen: set[str] = set()

        def _absorb(items: list[Evidence]) -> None:
            for item in items:
                if item.id in seen or not item.content:
                    continue
                hits = sum(len(pattern.findall(item.content)) for pattern in compiled)
                if hits <= 0:
                    continue
                seen.add(item.id)
                candidates.append((hits, item))

        def _direct_scan(records: list[Any]) -> list[Evidence]:
            found: list[Evidence] = []
            for record in records:
                if str(getattr(record, "processor_kind", "") or "") != "ragflow":
                    continue
                if str(getattr(record, "status", "") or "") not in {"parsed", "indexed"}:
                    continue
                found.extend(
                    _document_chunks(record, int(getattr(record, "kb_id", 0) or 0))
                )
            found.extend(
                item
                for _, item in _xlsx_candidates(
                    rt, records, spreadsheet_service, compiled,
                    regex=regex, limit=limit, seen=set(),
                )
            )
            found.extend(
                item
                for _, item in _circuit_candidates(
                    rt, records, circuit_service, compiled,
                    limit=limit, seen=set(),
                )
            )
            return found

        groups: dict[tuple[str, str], list[Any]] = {}
        for record in documents:
            groups.setdefault(
                (
                    str(getattr(record, "department_id", "") or ""),
                    str(getattr(record, "kb_name", "") or ""),
                ),
                [],
            ).append(record)
        for (department_id, kb_name), records in groups.items():
            try:
                index = PointerIndex(pointer_index_path(department_id, kb_name))
                signature = records_signature(records)
                if index.needs_rebuild(signature):
                    items: list[Evidence] = []
                    for record in records:
                        if (
                            str(getattr(record, "processor_kind", "") or "") == "ragflow"
                            and str(getattr(record, "status", "") or "") in {"parsed", "indexed"}
                        ):
                            items.extend(
                                _document_chunks(record, int(getattr(record, "kb_id", 0) or 0))
                            )
                    items.extend(
                        item
                        for _, item in _xlsx_candidates(
                            rt, records, spreadsheet_service, [re.compile(".")],
                            regex=True, limit=5000, seen=set(),
                        )
                    )
                    items.extend(_all_circuit_items(records, circuit_service, document_store))
                    index.build(
                        list({item.id: item for item in items}.values()), signature
                    )
                _absorb(index.search(values, regex=regex, limit=limit))
            except Exception:
                _absorb(_direct_scan(records))

        if not candidates:
            _absorb(_direct_scan(documents))

        candidates.sort(
            key=lambda pair: (
                -pair[0],
                pair[1].source_name,
                str(pair[1].locator),
            )
        )
        # Keep one pointer per format near the front so a docx-heavy hit list
        # cannot hide the only xlsx/EDF pointer of a multi-format query.
        selected_pairs: list[tuple[int, Evidence]] = []
        deferred: list[tuple[int, Evidence]] = []
        seen_kinds: set[str] = set()
        for pair in candidates:
            kind = str(pair[1].processor_kind or "")
            if kind and kind not in seen_kinds:
                seen_kinds.add(kind)
                selected_pairs.append(pair)
            else:
                deferred.append(pair)
        selected_pairs.extend(deferred)
        selected = [item for _, item in selected_pairs[:limit]]
        allowed_directory_ids = _allowed_directory_ids(directory_id, recursive)
        if allowed_directory_ids is not None:
            selected = [
                item
                for item in selected
                if int((item.metadata or {}).get("directory_id", 0) or 0)
                in allowed_directory_ids
            ]
        query = " | ".join(values)
        items, _ = timed_tool_call(
            rt,
            "kb_grep",
            query,
            None,
            lambda: selected,
            candidate_only=True,
        )
        return format_tool_result(rt, False, items)

    return kb_grep


__all__ = ["make_kb_grep"]
