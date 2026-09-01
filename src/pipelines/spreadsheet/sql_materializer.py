"""Spreadsheet SQL materialization.

将 table_semantic_rows 的 values(前向填充后)物化为每 record×sheet 一张物理表
(col_1..col_N + 类型), 供 Agent 通过只读 SQL 做筛选/聚合/比较类查询。

设计取自 TableRAG(EMNLP 2025)离线入库经验并按本项目调整:
- 列名统一 col_N, 中文表头映射存 registry.schema_json(不学其正则转写, 中文转写后信息全丢)
- 类型推断保守三态 INTEGER/REAL/TEXT; 日期已是展示字符串, 归 TEXT
- schema JSON 附每列 3 个确定性样本值(给 32B 写 SQL 时对齐值形态)
- 保留 row_index 支撑层级/相邻行查询(如 Debug List 的类别->子项)
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_MATERIALIZED_COLUMNS = 256
MAX_SAMPLE_VALUES = 3

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})*(\.\d+)?$|^[+-]?\d+(\.\d+)?$")

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS sql_table_registry (
    record_id INTEGER NOT NULL,
    sheet_name TEXT NOT NULL,
    table_name TEXT NOT NULL UNIQUE,
    column_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    schema_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (record_id, sheet_name)
)
"""


def ensure_registry_table(conn) -> None:
    conn.execute(_REGISTRY_DDL)


def drop_record_tables(conn, record_id: int) -> None:
    """删除某 record 的全部物理表与登记行(重解析/删除文档时调用)."""
    ensure_registry_table(conn)
    rows = conn.execute(
        "SELECT table_name FROM sql_table_registry WHERE record_id = ?",
        (record_id,),
    ).fetchall()
    for row in rows:
        table_name = str(row["table_name"] or "")
        if table_name and re.fullmatch(r"t_\d+_\d+", table_name):
            conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    conn.execute("DELETE FROM sql_table_registry WHERE record_id = ?", (record_id,))


def _infer_column_type(values: list[str]) -> str:
    """对一列非空字符串值做保守类型推断."""
    if not values:
        return "TEXT"
    cleaned = [v.strip().replace(",", "") for v in values]
    if all(_INT_RE.match(v) for v in cleaned):
        return "INTEGER"
    if all(_FLOAT_RE.match(v) for v in cleaned):
        return "REAL"
    return "TEXT"


def _convert_value(value: str, dtype: str):
    text = str(value or "").strip()
    if dtype == "INTEGER":
        try:
            return int(text.replace(",", ""))
        except ValueError:
            return None
    if dtype == "REAL":
        try:
            return float(text.replace(",", ""))
        except ValueError:
            return None
    return text


def _distinct_samples(values: list[str], limit: int = MAX_SAMPLE_VALUES) -> list[str]:
    samples: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        samples.append(text)
        if len(samples) >= limit:
            break
    return samples


def materialize_sheet(
    conn,
    record_id: int,
    sheet_name: str,
    semantic_rows: list[dict],
    *,
    table_seq: int | None = None,
) -> dict | None:
    """把一个 sheet 的语义行物化为物理表并登记; 无语义行时返回 None.

    semantic_rows 是 _sheet_semantic_rows 的输出, values 已做前向填充.
    """
    if not semantic_rows:
        return None

    ensure_registry_table(conn)

    # 旧登记(同 record 内重名 sheet 理论上不存在, 防御性处理)
    conn.execute(
        "DELETE FROM sql_table_registry WHERE record_id = ? AND sheet_name = ?",
        (record_id, sheet_name),
    )

    # 列序 = 语义行表头列序。不能用"首次出现顺序": pre_header_row(位于推断表头
    # 之前的真实数据行)通常只覆盖部分列, 会把 col_N 编号整体错位。改为用覆盖列
    # 最多的语义行(values 键按物理列序插入)作为列序种子, 其余按首次出现追加。
    headers: list[str] = []
    seen_headers: set[str] = set()
    seed_row = max(semantic_rows, key=lambda row: len(row.get("values") or {}))
    ordered_rows = [seed_row] + [row for row in semantic_rows if row is not seed_row]
    for semantic_row in ordered_rows:
        for header in semantic_row.get("values", {}):
            if header and header not in seen_headers:
                seen_headers.add(header)
                headers.append(header)
        if len(headers) >= MAX_MATERIALIZED_COLUMNS:
            break
    if not headers:
        return None
    headers = headers[:MAX_MATERIALIZED_COLUMNS]

    # 类型推断使用原始值(未经前向填充), 避免继承值干扰数值列判定
    raw_by_header: dict[str, list[str]] = {header: [] for header in headers}
    for semantic_row in semantic_rows:
        raw_values = semantic_row.get("raw_values") or {}
        for header in headers:
            value = str(raw_values.get(header) or "").strip()
            if value:
                raw_by_header[header].append(value)

    column_schema: list[dict] = []
    for index, header in enumerate(headers, start=1):
        column_schema.append(
            {
                "column": f"col_{index}",
                "header": header,
                "dtype": _infer_column_type(raw_by_header[header]),
                "samples": _distinct_samples(raw_by_header[header]),
            }
        )

    physical_name = f"t_{record_id}_{table_seq or 1}"
    conn.execute(f'DROP TABLE IF EXISTS "{physical_name}"')
    column_defs = ", ".join(
        f'"col_{index}" {column_schema[index - 1]["dtype"]}'
        for index in range(1, len(headers) + 1)
    )
    conn.execute(
        f'CREATE TABLE "{physical_name}" ('
        f"row_index INTEGER NOT NULL, "
        f"header_row_index INTEGER NOT NULL DEFAULT 0, "
        f'section TEXT NOT NULL DEFAULT \'\', '
        f"{column_defs})"
    )

    dtype_by_header = {item["header"]: item["dtype"] for item in column_schema}
    insert_sql = (
        f'INSERT INTO "{physical_name}" '
        f"(row_index, header_row_index, section, "
        + ", ".join(f'"col_{i}"' for i in range(1, len(headers) + 1))
        + ") VALUES ("
        + ", ".join(["?"] * (3 + len(headers)))
        + ")"
    )
    params: list[tuple] = []
    for semantic_row in semantic_rows:
        values = semantic_row.get("values") or {}
        row_params: list[Any] = [
            int(semantic_row.get("row_index") or 0),
            int(semantic_row.get("header_row_index") or 0),
            str(semantic_row.get("section_title") or ""),
        ]
        for header in headers:
            row_params.append(
                _convert_value(values.get(header, ""), dtype_by_header[header])
            )
        params.append(tuple(row_params))
    conn.executemany(insert_sql, params)

    schema_json = {
        "table_name": physical_name,
        "sheet_name": sheet_name,
        "columns": column_schema,
        "row_count": len(params),
        "note": "列值已按语义层做前向填充; row_index 为原表行号, 可用相邻行差值处理层级表",
    }
    conn.execute(
        """
        INSERT INTO sql_table_registry (
            record_id, sheet_name, table_name, column_count, row_count, schema_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            sheet_name,
            physical_name,
            len(headers),
            len(params),
            json.dumps(schema_json, ensure_ascii=False),
        ),
    )
    return schema_json


def list_registry_for_document(conn, record_id: int) -> list[dict]:
    ensure_registry_table(conn)
    rows = conn.execute(
        """
        SELECT sheet_name, table_name, column_count, row_count, schema_json
        FROM sql_table_registry WHERE record_id = ? ORDER BY sheet_name
        """,
        (record_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["schema"] = json.loads(item.pop("schema_json") or "{}")
        result.append(item)
    return result
