from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

from src.agents.query_tokens import tokenize_hardware_query
from src.agents.state import Evidence
from src.pipelines.document_rag.schemas import RequestContext
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
        scope = kb_scope_from_context(kb_name, ctx).require_department("search spreadsheet semantic rows in")
        db_path = self.spreadsheet_service.db_path(scope.department_id, scope.kb_name, create=False)
        if not os.path.exists(db_path):
            return []
        record_id = int((filters or {}).get("record_id") or 0)
        tokens = _tokens(query)
        where, params = _like_clauses(["r.semantic_text", "r.raw_text"], tokens)
        record_clause = " AND r.record_id = ?" if record_id else ""
        if record_id:
            params.append(record_id)
        params.append(max(1, int(top_k)))
        sql = f"""
            SELECT r.*, d.document_name, d.source_group
            FROM table_semantic_rows r
            JOIN table_documents d ON d.record_id = r.record_id
            WHERE {where}{record_clause}
            ORDER BY r.confidence_score DESC, r.id ASC
            LIMIT ?
        """
        return self._query_rows(db_path, sql, params, query)

    def _query_rows(self, db_path: str, sql: str, params: list, query: str) -> list[Evidence]:
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
                        "tool": self.name,
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
        scope = kb_scope_from_context(kb_name, ctx).require_department("search spreadsheet cells in")
        db_path = self.spreadsheet_service.db_path(scope.department_id, scope.kb_name, create=False)
        if not os.path.exists(db_path):
            return []
        record_id = int((filters or {}).get("record_id") or 0)
        tokens = _tokens(query)
        where, params = _like_clauses(["c.value", "c.raw_value", "c.header"], tokens)
        record_clause = " AND c.record_id = ?" if record_id else ""
        if record_id:
            params.append(record_id)
        params.append(max(1, int(top_k)))
        sql = f"""
            SELECT c.*, d.document_name, d.source_group
            FROM table_cells c
            JOIN table_documents d ON d.record_id = c.record_id
            WHERE {where}{record_clause}
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
                    "tool": self.name,
                    "query": query,
                    "header": row["header"],
                    "raw_value": row["raw_value"],
                    "number_format": row["number_format"],
                    "source_group": row["source_group"],
                },
            )
            for row in rows
        ]


class SpreadsheetProfileTool:
    name = "spreadsheet_profile"
    description = "Read Excel workbook and sheet profiles for source planning."

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
        record_id = int((filters or {}).get("record_id") or 0)
        if not record_id:
            return []
        scope = kb_scope_from_context(kb_name, ctx).require_department("read spreadsheet profile in")
        db_path = self.spreadsheet_service.db_path(scope.department_id, scope.kb_name, create=False)
        if not os.path.exists(db_path):
            return []
        try:
            from src.pipelines.spreadsheet.table_store import TableIndexStore

            profile = TableIndexStore(db_path).get_document_profile(record_id)
        except Exception:
            return []
        if not profile:
            return []
        source_name = profile.get("document_name", f"record:{record_id}")
        sheet_lines = []
        for sheet in profile.get("sheets", []):
            headers = ", ".join(str(item.get("header") or "") for item in sheet.get("headers", [])[:12])
            sheet_lines.append(
                f"Sheet {sheet.get('sheet_name')}: rows={sheet.get('row_count')}, "
                f"semantic_rows={sheet.get('semantic_row_count')}, headers={headers}"
            )
        content = "\n".join(sheet_lines) or json.dumps(profile, ensure_ascii=False)
        return [
            Evidence(
                id=f"xlsx:{record_id}:profile",
                content=content,
                source_name=source_name,
                content_kind="spreadsheet_table",
                processor_kind="spreadsheet_table",
                score=1.0,
                locator={"record_id": record_id},
                metadata={"tool": self.name, "query": query, "profile": profile},
            )
        ]
