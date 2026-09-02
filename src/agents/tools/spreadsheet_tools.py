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
        """按语义检索 Excel 表格行, 只适合取"具体某行/某个值"这类单点信息。
        注意: 统计数量、求和、最值、均值、按条件筛选多行、数值比较等问题用本工具会漏行漏列,
        必须改用 spreadsheet_schema_lookup + spreadsheet_sql_query 一次完成。"""
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        items, adds_nothing = timed_tool_call(
            rt,
            "spreadsheet_row_search",
            query,
            None,
            lambda: _semantic_rows(rt, spreadsheet_service, query, max(1, min(int(top_k), 20))),
        )
        return format_tool_result(rt, adds_nothing, items)

    def spreadsheet_cell_lookup(query: str, top_k: int = rt.top_k) -> str:
        """按精确值检索单元格（表头/原始值匹配），适合查找具体参数值、型号、数量等。"""
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        items, adds_nothing = timed_tool_call(
            rt,
            "spreadsheet_cell_lookup",
            query,
            None,
            lambda: _cell_rows(rt, spreadsheet_service, query, max(1, min(int(top_k), 20))),
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
                SELECT r.table_name, r.sheet_name, r.column_count, r.row_count,
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
                "table_name": row["table_name"],
                "sheet_name": row["sheet_name"],
                "document_name": row["document_name"],
                "columns": schema.get("columns", []),
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
    return (
        f"表 {entry['table_name']} | 文档 {entry['document_name']} | sheet「{entry['sheet_name']}」"
        f" | {entry.get('row_count', '?')} 行\n  列: {columns}"
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
        """列出可执行 SQL 的物化表 schema(表名/中文表头/类型/样本值), 写 SQL 前必查。"""
        from src.agents.tools.runtime import timed_tool_call

        scope = kb_scope_from_context(rt.kb_name, rt.ctx).require_department("lookup spreadsheet sql schema in")
        db_path = spreadsheet_service.db_path(scope.department_id, scope.kb_name, create=False)

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
            return [
                Evidence(
                    id=f"xlsx-schema:{entry['table_name']}",
                    content=_format_schema_entry(entry),
                    source_name=entry["document_name"],
                    content_kind="spreadsheet_schema",
                    processor_kind="spreadsheet_table",
                    score=1.0,
                    locator={"table_name": entry["table_name"], "sheet_name": entry["sheet_name"]},
                    metadata={"tool": "spreadsheet_schema_lookup", "query": query},
                )
                for entry in selected
            ]

        items, adds_nothing = timed_tool_call(rt, "spreadsheet_schema_lookup", query, None, _run)
        if not items:
            return "当前知识库没有可用的 SQL 物化表。"
        from src.agents.tools.runtime import format_tool_result

        return format_tool_result(rt, adds_nothing, items)

    def spreadsheet_sql_query(sql: str) -> str:
        """执行只读 SQL 查询物化表(仅 SELECT, 自动 LIMIT)。筛选/聚合/比较/统计类问题优先用本工具。

        出错时按错误信息修正 SQL 后重试, 最多 2 次; 表名与列结构先用 spreadsheet_schema_lookup 查询。
        """
        from src.agents.tools.runtime import format_tool_result, timed_tool_call

        scope = kb_scope_from_context(rt.kb_name, rt.ctx).require_department("query spreadsheet sql in")
        db_path = spreadsheet_service.db_path(scope.department_id, scope.kb_name, create=False)

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
                        metadata={"tool": "spreadsheet_sql_query", "sql": sql, "status": "invalid"},
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
                        metadata={"tool": "spreadsheet_sql_query", "sql": sql, "status": "error"},
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

