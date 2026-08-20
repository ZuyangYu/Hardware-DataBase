from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing

from src.agents.query_tokens import tokenize_hardware_query
from src.agents.state import Evidence
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


def _token_match_order(columns: list[str], tokens: list[str]) -> tuple[str, list[str]]:
    """Build a portable SQL relevance expression for the candidate window."""

    expressions: list[str] = []
    params: list[str] = []
    for token in tokens:
        expressions.append(
            "(CASE WHEN "
            + " OR ".join(f"LOWER({column}) LIKE ?" for column in columns)
            + " THEN 1 ELSE 0 END)"
        )
        params.extend(f"%{token}%" for _ in columns)
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
        scope = kb_scope_from_context(kb_name, ctx).require_department("search spreadsheet semantic rows in")
        db_path = self.spreadsheet_service.db_path(scope.department_id, scope.kb_name, create=False)
        if not os.path.exists(db_path):
            return []
        record_id = int((filters or {}).get("record_id") or 0)
        tokens = _tokens(query)
        columns = ["r.semantic_text", "r.raw_text"]
        where, where_params = _like_clauses(columns, tokens)
        relevance_order, order_params = _token_match_order(columns, tokens)
        # SQLite binds placeholders in SQL-text order: WHERE, record filter,
        # then ORDER BY relevance. Keep the parameter list in that order.
        params = [*where_params]
        requested_top_k = max(1, int(top_k))
        record_clause = " AND r.record_id = ?" if record_id else ""
        if record_id:
            params.append(record_id)
        params.extend(order_params)
        params.append(_candidate_limit(requested_top_k))
        sql = f"""
            SELECT r.*, d.document_name, d.source_group
            FROM table_semantic_rows r
            JOIN table_documents d ON d.record_id = r.record_id
            WHERE {where}{record_clause}
            ORDER BY {relevance_order} DESC, r.confidence_score DESC, r.id ASC
            LIMIT ?
        """
        return self._query_rows(db_path, sql, params, query, tokens, requested_top_k)

    def _query_rows(
        self,
        db_path: str,
        sql: str,
        params: list,
        query: str,
        tokens: list[str],
        top_k: int,
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
        rows = _rank_rows(
            rows,
            tokens,
            lambda row: f"{row['semantic_text'] or ''}\n{row['raw_text'] or ''}",
        )
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
        return evidences[:top_k]


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
        columns = ["c.value", "c.raw_value", "c.header"]
        where, where_params = _like_clauses(columns, tokens)
        relevance_order, order_params = _token_match_order(columns, tokens)
        params = [*where_params]
        requested_top_k = max(1, int(top_k))
        record_clause = " AND c.record_id = ?" if record_id else ""
        if record_id:
            params.append(record_id)
        params.extend(order_params)
        params.append(_candidate_limit(requested_top_k))
        sql = f"""
            SELECT c.*, d.document_name, d.source_group
            FROM table_cells c
            JOIN table_documents d ON d.record_id = c.record_id
            WHERE {where}{record_clause}
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
            for row in rows[:requested_top_k]
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
