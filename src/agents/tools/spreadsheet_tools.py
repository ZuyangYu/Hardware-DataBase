"""Spreadsheet (xlsx structured index) retrieval tools."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

from src.agents.schemas import Evidence
from src.core.query_tokens import tokenize_hardware_query
from src.services.kb_scope import kb_scope_from_context
from src.services.spreadsheet_index_service import SpreadsheetIndexService


def _tokens(query: str) -> list[str]:
    return tokenize_hardware_query(query, max_tokens=8, include_cjk_ngrams=False)


def _like_clauses(columns: list[str], tokens: list[str]) -> tuple[str, list[str]]:
    clauses = []
    params: list[str] = []
    for token in tokens:
        token_clauses = []
        for column in columns:
            token_clauses.append(f"LOWER({column}) LIKE ?")
            params.append(f"%{token}%")
        clauses.append("(" + " OR ".join(token_clauses) + ")")
    if not clauses:
        return "1=1", []
    return "(" + " OR ".join(clauses) + ")", params


def _semantic_rows(rt, service: SpreadsheetIndexService, query: str, top_k: int) -> list[Evidence]:
    scope = kb_scope_from_context(rt.kb_name, rt.ctx).require_department("search spreadsheet semantic rows in")
    db_path = service.db_path(scope.department_id, scope.kb_name, create=False)
    if not os.path.exists(db_path):
        return []
    tokens = _tokens(query)
    where, params = _like_clauses(["r.semantic_text", "r.raw_text"], tokens)
    params.append(max(1, int(top_k)))
    sql = f"""
        SELECT r.*, d.document_name, d.source_group
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
    for row in rows:
        values = json.loads(row["values_json"] or "{}")
        raw_values = json.loads(row["raw_values_json"] or "{}")
        evidences.append(
            Evidence(
                id=f"xlsx:{row['record_id']}:{row['sheet_name']}:{row['row_index']}:semantic",
                content=row["semantic_text"] or row["raw_text"],
                source_name=row["document_name"],
                content_kind="spreadsheet_table",
                processor_kind="spreadsheet_table",
                score=float(row["confidence_score"] or 0.0),
                locator={
                    "record_id": row["record_id"],
                    "sheet_name": row["sheet_name"],
                    "row_index": row["row_index"],
                    "header_row_index": row["header_row_index"],
                },
                metadata={
                    "tool": "spreadsheet_row_search",
                    "query": query,
                    "raw_text": row["raw_text"],
                    "values": values,
                    "raw_values": raw_values,
                    "confidence": row["confidence"],
                    "source_group": row["source_group"],
                },
            )
        )
    return evidences


def _cell_rows(rt, service: SpreadsheetIndexService, query: str, top_k: int) -> list[Evidence]:
    scope = kb_scope_from_context(rt.kb_name, rt.ctx).require_department("search spreadsheet cells in")
    db_path = service.db_path(scope.department_id, scope.kb_name, create=False)
    if not os.path.exists(db_path):
        return []
    tokens = _tokens(query)
    where, params = _like_clauses(["c.value", "c.raw_value", "c.header"], tokens)
    params.append(max(1, int(top_k)))
    sql = f"""
        SELECT c.*, d.document_name, d.source_group
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
    return [
        Evidence(
            id=f"xlsx:{row['record_id']}:{row['sheet_name']}:{row['cell_ref']}:cell",
            content=f"{row['header'] or 'Cell'}: {row['value']}",
            source_name=row["document_name"],
            content_kind="spreadsheet_table",
            processor_kind="spreadsheet_table",
            score=1.0,
            locator={
                "record_id": row["record_id"],
                "sheet_name": row["sheet_name"],
                "row_index": row["row_index"],
                "col_index": row["col_index"],
                "cell_ref": row["cell_ref"],
            },
            metadata={
                "tool": "spreadsheet_cell_lookup",
                "query": query,
                "header": row["header"],
                "raw_value": row["raw_value"],
                "number_format": row["number_format"],
                "source_group": row["source_group"],
            },
        )
        for row in rows
    ]


def make_spreadsheet_tools(rt, spreadsheet_service: SpreadsheetIndexService):
    """Return the two spreadsheet tool closures bound to this run."""

    def spreadsheet_row_search(query: str, top_k: int = rt.top_k) -> str:
        """按语义检索 Excel 表格行（BOM 物料、参数、替代料、测试记录等整行业务数据）。"""
        from src.agents.tools.runtime import format_evidence_for_llm, timed_tool_call

        items = timed_tool_call(
            rt,
            "spreadsheet_row_search",
            query,
            None,
            lambda: _semantic_rows(rt, spreadsheet_service, query, max(1, min(int(top_k), 20))),
        )
        return format_evidence_for_llm(items)

    def spreadsheet_cell_lookup(query: str, top_k: int = rt.top_k) -> str:
        """按精确值检索单元格（表头/原始值匹配），适合查找具体参数值、型号、数量等。"""
        from src.agents.tools.runtime import format_evidence_for_llm, timed_tool_call

        items = timed_tool_call(
            rt,
            "spreadsheet_cell_lookup",
            query,
            None,
            lambda: _cell_rows(rt, spreadsheet_service, query, max(1, min(int(top_k), 20))),
        )
        return format_evidence_for_llm(items)

    return spreadsheet_row_search, spreadsheet_cell_lookup
