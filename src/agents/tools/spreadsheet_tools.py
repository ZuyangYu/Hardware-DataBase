"""Spreadsheet (xlsx structured index) retrieval tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from typing import Any

from src.agents.schemas import Evidence
from src.agents.scopes import KnowledgeBaseSpreadsheetScopeResolver
from src.core.query_tokens import tokenize_hardware_query
from src.pipelines.document_rag.schemas import RequestContext
from src.services.kb_scope import kb_scope_from_context
from src.services.spreadsheet_index_service import SpreadsheetIndexService




_DOMAIN_QUERY_TERMS = (
    "供电",
    "电源",
    "网络",
    "电压",
    "电流",
    "功耗",
    "频率",
    "地址",
    "容量",
    "数量",
    "用量",
    "接口",
    "引脚",
    "连接",
    "使能",
    "唤醒",
    "看门狗",
    "复位",
    "阈值",
    "滤波",
    "截止频率",
    "模块",
    "芯片",
    "型号",
)
_CJK_STOP_CHARS = frozenset("的是什么有哪些列出如何这该项目前中与和及到从为将或能否可以是否请问多少几片了用名")
_CJK_NOISY_NGRAMS = frozenset({"电电", "围使", "存容", "源网", "络名"})
_BOILERPLATE_MARKERS = ("填写说明", "template instructions", "封面", "cover", "模板变更历史")


def _find_pin_column(connection, record_id: int, sheet_name: str, pin_field: str) -> str | None:
    """Resolve the pin-number column from the sheet's own header row."""
    import json

    wanted = {str(pin_field or "").strip().casefold(), "管脚号", "pin number", "pin no.", "pin", "引脚号"}
    wanted.discard("")
    try:
        rows = connection.execute(
            "SELECT values_json FROM table_semantic_rows WHERE record_id = ? AND sheet_name = ? ORDER BY row_index",
            (record_id, sheet_name),
        ).fetchall()
    except Exception:
        return None
    for row in rows:
        try:
            values = json.loads(row["values_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for key, value in (values or {}).items():
            if str(value).strip().casefold() in wanted:
                return str(key)
    return None


def _row_matches_pin(row, pin_value: str, pin_field: str = "", pin_key: str | None = None) -> bool:
    import json
    import re

    try:
        values = json.loads(row["values_json"] or "{}")
    except (TypeError, ValueError):
        values = {}
    if pin_key and pin_key in values:
        return str(values[pin_key]).strip() == str(pin_value).strip()
    field = pin_field.casefold()
    key_found = False
    for key, value in (values or {}).items():
        key_text = str(key).casefold()
        key_matches = bool(field and field in key_text) or "pin number" in key_text or key_text in {"pin", "pin no.", "管脚号", "引脚号"}
        if not key_matches:
            continue
        key_found = True
        if str(value).strip() == str(pin_value).strip():
            return True
    if key_found:
        return False
    text = f"{row['semantic_text'] or ''}\n{row['raw_text'] or ''}"
    patterns = (
        rf"(?:第\s*{re.escape(str(pin_value))}\s*脚|管脚(?:号)?\s*[:：]?\s*{re.escape(str(pin_value))}(?!\d)|"
        rf"pin\s*(?:number|no\.?)?\s*[:：#]?\s*{re.escape(str(pin_value))}(?!\d))"
    )
    return bool(re.search(patterns, text, re.IGNORECASE))


def _query_relevance_score(text: str, tokens: list[str], confidence: float = 0.0) -> float:
    """Bounded query-match score; parser confidence only breaks ties.

    The adapters sort candidates by ``Evidence.score``; assigning the parser's
    confidence there made an unrelated high-confidence row outrank a row that
    actually matched the question. Keep the two signals separate.
    """
    matched = {token for token in tokens if token in str(text or "").casefold()}
    if not matched:
        return float(confidence or 0.0) / 1_000_000
    return len(matched) + sum(len(token) for token in matched) / 1000.0 + float(confidence or 0.0) / 1_000_000



def _knowledge_base_scope(
    service: SpreadsheetIndexService,
    kb_name: str,
    ctx: RequestContext | None,
    action: str,
):
    """Resolve the KB namespace once before any spreadsheet query.

    ``kb_scope_from_context`` retains the existing request-context/department
    guard; the dedicated resolver then produces the typed scope consumed by
    every spreadsheet entry point.  This keeps KB and attachment tools on the
    same domain service while making it impossible for a caller to substitute
    an unscoped database path.
    """
    context_scope = kb_scope_from_context(kb_name, ctx).require_department(action)
    return KnowledgeBaseSpreadsheetScopeResolver(
        lambda department_id, name: service.db_path(
            department_id, name, create=False
        )
    ).resolve(
        kb_name=context_scope.kb_name,
        department_id=context_scope.department_id,
    )


def _tokens(query: str) -> list[str]:
    """Extract searchable hardware terms from mixed Chinese/English queries."""

    value = str(query or "")
    lowered = value.casefold()
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        normalized = token.strip().casefold()
        if len(normalized) < 2 or normalized in seen:
            return
        seen.add(normalized)
        tokens.append(normalized)

    # Preserve exact part numbers, references, buses, and English hardware terms.
    for token in tokenize_hardware_query(value, max_tokens=32, include_cjk_ngrams=False):
        add(token)
    for term in _DOMAIN_QUERY_TERMS:
        if term.casefold() in lowered:
            add(term)

    # Add meaningful CJK bigrams while excluding interrogative and
    # grammatical fragments such as ``的供`` or ``电电``. Exact domain terms
    # above cover the longer concepts we want to preserve (for example
    # ``截止频率`` and ``看门狗``), while bigrams avoid noisy fragments such as
    # ``外围使`` and ``围使``.
    for block in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        for size in (2,):
            for index in range(0, len(block) - size + 1):
                token = block[index : index + size]
                if (
                    len(set(token)) > 1
                    and token not in _CJK_NOISY_NGRAMS
                    and not any(char in _CJK_STOP_CHARS for char in token)
                ):
                    add(token)
    return tokens[:24]


def _candidate_limit(top_k: int) -> int:
    requested = max(1, int(top_k))
    return min(200, max(40, requested * 8))


def _row_relevance(text: str, tokens: list[str], confidence: float = 0.0, row_id: int = 0) -> tuple:
    searchable = str(text or "").casefold()
    matched = {token for token in tokens if token in searchable}
    boilerplate = any(marker in searchable for marker in _BOILERPLATE_MARKERS)
    return (
        len(matched),
        sum(len(token) for token in matched),
        not boilerplate,
        float(confidence or 0.0),
        -int(row_id or 0),
    )


def _rank_rows(rows, tokens: list[str], text_getter):
    return sorted(
        rows,
        key=lambda row: _row_relevance(
            text_getter(row),
            tokens,
            float(row["confidence_score"] or 0.0) if "confidence_score" in row.keys() else 0.0,
            int(row["id"] or 0) if "id" in row.keys() else 0,
        ),
        reverse=True,
    )

def _escape_like(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_clauses(columns: list[str], tokens: list[str]) -> tuple[str, list[str]]:
    clauses = []
    params: list[str] = []
    for token in tokens:
        escaped = _escape_like(str(token))
        token_clauses = []
        for column in columns:
            token_clauses.append(f"LOWER({column}) LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        clauses.append("(" + " OR ".join(token_clauses) + ")")
    if not clauses:
        return "1=1", []
    return "(" + " OR ".join(clauses) + ")", params


def _authoritative_metadata(rt, document_store, row, locator: dict, content: str, base: dict) -> dict:
    """Bind a legacy spreadsheet row to its authoritative document record."""
    metadata = dict(base)
    record = None
    if document_store is not None:
        ctx_metadata = getattr(rt.ctx, "metadata", {}) or {}
        department_id = str(ctx_metadata.get("resource_department_id") or ctx_metadata.get("department_id") or "")
        try:
            record = document_store.get_document_by_id_scoped(int(row["record_id"]), department_id)
        except Exception:
            record = None
    if record is None:
        return metadata
    if record.kb_name != rt.kb_name or record.processor_kind != "spreadsheet_table":
        return metadata
    index_hash = row["index_content_hash"] if "index_content_hash" in row.keys() else ""
    if index_hash and index_hash != record.content_hash:
        return metadata
    from hashlib import sha256

    metadata.update({
        "tenant_key": str(getattr(rt.ctx, "tenant_id", None) or "default"),
        "kb_id": record.kb_id if record.kb_id not in (0, None) else int(row["index_kb_id"] or 0),
        "kb_name": record.kb_name,
        "department_id": str(record.department_id),
        "local_record_id": int(record.id),
        "source_content_hash": str(record.content_hash or ""),
        "remote_document_id": str(record.document_id or ""),
        "source_version_id": record.source_version_id,
        "revision": record.revision,
        "approval_status": record.approval_status,
        "project_id": record.project_id,
        "processor_kind": "spreadsheet_table",
        "chunk_or_structured_locator": dict(locator),
        "locator": dict(locator),
        "index_revision": int(getattr(record, "index_revision", 0) or 0),
        "processing_artifact_id": str(getattr(record, "processing_artifact_id", "") or ""),
        "processor_version": str(getattr(record, "processor_version", "") or ""),
        "evidence_text_hash": sha256(content.encode("utf-8")).hexdigest(),
    })
    try:
        from src.services.evidence_fusion import evidence_identity

        metadata["authoritative_evidence_identity"] = evidence_identity(metadata)
    except Exception:
        pass
    return metadata


def _target_record_ids(rt, target: str | None = None) -> list[int]:
    targets = getattr(getattr(rt, "turn_retrieval_targets", None), "targets", ()) or ()
    if target is not None:
        targets = tuple(item for item in targets if item.target_id == target)
    return sorted({int(item.record_id) for item in targets})


def _semantic_rows(rt, service: SpreadsheetIndexService, query: str, top_k: int,
                   target: str | None = None) -> list[Evidence]:
    scope = _knowledge_base_scope(
        service, rt.kb_name, rt.ctx, "search spreadsheet semantic rows in"
    )
    db_path = scope.db_path
    if not os.path.exists(db_path):
        return []
    tokens = _tokens(query)
    where, params = _like_clauses(["r.semantic_text", "r.raw_text"], tokens)
    target_ids = _target_record_ids(rt, target)
    if target_ids:
        where = f"({where}) AND r.record_id IN ({','.join('?' for _ in target_ids)})"
        params.extend(target_ids)
    params.append(max(1, int(top_k)))
    sql = f"""
        SELECT r.*, d.document_name, d.source_group,
               d.content_hash AS index_content_hash, d.kb_id AS index_kb_id
        FROM table_semantic_rows r
        JOIN table_documents d ON d.record_id = r.record_id
        WHERE {where}
        ORDER BY r.confidence_score DESC, r.id ASC
        LIMIT ?
    """
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
    except Exception:
        return []
    with closing(conn):
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
    evidences = []
    for rank, row in enumerate(rows, 1):
        values = json.loads(row["values_json"] or "{}")
        raw_values = json.loads(row["raw_values_json"] or "{}")
        content = row["semantic_text"] or row["raw_text"]
        locator = {
            "record_id": row["record_id"],
            "sheet_name": row["sheet_name"],
            "row_index": row["row_index"],
            "header_row_index": row["header_row_index"],
            "cell_ref": row.get("cell_ref") if "cell_ref" in row.keys() else None,
        }
        metadata = _authoritative_metadata(rt, getattr(rt, "document_store", None), row, locator, content, {
            "tool": "spreadsheet_row_search",
            "query": query,
            "raw_text": row["raw_text"],
            "values": values,
            "raw_values": raw_values,
            "confidence": row["confidence"],
            "parser_confidence": float(row["confidence_score"] or 0.0),
            "retriever_rank": rank,
            "source_group": row["source_group"],
        })
        if getattr(rt, "document_store", None) is not None and "local_record_id" not in metadata:
            continue
        relevance = _query_relevance_score(content, tokens, float(row["confidence_score"] or 0.0))
        metadata["query_relevance"] = relevance
        evidences.append(
            Evidence(
                id=f"xlsx:{row['record_id']}:{row['sheet_name']}:{row['row_index']}:semantic",
                content=content,
                source_name=row["document_name"],
                content_kind="spreadsheet_table",
                processor_kind="spreadsheet_table",
                score=relevance,
                locator=locator,
                metadata=metadata,
            )
        )
    return evidences


def _cell_rows(rt, service: SpreadsheetIndexService, query: str, top_k: int,
               target: str | None = None) -> list[Evidence]:
    scope = _knowledge_base_scope(
        service, rt.kb_name, rt.ctx, "search spreadsheet cells in"
    )
    db_path = scope.db_path
    if not os.path.exists(db_path):
        return []
    tokens = _tokens(query)
    where, params = _like_clauses(["c.value", "c.raw_value", "c.header"], tokens)
    target_ids = _target_record_ids(rt, target)
    if target_ids:
        where = f"({where}) AND c.record_id IN ({','.join('?' for _ in target_ids)})"
        params.extend(target_ids)
    params.append(max(1, int(top_k)))
    sql = f"""
        SELECT c.*, d.document_name, d.source_group,
               d.content_hash AS index_content_hash, d.kb_id AS index_kb_id
        FROM table_cells c
        JOIN table_documents d ON d.record_id = c.record_id
        WHERE {where}
        ORDER BY c.id ASC
        LIMIT ?
    """
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
    except Exception:
        return []
    with closing(conn):
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
    evidences = []
    for rank, row in enumerate(rows, 1):
        content = f"{row['header'] or 'Cell'}: {row['value']}"
        locator = {
            "record_id": row["record_id"],
            "sheet_name": row["sheet_name"],
            "row_index": row["row_index"],
            "col_index": row["col_index"],
            "cell_ref": row["cell_ref"],
        }
        metadata = _authoritative_metadata(rt, getattr(rt, "document_store", None), row, locator, content, {
            "tool": "spreadsheet_cell_lookup",
            "query": query,
            "header": row["header"],
            "raw_value": row["raw_value"],
            "number_format": row["number_format"],
            "retriever_rank": rank,
            "source_group": row["source_group"],
        })
        if getattr(rt, "document_store", None) is not None and "local_record_id" not in metadata:
            continue
        relevance = _query_relevance_score(
            f"{row['header'] or ''}\n{row['value'] or ''}\n{row['raw_value'] or ''}", tokens,
        )
        metadata["query_relevance"] = relevance
        evidences.append(
            Evidence(
                id=f"xlsx:{row['record_id']}:{row['sheet_name']}:{row['cell_ref']}:cell",
                content=content,
                source_name=row["document_name"],
                content_kind="spreadsheet_table",
                processor_kind="spreadsheet_table",
                score=relevance,
                locator=locator,
                metadata=metadata,
            )
        )
    return evidences


def make_spreadsheet_tools(rt, spreadsheet_service: SpreadsheetIndexService):
    """Return the two spreadsheet tool closures bound to this run."""
    def spreadsheet_row_search(query: str, top_k: int = rt.top_k, target: str | None = None) -> str:
        """按语义检索 Excel 表格行, 只适合取"具体某行/某个值"这类单点信息。
        注意: 统计数量、求和、最值、均值、按条件筛选多行、数值比较等问题用本工具会漏行漏列,
        必须改用 spreadsheet_schema_lookup + spreadsheet_sql_query 一次完成。"""
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        items, adds_nothing = timed_tool_call(
            rt,
            "spreadsheet_row_search",
            query,
            None,
            lambda: _semantic_rows(rt, spreadsheet_service, query, max(1, min(int(top_k), 20)), target),
        )
        return format_tool_result(rt, adds_nothing, items)

    def spreadsheet_cell_lookup(query: str, top_k: int = rt.top_k, target: str | None = None) -> str:
        """按精确值检索单元格（表头/原始值匹配），适合查找具体参数值、型号、数量等。"""
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        items, adds_nothing = timed_tool_call(
            rt,
            "spreadsheet_cell_lookup",
            query,
            None,
            lambda: _cell_rows(rt, spreadsheet_service, query, max(1, min(int(top_k), 20)), target),
        )
        return format_tool_result(rt, adds_nothing, items)

    return spreadsheet_row_search, spreadsheet_cell_lookup

# ---- 直接调用兼容层（AppPipeline 项目检索等非 Agent 入口复用类接口） ----





def _candidate_limit(top_k: int) -> int:
    requested = max(1, int(top_k))
    return min(200, max(40, requested * 8))


def _row_relevance(text: str, tokens: list[str], confidence: float = 0.0, row_id: int = 0) -> tuple:
    searchable = str(text or "").casefold()
    matched = {token for token in tokens if token in searchable}
    boilerplate = any(marker in searchable for marker in _BOILERPLATE_MARKERS)
    return (
        len(matched),
        sum(len(token) for token in matched),
        not boilerplate,
        float(confidence or 0.0),
        -int(row_id or 0),
    )


def _rank_rows(rows, tokens: list[str], text_getter):
    return sorted(
        rows,
        key=lambda row: _row_relevance(
            text_getter(row),
            tokens,
            float(row["confidence_score"] or 0.0) if "confidence_score" in row.keys() else 0.0,
            int(row["id"] or 0) if "id" in row.keys() else 0,
        ),
        reverse=True,
    )


def _like_clauses(columns: list[str], tokens: list[str]) -> tuple[str, list[str]]:
    clauses = []
    params: list[str] = []
    for token in tokens:
        escaped = _escape_like(str(token))
        token_clauses = []
        for column in columns:
            token_clauses.append(f"LOWER({column}) LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        clauses.append("(" + " OR ".join(token_clauses) + ")")
    if not clauses:
        return "1=1", []
    return "(" + " OR ".join(clauses) + ")", params


def _token_match_order(columns: list[str], tokens: list[str]) -> tuple[str, list[str]]:
    """Build a portable SQL relevance expression for the candidate window."""

    expressions: list[str] = []
    params: list[str] = []
    for token in tokens:
        escaped = _escape_like(str(token))
        expressions.append(
            "(CASE WHEN "
            + " OR ".join(f"LOWER({column}) LIKE ? ESCAPE '\\'" for column in columns)
            + " THEN 1 ELSE 0 END)"
        )
        params.extend(f"%{escaped}%" for _ in columns)
    return " + ".join(expressions) or "0", params


class SpreadsheetSemanticTool:
    name = "spreadsheet_semantic"
    description = "Search semantic Excel rows by row-level facts such as BOM quantities, substitutes, parameters, or test rows."

    def __init__(self, spreadsheet_service: SpreadsheetIndexService):
        self.spreadsheet_service = spreadsheet_service

    def run(
        self,
        query: str,
        kb_name: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[Evidence]:
        scope = _knowledge_base_scope(
            self.spreadsheet_service,
            kb_name,
            ctx,
            "search spreadsheet semantic rows in",
        )
        db_path = scope.db_path
        if not os.path.exists(db_path):
            return []
        record_id = int((filters or {}).get("record_id") or 0)
        sheet_name = str((filters or {}).get("sheet_name") or "").strip()
        row_index = (filters or {}).get("row_index")
        pin_value = str((filters or {}).get("pin_value") or "").strip()
        pin_field = str((filters or {}).get("pin_field") or "").strip()
        tokens = _tokens(query)
        columns = ["r.semantic_text", "r.raw_text"]
        where, where_params = _like_clauses(columns, tokens)
        relevance_order, order_params = _token_match_order(columns, tokens)
        # A resolved locator is the query constraint; do not additionally
        # require the free-text question tokens to appear in the target row.
        if pin_value or (isinstance(row_index, int) and not isinstance(row_index, bool) and sheet_name):
            where, where_params = "1=1", []
        # SQLite binds placeholders in SQL-text order: WHERE, record filter,
        # locator filters, then ORDER BY relevance.
        params = [*where_params]
        requested_top_k = max(1, int(top_k))
        record_clause = " AND r.record_id = ?" if record_id else ""
        if record_id:
            params.append(record_id)
        locator_clauses = ""
        if sheet_name:
            locator_clauses += " AND r.sheet_name = ?"
            params.append(sheet_name)
        if isinstance(row_index, int) and not isinstance(row_index, bool):
            locator_clauses += " AND r.row_index = ?"
            params.append(row_index)
        if pin_value:
            # Pin matching depends on the header/value mapping. Narrow by the
            # sheet first and resolve the exact column in Python instead of a
            # noisy LIKE '%2%' over every numeric value.
            pass
        params.extend(order_params)
        params.append(_candidate_limit(requested_top_k))
        sql = f"""
            SELECT r.*, d.document_name, d.source_group
            FROM table_semantic_rows r
            JOIN table_documents d ON d.record_id = r.record_id
            WHERE {where}{record_clause}{locator_clauses}
            ORDER BY {relevance_order} DESC, r.confidence_score DESC, r.id ASC
            LIMIT ?
        """
        locator_match = bool(
            pin_value
            or (
                sheet_name
                and isinstance(row_index, int)
                and not isinstance(row_index, bool)
            )
        )
        return self._query_rows(db_path, sql, params, query, tokens, requested_top_k,
                                pin_value=pin_value, pin_field=pin_field, sheet_name=sheet_name,
                                record_id=record_id, locator_match=locator_match)

    def _query_rows(
        self,
        db_path: str,
        sql: str,
        params: list,
        query: str,
        tokens: list[str],
        top_k: int,
        *,
        pin_value: str = "",
        pin_field: str = "",
        sheet_name: str = "",
        record_id: int = 0,
        locator_match: bool = False,
    ) -> list[Evidence]:
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            conn.row_factory = sqlite3.Row
        except Exception:
            return []
        with closing(conn):
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                return []
            pin_key = None
            if pin_value and sheet_name and record_id:
                pin_key = _find_pin_column(conn, record_id, sheet_name, pin_field)
        if pin_value:
            rows = [row for row in rows if _row_matches_pin(row, pin_value, pin_field, pin_key)]
        rows = _rank_rows(
            rows,
            tokens,
            lambda row: f"{row['semantic_text'] or ''}\n{row['raw_text'] or ''}",
        )
        return self._build_evidence(rows, query, tokens, top_k, locator_match=locator_match)

    def _build_evidence(self, rows, query: str, tokens: list[str], top_k: int,
                        *, locator_match: bool = False) -> list[Evidence]:
        evidences = []
        for rank, row in enumerate(rows, 1):
            values = json.loads(row["values_json"] or "{}")
            raw_values = json.loads(row["raw_values_json"] or "{}")
            parser_confidence = float(row["confidence_score"] or 0.0)
            relevance = _query_relevance_score(
                f"{row['semantic_text'] or ''}\n{row['raw_text'] or ''}", tokens, parser_confidence,
            )
            score = 1.0 + relevance if locator_match else relevance
            evidences.append(
                Evidence(
                    id=f"xlsx:{row['record_id']}:{row['sheet_name']}:{row['row_index']}:semantic",
                    content=row["semantic_text"] or row["raw_text"],
                    source_name=row["document_name"],
                    content_kind="spreadsheet_table",
                    processor_kind="spreadsheet_table",
                    score=score,
                    locator={
                        "record_id": row["record_id"],
                        "sheet_name": row["sheet_name"],
                        "row_index": row["row_index"],
                        "header_row_index": row["header_row_index"],
                    },
                    metadata={
                        "tool": self.name,
                        "query": query,
                        "raw_text": row["raw_text"],
                        "values": values,
                        "raw_values": raw_values,
                        "confidence": row["confidence"],
                        "parser_confidence": parser_confidence,
                        "query_relevance": relevance,
                        "locator_match": locator_match,
                        "retriever_rank": rank,
                        "source_group": row["source_group"],
                    },
                )
            )
        return evidences[:top_k]

    def run_batch(
        self, query: str, kb_name: str, ctx, record_ids: list[int],
        locators_by_record: dict[int, tuple], top_k: int = 5,
    ) -> list[Evidence]:
        """One parameterized candidate query for every allowed record."""
        ids = sorted({int(record_id) for record_id in record_ids if int(record_id) > 0})
        if not ids:
            return []
        scope = _knowledge_base_scope(self.spreadsheet_service, kb_name, ctx,
                                      "search spreadsheet semantic rows in")
        db_path = scope.db_path
        if not os.path.exists(db_path):
            return []
        tokens = _tokens(query)
        columns = ["r.semantic_text", "r.raw_text"]
        where, where_params = _like_clauses(columns, tokens)
        relevance_order, order_params = _token_match_order(columns, tokens)
        locators = [locator for record_id in ids for locator in (locators_by_record.get(record_id) or ())]
        if any(getattr(locator, "pin_value", None) for locator in locators):
            where, where_params = "1=1", []
        params = [*where_params]
        branches = []
        for record_id in ids:
            record_locators = tuple(locators_by_record.get(record_id) or ())
            if not record_locators:
                branches.append("r.record_id = ?")
                params.append(record_id)
                continue
            for locator in record_locators:
                branch = ["r.record_id = ?"]
                branch_params = [record_id]
                if getattr(locator, "sheet_name", None):
                    branch.append("r.sheet_name = ?")
                    branch_params.append(locator.sheet_name)
                if isinstance(getattr(locator, "row_index", None), int):
                    branch.append("r.row_index = ?")
                    branch_params.append(locator.row_index)
                if getattr(locator, "pin_value", None):
                    branch.append("(LOWER(r.semantic_text) LIKE ? OR LOWER(r.raw_text) LIKE ? OR LOWER(r.values_json) LIKE ?)")
                    pattern = f"%{locator.pin_value}%"
                    branch_params.extend([pattern, pattern, pattern])
                branches.append("(" + " AND ".join(branch) + ")")
                params.extend(branch_params)
        params.extend(order_params)
        params.append(_candidate_limit(top_k))
        sql = f"""
            SELECT r.*, d.document_name, d.source_group
            FROM table_semantic_rows r
            JOIN table_documents d ON d.record_id = r.record_id
            WHERE {where} AND ({" OR ".join(branches)})
            ORDER BY {relevance_order} DESC, r.confidence_score DESC, r.id ASC
            LIMIT ?
        """
        try:
            with closing(sqlite3.connect(db_path, timeout=30)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(sql, params).fetchall()
                pin_keys = {}
                for record_id in ids:
                    for locator in (locators_by_record.get(record_id) or ()):
                        if getattr(locator, "pin_value", None) and getattr(locator, "sheet_name", None):
                            pin_keys[(int(record_id), str(locator.sheet_name))] = _find_pin_column(
                                connection, int(record_id), str(locator.sheet_name),
                                str(getattr(locator, "pin_field", "") or ""),
                            )
        except sqlite3.Error:
            return []
        pin_by_record: dict[int, set[str]] = {}
        for record_id in ids:
            for locator in (locators_by_record.get(record_id) or ()):
                if getattr(locator, "pin_value", None):
                    pin_by_record.setdefault(int(record_id), set()).add(str(locator.pin_value))
        if pin_by_record:
            rows = [row for row in rows if any(
                _row_matches_pin(row, value, "", pin_keys.get((int(row["record_id"]), str(row["sheet_name"]))))
                for value in pin_by_record.get(int(row["record_id"]), set())
            )]
        rows = _rank_rows(rows, tokens, lambda row: f"{row['semantic_text'] or ''}\n{row['raw_text'] or ''}")
        return self._build_evidence(rows, query, tokens, top_k, locator_match=bool(pin_by_record))



class SpreadsheetCellTool:
    name = "spreadsheet_cell"
    description = "Search exact-ish Excel cells by value/header/raw value."

    def __init__(self, spreadsheet_service: SpreadsheetIndexService):
        self.spreadsheet_service = spreadsheet_service

    def run(
        self,
        query: str,
        kb_name: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[Evidence]:
        scope = _knowledge_base_scope(
            self.spreadsheet_service,
            kb_name,
            ctx,
            "search spreadsheet cells in",
        )
        db_path = scope.db_path
        if not os.path.exists(db_path):
            return []
        record_id = int((filters or {}).get("record_id") or 0)
        sheet_name = str((filters or {}).get("sheet_name") or "").strip()
        row_index = (filters or {}).get("row_index")
        cell_ref = str((filters or {}).get("cell_ref") or "").strip().upper()
        pin_value = str((filters or {}).get("pin_value") or "").strip()
        pin_field = str((filters or {}).get("pin_field") or "").strip()
        tokens = _tokens(query)
        columns = ["c.value", "c.raw_value", "c.header"]
        where, where_params = _like_clauses(columns, tokens)
        relevance_order, order_params = _token_match_order(columns, tokens)
        if pin_value or cell_ref or (isinstance(row_index, int) and not isinstance(row_index, bool) and sheet_name):
            where, where_params = "1=1", []
        params = [*where_params]
        requested_top_k = max(1, int(top_k))
        record_clause = " AND c.record_id = ?" if record_id else ""
        if record_id:
            params.append(record_id)
        locator_clauses = ""
        if sheet_name:
            locator_clauses += " AND c.sheet_name = ?"
            params.append(sheet_name)
        if isinstance(row_index, int) and not isinstance(row_index, bool):
            locator_clauses += " AND c.row_index = ?"
            params.append(row_index)
        if cell_ref:
            locator_clauses += " AND UPPER(c.cell_ref) = ?"
            params.append(cell_ref)
        if pin_value:
            header_pattern = f"%{pin_field.casefold()}%" if pin_field else "%pin%"
            locator_clauses += " AND (LOWER(c.header) LIKE ? OR LOWER(c.header) LIKE '%脚%') AND c.raw_value = ?"
            params.extend([header_pattern, pin_value])
        params.extend(order_params)
        params.append(_candidate_limit(requested_top_k))
        sql = f"""
            SELECT c.*, d.document_name, d.source_group
            FROM table_cells c
            JOIN table_documents d ON d.record_id = c.record_id
            WHERE {where}{record_clause}{locator_clauses}
            ORDER BY {relevance_order} DESC, c.id ASC
            LIMIT ?
        """
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            conn.row_factory = sqlite3.Row
        except Exception:
            return []
        with closing(conn):
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                return []
        rows = _rank_rows(
            rows,
            tokens,
            lambda row: f"{row['header'] or ''}\n{row['value'] or ''}\n{row['raw_value'] or ''}",
        )
        return self._build_evidence(rows, query, tokens, requested_top_k,
                                    locator_match=bool(pin_value or cell_ref or isinstance(row_index, int)))

    def _build_evidence(self, rows, query: str, tokens: list[str], top_k: int,
                        *, locator_match: bool = False) -> list[Evidence]:
        evidences = []
        for rank, row in enumerate(rows, 1):
            parser_confidence = float(row["confidence_score"] or 0.0) if "confidence_score" in row.keys() else 0.0
            relevance = _query_relevance_score(
                f"{row['header'] or ''}\n{row['value'] or ''}\n{row['raw_value'] or ''}", tokens, parser_confidence,
            )
            evidences.append(
                Evidence(
                    id=f"xlsx:{row['record_id']}:{row['sheet_name']}:{row['cell_ref']}:cell",
                    content=f"{row['header'] or 'Cell'}: {row['value']}",
                    source_name=row["document_name"],
                    content_kind="spreadsheet_table",
                    processor_kind="spreadsheet_table",
                    score=1.0 + relevance if locator_match else relevance,
                    locator={
                        "record_id": row["record_id"],
                        "sheet_name": row["sheet_name"],
                        "row_index": row["row_index"],
                        "col_index": row["col_index"],
                        "cell_ref": row["cell_ref"],
                    },
                    metadata={
                        "tool": self.name,
                        "query": query,
                        "header": row["header"],
                        "raw_value": row["raw_value"],
                        "number_format": row["number_format"],
                        "parser_confidence": parser_confidence,
                        "query_relevance": relevance,
                        "retriever_rank": rank,
                        "source_group": row["source_group"],
                    },
                )
            )
        return evidences[:top_k]

    def run_batch(
        self, query: str, kb_name: str, ctx, record_ids: list[int],
        locators_by_record: dict[int, tuple], top_k: int = 5,
    ) -> list[Evidence]:
        """One parameterized cell query for every allowed record."""
        ids = sorted({int(record_id) for record_id in record_ids if int(record_id) > 0})
        if not ids:
            return []
        scope = _knowledge_base_scope(self.spreadsheet_service, kb_name, ctx,
                                      "search spreadsheet cells in")
        db_path = scope.db_path
        if not os.path.exists(db_path):
            return []
        tokens = _tokens(query)
        columns = ["c.value", "c.raw_value", "c.header"]
        where, where_params = _like_clauses(columns, tokens)
        relevance_order, order_params = _token_match_order(columns, tokens)
        locators = [locator for record_id in ids for locator in (locators_by_record.get(record_id) or ())]
        if any(getattr(locator, "pin_value", None) or getattr(locator, "cell_ref", None) for locator in locators):
            where, where_params = "1=1", []
        params = [*where_params]
        branches = []
        for record_id in ids:
            record_locators = tuple(locators_by_record.get(record_id) or ())
            if not record_locators:
                branches.append("c.record_id = ?")
                params.append(record_id)
                continue
            for locator in record_locators:
                branch = ["c.record_id = ?"]
                branch_params = [record_id]
                if getattr(locator, "sheet_name", None):
                    branch.append("c.sheet_name = ?")
                    branch_params.append(locator.sheet_name)
                if isinstance(getattr(locator, "row_index", None), int):
                    branch.append("c.row_index = ?")
                    branch_params.append(locator.row_index)
                if getattr(locator, "cell_ref", None):
                    branch.append("UPPER(c.cell_ref) = ?")
                    branch_params.append(locator.cell_ref.upper())
                if getattr(locator, "pin_value", None):
                    header_pattern = f"%{(getattr(locator, 'pin_field', '') or 'pin').casefold()}%"
                    branch.append("(LOWER(c.header) LIKE ? OR LOWER(c.header) LIKE '%脚%') AND c.raw_value = ?")
                    branch_params.extend([header_pattern, locator.pin_value])
                branches.append("(" + " AND ".join(branch) + ")")
                params.extend(branch_params)
        params.extend(order_params)
        params.append(_candidate_limit(top_k))
        sql = f"""
            SELECT c.*, d.document_name, d.source_group
            FROM table_cells c
            JOIN table_documents d ON d.record_id = c.record_id
            WHERE {where} AND ({" OR ".join(branches)})
            ORDER BY {relevance_order} DESC, c.id ASC
            LIMIT ?
        """
        try:
            with closing(sqlite3.connect(db_path, timeout=30)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        rows = _rank_rows(rows, tokens, lambda row: f"{row['header'] or ''}\n{row['value'] or ''}\n{row['raw_value'] or ''}")
        return self._build_evidence(rows, query, tokens, top_k)



# ---- SQL 只读查询工具(物化表 + sqlglot 校验, TableRAG 式混合检索的 SQL 路) ----

_SQL_MAX_ROWS = 200
_SQL_RESULT_CHAR_BUDGET = 2000


def _load_sql_registry(db_path: str) -> list[dict]:
    """读取全部 sql_table_registry 行(schema_json 已解码)."""
    if not os.path.exists(db_path):
        return []
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT r.record_id, r.table_name, r.sheet_name, r.column_count, r.row_count,
                       r.schema_json, d.document_name
                FROM sql_table_registry r
                JOIN table_documents d ON d.record_id = r.record_id
                ORDER BY d.document_name, r.sheet_name
                """
            ).fetchall()
        except sqlite3.Error:
            return []
    registry = []
    for row in rows:
        try:
            schema = json.loads(row["schema_json"] or "{}")
        except (TypeError, ValueError):
            schema = {}
        registry.append(
            {
                "record_id": int(row["record_id"]),
                "table_name": row["table_name"],
                "sheet_name": row["sheet_name"],
                "document_name": row["document_name"],
                "columns": schema.get("columns", []),
                "meta_columns": schema.get("meta_columns", []),
                "row_count": schema.get("row_count", row["row_count"]),
                "note": schema.get("note", ""),
            }
        )
    return registry


def _format_schema_entry(entry: dict) -> str:
    columns = "; ".join(
        f"{col['column']}={col['header']}({col['dtype']})"
        + (f" 样本:{col['samples'][:3]}" if col.get("samples") else "")
        for col in entry.get("columns", [])
    )
    meta = "; ".join(
        f"{col['column']}={col.get('header', '')}"
        for col in entry.get("meta_columns", [])
    )
    return (
        f"表 {entry['table_name']} | 文档 {entry['document_name']} | sheet「{entry['sheet_name']}」"
        f" | {entry.get('row_count', '?')} 行\n  列: {columns}"
        + (f"\n  附加列: {meta}" if meta else "")
        + (f"\n  说明: {entry['note']}" if entry.get("note") else "")
    )


def _validate_readonly_sql(sql_text: str, allowed_tables: set[str]) -> tuple[Any, str | None]:
    """sqlglot 静态校验: 单条 SELECT、表白名单、自动补/收 LIMIT.

    返回 (ast, 错误消息); 成功时错误消息为 None.
    """
    import sqlglot
    from sqlglot import exp

    try:
        statements = sqlglot.parse(sql_text, read="sqlite")
    except sqlglot.errors.ParseError as e:
        return None, f"SQL 语法错误: {e}"
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return None, "只允许一条 SELECT 语句"
    root = statements[0]
    if not isinstance(root, exp.Select):
        return None, f"只允许 SELECT 查询(检测到 {root.key.upper()})"
    cte_names = {cte.alias_or_name for cte in root.find_all(exp.CTE)}
    used_tables = {table.name for table in root.find_all(exp.Table)} - cte_names
    unknown = used_tables - allowed_tables
    if unknown:
        return None, (
            "引用了未登记的表: " + ", ".join(sorted(unknown))
            + f"(可用表: {', '.join(sorted(allowed_tables))})"
        )
    # LIMIT 收敛: 已有限值取 min(200), 否则补 200
    limit_node = root.args.get("limit")
    if limit_node is not None and limit_node.expression is not None:
        try:
            current = int(limit_node.expression.this)
        except (AttributeError, ValueError, TypeError):
            current = None
        if current is None or current > _SQL_MAX_ROWS:
            root = root.limit(_SQL_MAX_ROWS)
    else:
        root = root.limit(_SQL_MAX_ROWS)
    return root, None


def _execute_readonly_sql(db_path: str, sql_text: str) -> tuple[list[dict] | None, str | None]:
    """mode=ro + query_only 双保险执行; 返回 (行列表, 错误消息), 成功时错误消息为 None."""
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as e:
        return None, f"数据库连接失败: {e}"
    with closing(conn):
        try:
            conn.execute("PRAGMA query_only = ON")
            cursor = conn.execute(sql_text)
            columns = [desc[0] for desc in cursor.description or []]
            rows = cursor.fetchmany(_SQL_MAX_ROWS + 1)
        except sqlite3.Error as e:
            return None, f"SQL 执行失败: {e}"
    if len(rows) > _SQL_MAX_ROWS:
        rows = rows[:_SQL_MAX_ROWS]
    records = [dict(zip(columns, row)) for row in rows]
    return records, None


def _format_sql_result(records: list[dict]) -> str:
    if not records:
        return "SQL 执行成功, 但结果为空(0 行)。请检查筛选条件是否过严或值拼写是否与 schema 样本一致。"
    lines = []
    used = 0
    for index, record in enumerate(records, start=1):
        cells = ", ".join(f"{k}={v}" for k, v in record.items())
        line = f"行{index}: {cells}"
        if used + len(line) > _SQL_RESULT_CHAR_BUDGET and index > 1:
            lines.append(f"(结果已截断, 共 {len(records)} 行, 仅显示前 {index - 1} 行)")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def _sql_evidence_id(sql: str, kind: str) -> str:
    """Content-derived evidence id for SQL results.

    Two different SQL statements must never share an evidence id: the shared
    static ``xlsx-sql:result`` id made the second query's rows inherit the
    first query's citation number, so the answer's [n] markers pointed at the
    wrong evidence content.
    """
    digest = hashlib.sha1(str(sql or "").encode("utf-8")).hexdigest()[:12]
    return f"xlsx-sql:{kind}:{digest}"


def make_spreadsheet_sql_tools(rt, spreadsheet_service):
    """返回 SQL 查询两件套: 先查 schema 再执行只读 SQL."""

    def spreadsheet_schema_lookup(query: str = "", top_k: int = 8) -> str:
        """列出可执行 SQL 的物化表 schema(表名/中文表头/类型/样本值), 写 SQL 前必查。
        附加列: section 为章节标题(章节/层级问题用它过滤), row_index 为原表行号。"""
        from src.agents.tools.runtime import timed_tool_call

        scope = _knowledge_base_scope(
            spreadsheet_service,
            rt.kb_name,
            rt.ctx,
            "lookup spreadsheet sql schema in",
        )
        db_path = scope.db_path

        def _run() -> list[Evidence]:
            registry = _load_sql_registry(db_path)
            if not registry:
                return []
            tokens = _tokens(query) if query else []

            def searchable(entry: dict) -> str:
                headers = " ".join(col.get("header", "") for col in entry.get("columns", []))
                return f"{entry['document_name']} {entry['sheet_name']} {headers}".casefold()

            if tokens:
                scored = [
                    entry for entry in registry
                    if any(token in searchable(entry) for token in tokens)
                ]
                if not scored:
                    scored = registry
            else:
                scored = registry
            scored.sort(key=lambda entry: -sum(1 for token in tokens if token in searchable(entry)))
            selected = scored[: max(1, min(int(top_k), 20))]
            # 打包成 ≤1100 字符的分块: 单条证据渲染上限 1200 字符,
            # 超限会让排在后面的表对 agent 不可见, SQL 规划会漏表。
            chunks: list[list[str]] = [[]]
            size = 0
            for index, entry in enumerate(selected, start=1):
                block = f"【{index}】{_format_schema_entry(entry)}"
                if size + len(block) > 1100 and chunks[-1]:
                    chunks.append([])
                    size = 0
                chunks[-1].append(block)
                size += len(block) + 2
            source_names = ", ".join(
                dict.fromkeys(entry["document_name"] for entry in selected)
            )
            evidences: list[Evidence] = []
            for chunk_index, blocks in enumerate(chunks, start=1):
                header = (
                    f"可执行 SQL 的物化表(第 {chunk_index}/{len(chunks)} 部分, "
                    f"文档: {source_names}):"
                )
                evidences.append(
                    Evidence(
                        id=f"xlsx-schema:summary:{chunk_index}",
                        content=f"{header}\n\n" + "\n\n".join(blocks),
                        source_name=source_names,
                        content_kind="spreadsheet_schema",
                        processor_kind="spreadsheet_table",
                        score=1.0,
                        locator={"part": f"{chunk_index}/{len(chunks)}"},
                        metadata={"tool": "spreadsheet_schema_lookup", "query": query,
                                  "evidence_role": "navigation"},
                    )
                )
            return evidences

        items, adds_nothing = timed_tool_call(rt, "spreadsheet_schema_lookup", query, None, _run)
        if not items:
            from src.agents.tools.runtime import empty_result_message

            return empty_result_message(
                rt,
                "当前知识库没有可用的 SQL 物化表。",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
        from src.agents.tools.runtime import format_tool_result

        return format_tool_result(rt, adds_nothing, items)

    def spreadsheet_sql_query(sql: str) -> str:
        """执行只读 SQL 查询物化表(仅 SELECT, 自动 LIMIT)。筛选/聚合/比较/统计类问题优先用本工具。

        出错时按错误信息修正 SQL 后重试, 最多 2 次; 表名与列结构先用 spreadsheet_schema_lookup 查询。
        按章节/层级编号(如 3.1.2)定位行时用附加列 section(章节标题): WHERE section LIKE '%3.1.2%',
        不要只查普通列(col_N)——章节号常常不在数据列里。
        """
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        scope = _knowledge_base_scope(
            spreadsheet_service,
            rt.kb_name,
            rt.ctx,
            "query spreadsheet sql in",
        )
        db_path = scope.db_path

        def _run() -> list[Evidence]:
            registry = _load_sql_registry(db_path)
            if not registry:
                return []
            allowed = {entry["table_name"]: entry for entry in registry}
            ast, error = _validate_readonly_sql(str(sql or ""), set(allowed))
            if error:
                return [
                    Evidence(
                        id=_sql_evidence_id(sql, "invalid"),
                        content=f"SQL 校验未通过: {error}\n请用 spreadsheet_schema_lookup 确认表名/列名后修正重试(最多 2 次)。",
                        source_name="spreadsheet_sql",
                        content_kind="spreadsheet_sql_result",
                        processor_kind="spreadsheet_table",
                        score=0.0,
                        locator={},
                        metadata={
                            "tool": "spreadsheet_sql_query",
                            "sql": sql,
                            "status": "invalid",
                            "evidence_role": "navigation",
                            "progress_neutral": True,
                        },
                    )
                ]
            table_names = sorted({table.name for table in ast.find_all(__import__("sqlglot").exp.Table)})
            source_names = list(dict.fromkeys(
                allowed[name]["document_name"] for name in table_names if name in allowed
            ))
            records, exec_error = _execute_readonly_sql(db_path, ast.sql(dialect="sqlite"))
            if exec_error:
                return [
                    Evidence(
                        id=_sql_evidence_id(sql, "error"),
                        content=(
                            f"{exec_error}\n请根据 schema(可用 spreadsheet_schema_lookup 查看)修正 SQL 后重试, 最多 2 次。"
                        ),
                        source_name=source_names[0] if source_names else "spreadsheet_sql",
                        content_kind="spreadsheet_sql_result",
                        processor_kind="spreadsheet_table",
                        score=0.0,
                        locator={"tables": ", ".join(table_names)},
                        metadata={
                            "tool": "spreadsheet_sql_query",
                            "sql": sql,
                            "status": "error",
                            "evidence_role": "navigation",
                            "progress_neutral": True,
                        },
                    )
                ]
            if not records:
                # A successful zero-row query is navigation, not evidence.  In
                # particular it must not be stashed as a candidate and promoted
                # by evidence_read: that would count as progress forever and
                # let a model explore 40+ empty queries until recursion_limit.
                return [
                    Evidence(
                        id=_sql_evidence_id(sql, "empty"),
                        content=_format_sql_result(records),
                        source_name=source_names[0] if source_names else "spreadsheet_sql",
                        content_kind="spreadsheet_sql_result",
                        processor_kind="spreadsheet_table",
                        score=0.0,
                        locator={"tables": ", ".join(table_names)},
                        metadata={
                            "tool": "spreadsheet_sql_query",
                            "sql": sql,
                            "row_count": 0,
                            "evidence_role": "navigation",
                            "progress_neutral": True,
                        },
                    )
                ]
            return [
                Evidence(
                    id=_sql_evidence_id(sql, "result"),
                    content=_format_sql_result(records),
                    source_name=source_names[0] if source_names else "spreadsheet_sql",
                    content_kind="spreadsheet_sql_result",
                    processor_kind="spreadsheet_table",
                    score=1.0,
                    locator={"tables": ", ".join(table_names)},
                    metadata={"tool": "spreadsheet_sql_query", "sql": sql, "row_count": len(records)},
                )
            ]

        items, adds_nothing = timed_tool_call(rt, "spreadsheet_sql_query", sql, None, _run)
        return format_tool_result(rt, adds_nothing, items)

    return spreadsheet_schema_lookup, spreadsheet_sql_query
