import json
import os
import re
import sqlite3
import hashlib
from contextlib import closing
from dataclasses import dataclass, field

import src.settings
from src.ingestion.kb_paths import safe_child_path, validate_kb_name
from src.pipelines.spreadsheet.xlsx_parser import ParsedWorkbook, _row_col_from_ref, parse_xlsx


SPREADSHEET_KIND_UNIVERSAL = "universal_table_index"


@dataclass
class TableIndexStats:
    sheet_count: int = 0
    row_count: int = 0
    cell_count: int = 0
    document_kind: str = SPREADSHEET_KIND_UNIVERSAL
    embedded_object_count: int = 0
    media_object_count: int = 0
    drawing_object_count: int = 0
    text_block_count: int = 0
    semantic_row_count: int = 0
    warnings: list[str] = field(default_factory=list)


class TableIndexStore:
    def __init__(self, db_path: str):
        if not db_path:
            raise ValueError("db_path is required for scoped table index storage")
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS table_documents (
                    record_id INTEGER PRIMARY KEY,
                    kb_id INTEGER NOT NULL DEFAULT 0,
                    kb_name TEXT NOT NULL,
                    department_id TEXT NOT NULL DEFAULT '',
                    document_name TEXT NOT NULL,
                    source_group TEXT NOT NULL DEFAULT '',
                    local_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    document_kind TEXT NOT NULL DEFAULT 'tabular',
                    embedded_object_count INTEGER NOT NULL DEFAULT 0,
                    media_object_count INTEGER NOT NULL DEFAULT 0,
                    drawing_object_count INTEGER NOT NULL DEFAULT 0,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS table_sheets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    sheet_name TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    column_count INTEGER NOT NULL DEFAULT 0,
                    non_empty_row_count INTEGER NOT NULL DEFAULT 0,
                    non_empty_cell_count INTEGER NOT NULL DEFAULT 0,
                    header_row_index INTEGER,
                    header_json TEXT NOT NULL DEFAULT '[]',
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    merged_ranges_json TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(record_id) REFERENCES table_documents(record_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS table_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    sheet_name TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    row_text TEXT NOT NULL,
                    FOREIGN KEY(record_id) REFERENCES table_documents(record_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS table_cells (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    sheet_name TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    col_index INTEGER NOT NULL,
                    cell_ref TEXT NOT NULL DEFAULT '',
                    value TEXT NOT NULL,
                    header TEXT NOT NULL DEFAULT '',
                    number_format TEXT NOT NULL DEFAULT '',
                    raw_value TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(record_id) REFERENCES table_documents(record_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS table_text_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    sheet_name TEXT NOT NULL,
                    block_index INTEGER NOT NULL,
                    block_text TEXT NOT NULL,
                    FOREIGN KEY(record_id) REFERENCES table_documents(record_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS table_semantic_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    sheet_name TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    header_row_index INTEGER NOT NULL DEFAULT 0,
                    semantic_text TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    values_json TEXT NOT NULL DEFAULT '{}',
                    raw_values_json TEXT NOT NULL DEFAULT '{}',
                    inherited_json TEXT NOT NULL DEFAULT '{}',
                    confidence TEXT NOT NULL DEFAULT '',
                    confidence_score REAL NOT NULL DEFAULT 0,
                    confidence_reasons_json TEXT NOT NULL DEFAULT '[]',
                    source_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(record_id) REFERENCES table_documents(record_id) ON DELETE CASCADE
                )
            """)
            self._ensure_columns(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_rows_lookup ON table_rows(record_id, sheet_name, row_index)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_rows_text ON table_rows(row_text)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_cells_lookup ON table_cells(record_id, sheet_name, row_index, col_index)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_cells_value ON table_cells(value)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_cells_header ON table_cells(header)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_text_blocks_lookup ON table_text_blocks(record_id, sheet_name, block_index)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_text_blocks_text ON table_text_blocks(block_text)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_semantic_rows_lookup ON table_semantic_rows(record_id, sheet_name, row_index)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_semantic_rows_text ON table_semantic_rows(semantic_text)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_semantic_rows_confidence ON table_semantic_rows(confidence)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_documents_scope ON table_documents(kb_name, department_id)")

    def _ensure_columns(self, conn):
        self._ensure_table_columns(conn, "table_documents", {
            "kb_id": "ALTER TABLE table_documents ADD COLUMN kb_id INTEGER NOT NULL DEFAULT 0",
            "source_group": "ALTER TABLE table_documents ADD COLUMN source_group TEXT NOT NULL DEFAULT ''",
            "document_kind": "ALTER TABLE table_documents ADD COLUMN document_kind TEXT NOT NULL DEFAULT 'tabular'",
            "embedded_object_count": "ALTER TABLE table_documents ADD COLUMN embedded_object_count INTEGER NOT NULL DEFAULT 0",
            "media_object_count": "ALTER TABLE table_documents ADD COLUMN media_object_count INTEGER NOT NULL DEFAULT 0",
            "drawing_object_count": "ALTER TABLE table_documents ADD COLUMN drawing_object_count INTEGER NOT NULL DEFAULT 0",
            "warnings_json": "ALTER TABLE table_documents ADD COLUMN warnings_json TEXT NOT NULL DEFAULT '[]'",
        })
        self._ensure_table_columns(conn, "table_sheets", {
            "non_empty_row_count": "ALTER TABLE table_sheets ADD COLUMN non_empty_row_count INTEGER NOT NULL DEFAULT 0",
            "non_empty_cell_count": "ALTER TABLE table_sheets ADD COLUMN non_empty_cell_count INTEGER NOT NULL DEFAULT 0",
            "header_row_index": "ALTER TABLE table_sheets ADD COLUMN header_row_index INTEGER",
            "header_json": "ALTER TABLE table_sheets ADD COLUMN header_json TEXT NOT NULL DEFAULT '[]'",
            "profile_json": "ALTER TABLE table_sheets ADD COLUMN profile_json TEXT NOT NULL DEFAULT '{}'",
            "merged_ranges_json": "ALTER TABLE table_sheets ADD COLUMN merged_ranges_json TEXT NOT NULL DEFAULT '[]'",
        })
        self._ensure_table_columns(conn, "table_cells", {
            "number_format": "ALTER TABLE table_cells ADD COLUMN number_format TEXT NOT NULL DEFAULT ''",
            "raw_value": "ALTER TABLE table_cells ADD COLUMN raw_value TEXT NOT NULL DEFAULT ''",
        })

    def _ensure_table_columns(self, conn, table_name: str, migrations: dict[str, str]):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

    def index_xlsx(
        self,
        record_id: int,
        kb_name: str,
        department_id: str,
        document_name: str,
        source_group: str,
        file_path: str,
        local_path: str,
        content_hash: str,
        kb_id: int | None = None,
        progress_callback=None,
    ) -> TableIndexStats:
        if progress_callback:
            progress_callback(20, "读取 Excel 工作簿")
        workbook = parse_xlsx(file_path)
        stats = _workbook_stats(workbook)
        if progress_callback:
            progress_callback(35, f"分析工作表结构（{len(workbook.sheets)} 个 sheet）")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM table_cells WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_rows WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_text_blocks WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_semantic_rows WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_sheets WHERE record_id = ?", (record_id,))
                conn.execute(
                    """
                    INSERT INTO table_documents (
                        record_id, kb_id, kb_name, department_id, document_name, source_group, local_path,
                        content_hash, document_kind, embedded_object_count, media_object_count,
                        drawing_object_count, warnings_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(record_id) DO UPDATE SET
                        kb_id = excluded.kb_id,
                        kb_name = excluded.kb_name,
                        department_id = excluded.department_id,
                        document_name = excluded.document_name,
                        source_group = excluded.source_group,
                        local_path = excluded.local_path,
                        content_hash = excluded.content_hash,
                        document_kind = excluded.document_kind,
                        embedded_object_count = excluded.embedded_object_count,
                        media_object_count = excluded.media_object_count,
                        drawing_object_count = excluded.drawing_object_count,
                        warnings_json = excluded.warnings_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        record_id, int(kb_id or 0), kb_name, department_id, document_name, source_group, local_path,
                        content_hash, stats.document_kind, stats.embedded_object_count,
                        stats.media_object_count, stats.drawing_object_count,
                        json.dumps(stats.warnings, ensure_ascii=False),
                    ),
                )
                total_sheets = max(1, len(workbook.sheets))
                for sheet_number, sheet in enumerate(workbook.sheets, start=1):
                    if progress_callback:
                        progress = 35 + int(((sheet_number - 1) / total_sheets) * 50)
                        progress_callback(progress, f"写入工作表 {sheet_number}/{total_sheets}: {sheet.name}")
                    profile = _sheet_profile(sheet.rows)
                    semantic_rows = _sheet_semantic_rows(sheet, profile)
                    profile["semantic_row_count"] = len(semantic_rows)
                    profile["semantic_confidence_counts"] = _semantic_confidence_counts(semantic_rows)
                    stats.semantic_row_count += len(semantic_rows)
                    conn.execute(
                        """
                        INSERT INTO table_sheets (
                            record_id, sheet_name, row_count, column_count, non_empty_row_count,
                            non_empty_cell_count, header_row_index, header_json, profile_json, merged_ranges_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record_id, sheet.name, profile["row_count"], profile["column_count"],
                            profile["non_empty_row_count"], profile["non_empty_cell_count"],
                            profile["header_row_index"], json.dumps(profile["headers"], ensure_ascii=False),
                            json.dumps(profile, ensure_ascii=False),
                            json.dumps(sheet.merged_ranges, ensure_ascii=False),
                        ),
                    )
                    row_records = []
                    for fallback_index, row in enumerate(sheet.rows, start=1):
                        row_index = _sheet_row_index(sheet, fallback_index)
                        row_text = _row_to_text(row)
                        if row_text:
                            row_records.append((record_id, sheet.name, row_index, row_text))
                    conn.executemany(
                        """
                        INSERT INTO table_rows (record_id, sheet_name, row_index, row_text)
                        VALUES (?, ?, ?, ?)
                        """,
                        row_records,
                    )
                    headers_by_col = {item["col_index"]: item["header"] for item in profile["headers"]}
                    cell_records = [
                        (
                            record_id, sheet.name, cell.row_index, cell.col_index,
                            cell.ref, cell.value, headers_by_col.get(cell.col_index, ""),
                            cell.number_format, cell.raw_value,
                        )
                        for cell in sheet.cells
                    ]
                    conn.executemany(
                        """
                        INSERT INTO table_cells (
                            record_id, sheet_name, row_index, col_index, cell_ref, value, header,
                            number_format, raw_value
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        cell_records,
                    )
                    text_blocks = _sheet_text_blocks(sheet)
                    stats.text_block_count += len(text_blocks)
                    conn.executemany(
                        """
                        INSERT INTO table_text_blocks (record_id, sheet_name, block_index, block_text)
                        VALUES (?, ?, ?, ?)
                        """,
                        [
                            (record_id, sheet.name, index, block["text"])
                            for index, block in enumerate(text_blocks, start=1)
                        ],
                    )
                    semantic_row_records = [
                        _semantic_row_db_record(record_id, sheet.name, semantic_row)
                        for semantic_row in semantic_rows
                    ]
                    conn.executemany(
                        """
                        INSERT INTO table_semantic_rows (
                            record_id, sheet_name, row_index, header_row_index,
                            semantic_text, raw_text, values_json, raw_values_json,
                            inherited_json, confidence, confidence_score,
                            confidence_reasons_json, source_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        semantic_row_records,
                    )
                    if progress_callback:
                        progress = 35 + int((sheet_number / total_sheets) * 50)
                        progress_callback(progress, f"完成工作表 {sheet_number}/{total_sheets}: {sheet.name}")
                if progress_callback:
                    progress_callback(90, "提交表格结构化索引")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return stats

    def delete_document(self, record_id: int):
        document = None
        with closing(self._connect()) as conn:
            document = conn.execute(
                "SELECT * FROM table_documents WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM table_cells WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_rows WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_text_blocks WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_semantic_rows WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_sheets WHERE record_id = ?", (record_id,))
                conn.execute("DELETE FROM table_documents WHERE record_id = ?", (record_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if document:
            _delete_legacy_index_files_for_document(
                department_id=document["department_id"],
                kb_name=document["kb_name"],
                record_id=document["record_id"],
                content_hash=document["content_hash"],
            )

    def get_document_profile(self, record_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            document = conn.execute(
                "SELECT * FROM table_documents WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if not document:
                return None
            sheets = conn.execute(
                "SELECT * FROM table_sheets WHERE record_id = ? ORDER BY id",
                (record_id,),
            ).fetchall()
            text_block_count = conn.execute(
                "SELECT COUNT(*) AS count FROM table_text_blocks WHERE record_id = ?",
                (record_id,),
            ).fetchone()["count"]
            sheet_profiles = {
                row["sheet_name"]: _json_loads(row["profile_json"], {})
                for row in sheets
            }
            semantic_row_count = conn.execute(
                "SELECT COUNT(*) AS count FROM table_semantic_rows WHERE record_id = ?",
                (record_id,),
            ).fetchone()["count"]
            sheet_semantic_counts = {
                row["sheet_name"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT sheet_name, COUNT(*) AS count
                    FROM table_semantic_rows
                    WHERE record_id = ?
                    GROUP BY sheet_name
                    """,
                    (record_id,),
                ).fetchall()
            }
            sheet_block_counts = {
                row["sheet_name"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT sheet_name, COUNT(*) AS count
                    FROM table_text_blocks
                    WHERE record_id = ?
                    GROUP BY sheet_name
                    """,
                    (record_id,),
                ).fetchall()
            }
        return {
            "record_id": document["record_id"],
            "kb_id": document["kb_id"],
            "document_name": document["document_name"],
            "document_kind": document["document_kind"],
            "processing_mode": "structured_excel_parse",
            "text_block_count": text_block_count,
            "semantic_row_count": semantic_row_count,
            "embedded_object_count": document["embedded_object_count"],
            "media_object_count": document["media_object_count"],
            "drawing_object_count": document["drawing_object_count"],
            "warnings": _json_loads(document["warnings_json"], []),
            "sheets": [
                {
                    "sheet_name": sheet["sheet_name"],
                    "row_count": sheet["row_count"],
                    "column_count": sheet["column_count"],
                    "non_empty_row_count": sheet["non_empty_row_count"],
                    "non_empty_cell_count": sheet["non_empty_cell_count"],
                    "header_row_index": sheet["header_row_index"],
                    "headers": _json_loads(sheet["header_json"], []),
                    "text_block_count": sheet_block_counts.get(sheet["sheet_name"], 0),
                    "semantic_row_count": sheet_semantic_counts.get(sheet["sheet_name"], 0),
                    "semantic_confidence_counts": sheet_profiles.get(sheet["sheet_name"], {}).get(
                        "semantic_confidence_counts", {}
                    ),
                }
                for sheet in sheets
            ],
        }

    def rank_documents_by_terms(self, terms: list[str], limit: int = 20) -> dict[int, dict]:
        """Score workbook records by exact/partial matches in indexed cells.

        This is used only for source routing before a fast query. It returns
        record identifiers and matching terms, never cell content, so the
        planner can scope the later evidence retrieval without duplicating it.
        """
        normalized_terms = list(dict.fromkeys(str(term or "").strip().casefold() for term in terms if str(term or "").strip()))
        if not normalized_terms:
            return {}

        by_record: dict[int, dict[str, int]] = {}
        with closing(self._connect()) as conn:
            for term in normalized_terms[:8]:
                pattern = f"%{term}%"
                rows = conn.execute(
                    """
                    SELECT record_id, value, raw_value, header
                    FROM table_cells
                    WHERE LOWER(value) LIKE ? OR LOWER(raw_value) LIKE ? OR LOWER(header) LIKE ?
                    LIMIT 300
                    """,
                    (pattern, pattern, pattern),
                ).fetchall()
                for row in rows:
                    value = str(row["value"] or "").casefold()
                    raw_value = str(row["raw_value"] or "").casefold()
                    header = str(row["header"] or "").casefold()
                    if term == value or term == raw_value:
                        score = 8
                    elif term in value or term in raw_value:
                        score = 5
                    elif term in header:
                        score = 3
                    else:
                        continue
                    record_scores = by_record.setdefault(int(row["record_id"]), {})
                    record_scores[term] = max(score, record_scores.get(term, 0))

        ranked = sorted(
            (
                (record_id, sum(term_scores.values()), sorted(term_scores))
                for record_id, term_scores in by_record.items()
                # A shared column heading (for example "Part Number") is a
                # weak hint. Require a value/raw-value match before treating a
                # workbook as a precise source match.
                if max(term_scores.values()) >= 5
            ),
            key=lambda item: (-item[1], item[0]),
        )[: max(1, int(limit))]
        return {
            record_id: {"score": score, "matched_terms": matched_terms}
            for record_id, score, matched_terms in ranked
        }


def _workbook_stats(workbook: ParsedWorkbook) -> TableIndexStats:
    stats = TableIndexStats(
        sheet_count=len(workbook.sheets),
        embedded_object_count=workbook.embedded_object_count,
        media_object_count=workbook.media_object_count,
        drawing_object_count=workbook.drawing_object_count,
    )
    sheet_profiles = [_sheet_profile(sheet.rows) for sheet in workbook.sheets]
    stats.row_count = sum(profile["row_count"] for profile in sheet_profiles)
    stats.cell_count = sum(profile["non_empty_cell_count"] for profile in sheet_profiles)
    stats.document_kind = SPREADSHEET_KIND_UNIVERSAL
    stats.warnings = _workbook_warnings(stats)
    return stats


def _workbook_warnings(stats: TableIndexStats) -> list[str]:
    warnings = []
    warnings.append("Excel 已按原貌结构建立文档、工作表、行、单元格和块级记录。")
    embedded_parts = []
    if stats.embedded_object_count:
        embedded_parts.append(f"{stats.embedded_object_count} 个嵌入对象")
    if stats.media_object_count:
        embedded_parts.append(f"{stats.media_object_count} 个媒体对象")
    if stats.drawing_object_count:
        embedded_parts.append(f"{stats.drawing_object_count} 个绘图对象")
    if embedded_parts:
        warnings.append(f"检测到 {', '.join(embedded_parts)}；当前记录对象数量，嵌入文档、图片和绘图内容暂未展开。")
    return warnings


def _sheet_profile(rows: list[list[str]]) -> dict:
    row_count = len(rows)
    column_count = max((len(row) for row in rows), default=0)
    non_empty_rows = [row for row in rows if _row_to_text(row)]
    non_empty_cell_count = sum(1 for row in rows for value in row if str(value).strip())
    headers = _infer_headers(rows)
    title_like_row_count = sum(1 for row in non_empty_rows if _is_title_like_row(row))
    capacity = max(1, row_count * max(1, column_count))
    sparsity = 1.0 - (non_empty_cell_count / capacity)
    return {
        "row_count": row_count,
        "column_count": column_count,
        "non_empty_row_count": len(non_empty_rows),
        "non_empty_cell_count": non_empty_cell_count,
        "header_row_index": headers[0]["row_index"] if headers else None,
        "headers": headers,
        "title_like_row_count": title_like_row_count,
        "sparsity": round(sparsity, 4),
    }


def _infer_headers(rows: list[list[str]]) -> list[dict]:
    best_index = None
    best_score = 0.0
    for index, row in enumerate(rows[:30], start=1):
        values = [str(value).strip() for value in row if str(value).strip()]
        if len(values) < 2:
            continue
        if all(_is_numeric_like(value) for value in values):
            continue
        text_count = sum(1 for value in values if re.search(r"[A-Za-z一-鿿]", value))
        duplicate_penalty = len(values) - len(set(values))
        score = text_count + min(len(values), 8) * 0.25 - duplicate_penalty
        if score > best_score:
            best_score = score
            best_index = index
    if best_index is None or best_score < 2:
        return []
    headers = []
    for col_index, value in enumerate(rows[best_index - 1], start=1):
        header = str(value).strip()
        if header:
            headers.append({"col_index": col_index, "header": header, "row_index": best_index})
    return headers


def _is_title_like_row(row: list[str]) -> bool:
    values = [str(value).strip() for value in row if str(value).strip()]
    if len(values) != 1:
        return False
    value = values[0]
    return len(value) >= 8 or bool(re.search(r"[一-鿿].*[A-Za-z]|[A-Za-z].*[一-鿿]", value))


def _is_numeric_like(value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    normalized = text.replace(",", "").replace("%", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = "-" + normalized[1:-1]
    normalized = normalized.lstrip("$￥¥")
    try:
        float(normalized)
    except ValueError:
        return False
    return True


def _row_to_text(row: list[str]) -> str:
    values = [str(value).strip() for value in row if str(value).strip()]
    return " | ".join(values)


def _sheet_text_blocks(sheet, max_chars: int = 1200) -> list[dict]:
    blocks = []
    current_lines = []
    current_start = None
    current_end = None
    current_size = 0
    for fallback_index, row in enumerate(sheet.rows, start=1):
        row_index = _sheet_row_index(sheet, fallback_index)
        row_text = _row_to_text(row)
        if not row_text:
            if current_lines:
                blocks.append(_format_text_block(sheet.name, current_start, current_end, current_lines))
                current_lines = []
                current_start = None
                current_end = None
                current_size = 0
            continue
        if current_lines and current_size + len(row_text) > max_chars:
            blocks.append(_format_text_block(sheet.name, current_start, current_end, current_lines))
            current_lines = []
            current_start = None
            current_end = None
            current_size = 0
        if current_start is None:
            current_start = row_index
        current_end = row_index
        current_lines.append(row_text)
        current_size += len(row_text)
    if current_lines:
        blocks.append(_format_text_block(sheet.name, current_start, current_end, current_lines))
    return blocks


def _format_text_block(sheet_name: str, row_start: int | None, row_end: int | None, lines: list[str]) -> dict:
    return {
        "sheet_name": sheet_name,
        "row_start": row_start or 0,
        "row_end": row_end or row_start or 0,
        "text": f"Sheet: {sheet_name}\nRows: {row_start}-{row_end}\n" + "\n".join(lines),
    }


def _merged_fill_set(merged_ranges: list[str]) -> set[tuple[int, int]]:
    """返回每个合并区【非左上】格的 (真实行号, 列号 1-based) 集合。

    parser 已把合并区扩展进网格(左上格的值结构化填到区内缺失格),所以这些格在
    raw_values 里是非空的。本集合用于在语义行里把它们标记为"结构化事实"
    (structural),而非前向继承(inferred)——并阻止合并值越过其区域前向泄漏。
    """
    fill: set[tuple[int, int]] = set()
    for ref in merged_ranges or []:
        parts = ref.split(":")
        if len(parts) != 2:
            continue
        top_left = _row_col_from_ref(parts[0])
        bottom_right = _row_col_from_ref(parts[1])
        if top_left is None or bottom_right is None:
            continue
        top_row, top_col_0 = top_left
        bottom_row, bottom_col_0 = bottom_right
        for row_index in range(top_row, bottom_row + 1):
            for col_0 in range(top_col_0, bottom_col_0 + 1):
                if row_index == top_row and col_0 == top_col_0:
                    continue  # 左上格是 authored,不计入结构化填充
                fill.add((row_index, col_0 + 1))  # 转为 1-based 列号,与 header_by_col 一致
    return fill


def _sheet_semantic_rows(sheet, profile: dict | None = None) -> list[dict]:
    profile = profile or _sheet_profile(sheet.rows)
    headers = profile.get("headers") or []
    if not headers:
        return []

    header_row_position = int(headers[0].get("row_index") or 0)
    header_by_col = {
        int(header["col_index"]): str(header.get("header") or "").strip()
        for header in headers
        if header.get("header")
    }
    if not header_by_col:
        return []

    merged_fill = _merged_fill_set(getattr(sheet, "merged_ranges", []) or [])

    semantic_rows = []
    context_values: dict[str, str] = {}
    context_sources: dict[str, int] = {}
    # context_structural[header]=True 表示当前上下文值来自合并单元格的结构化填充
    # (authoritative),不应越过其合并区域继续前向继承。
    context_structural: dict[str, bool] = {}
    section_title = ""

    for row_position, row in enumerate(sheet.rows, start=1):
        if row_position <= header_row_position:
            continue

        raw_text = _row_to_text(row)
        if not raw_text:
            context_values.clear()
            context_sources.clear()
            context_structural.clear()
            continue

        if _looks_like_header_repeat(row, header_by_col):
            context_values.clear()
            context_sources.clear()
            context_structural.clear()
            continue

        if _is_title_like_row(row):
            section_title = raw_text
            context_values.clear()
            context_sources.clear()
            context_structural.clear()
            continue

        row_index = _sheet_row_index(sheet, row_position)
        raw_values = _row_header_values(row, header_by_col)
        if not raw_values:
            continue

        values = {}
        inherited = {}
        structural_fill: dict[str, int] = {}
        for col_index in sorted(header_by_col):
            header = header_by_col[col_index]
            raw_value = raw_values.get(header, "")
            is_structural = (row_index, col_index) in merged_fill
            if raw_value:
                values[header] = raw_value
                context_values[header] = raw_value
                context_sources[header] = row_index
                context_structural[header] = is_structural
                if is_structural:
                    structural_fill[header] = row_index
            elif header in context_values and not context_structural.get(header):
                # 仅前向继承 authored 上下文;合并值不越过其区域。
                values[header] = context_values[header]
                inherited[header] = context_sources.get(header)

        if not values:
            continue

        semantic_text = _format_semantic_row_text(section_title, values)
        confidence = _semantic_row_confidence(
            raw_values=raw_values,
            inherited=inherited,
            header_count=len(header_by_col),
            section_title=section_title,
        )
        reasons = list(confidence["reasons"])
        if structural_fill:
            reasons.append("merged_cell_structural_fill")
        semantic_rows.append({
            "sheet_name": sheet.name,
            "row_index": row_index,
            "header_row_index": _sheet_row_index(sheet, header_row_position),
            "layer": "inference",
            "is_inferred": True,
            "inference_type": "forward_fill_context",
            "confidence": confidence["label"],
            "confidence_score": confidence["score"],
            "confidence_reasons": reasons,
            "evidence_policy": "Use raw_text/raw_values as facts; verify inherited fields with source.inherited_from_rows. Structural (merged-cell) values are authoritative, not inferred.",
            "section_title": section_title,
            "raw_text": raw_text,
            "semantic_text": semantic_text,
            "values": values,
            "raw_values": raw_values,
            "inherited": inherited,
            "source": {
                "row_index": row_index,
                "header_row_index": _sheet_row_index(sheet, header_row_position),
                "evidence_layer": "fact",
                "raw_row_text": raw_text,
                "raw_headers": sorted(raw_values),
                "inherited_headers": sorted(inherited),
                "inherited_from_rows": inherited,
                "structural_fill": dict(structural_fill),
            },
        })

    return semantic_rows


def _row_header_values(row: list[str], header_by_col: dict[int, str]) -> dict[str, str]:
    values = {}
    for col_index, header in header_by_col.items():
        value = ""
        if col_index - 1 < len(row):
            value = str(row[col_index - 1]).strip()
        if value:
            values[header] = value
    return values


def _looks_like_header_repeat(row: list[str], header_by_col: dict[int, str]) -> bool:
    expected = {header.strip() for header in header_by_col.values() if header.strip()}
    actual = {str(value).strip() for value in row if str(value).strip()}
    if len(actual) < 2:
        return False
    return len(actual & expected) >= max(2, min(len(actual), len(expected)) // 2)


def _format_semantic_row_text(section_title: str, values: dict[str, str]) -> str:
    parts = []
    if section_title:
        parts.append(f"Section: {section_title}")
    parts.extend(f"{header}: {value}" for header, value in values.items() if value)
    return "; ".join(parts)


def _semantic_row_confidence(
    raw_values: dict[str, str],
    inherited: dict[str, int | None],
    header_count: int,
    section_title: str,
) -> dict:
    raw_count = len(raw_values)
    inherited_count = len(inherited)
    coverage = raw_count / max(1, header_count)
    inherited_ratio = inherited_count / max(1, raw_count + inherited_count)
    score = 0.58 + min(coverage, 1.0) * 0.22
    reasons = [
        "header_row_detected",
        "same_contiguous_table_block",
        "raw_row_values_preserved",
    ]

    if inherited_count:
        score += 0.12
        reasons.append("blank_cells_forward_filled_from_previous_rows")
    else:
        score += 0.18
        reasons.append("no_inherited_values")

    if section_title:
        score += 0.03
        reasons.append("section_title_attached")

    if inherited_ratio > 0.6:
        score -= 0.18
        reasons.append("mostly_inherited_values")
    elif inherited_ratio > 0.35:
        score -= 0.08
        reasons.append("partially_inherited_values")

    score = round(max(0.0, min(score, 0.98)), 2)
    if score >= 0.82:
        label = "high"
    elif score >= 0.68:
        label = "medium"
    else:
        label = "low"
    return {"label": label, "score": score, "reasons": reasons}


def _semantic_confidence_counts(semantic_rows: list[dict]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for row in semantic_rows:
        label = row.get("confidence") or "low"
        if label not in counts:
            counts[label] = 0
        counts[label] += 1
    return counts


def _semantic_row_db_record(record_id: int, sheet_name: str, row: dict) -> tuple:
    return (
        record_id,
        sheet_name,
        int(row.get("row_index") or 0),
        int(row.get("header_row_index") or 0),
        str(row.get("semantic_text") or ""),
        str(row.get("raw_text") or ""),
        json.dumps(row.get("values") or {}, ensure_ascii=False),
        json.dumps(row.get("raw_values") or {}, ensure_ascii=False),
        json.dumps(row.get("inherited") or {}, ensure_ascii=False),
        str(row.get("confidence") or ""),
        float(row.get("confidence_score") or 0.0),
        json.dumps(row.get("confidence_reasons") or [], ensure_ascii=False),
        json.dumps(row.get("source") or {}, ensure_ascii=False),
    )


def _sheet_row_index(sheet, one_based_position: int) -> int:
    if one_based_position <= len(getattr(sheet, "row_indices", [])):
        return sheet.row_indices[one_based_position - 1]
    return one_based_position


def _legacy_department_kb_index_dir(department_id: str, kb_name: str) -> str:
    department_part = _safe_scope_part(department_id or "unknown")
    kb_part = validate_kb_name(kb_name)
    root = safe_child_path(src.settings.STORAGE_DIR, "excel_indexes", "departments")
    return safe_child_path(root, department_part, "kbs", kb_part)


def _legacy_department_kb_index_path(department_id: str, kb_name: str) -> str:
    return safe_child_path(_legacy_department_kb_index_dir(department_id, kb_name), "kb_excel_index.json")


def _safe_scope_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:80] or "unknown"


def _document_index_id(record_id: int, content_hash: str) -> str:
    suffix = (content_hash or hashlib.sha256(str(record_id).encode("utf-8")).hexdigest())[:12]
    return f"doc_{record_id}_{suffix}"


def _delete_legacy_index_files_for_document(department_id: str, kb_name: str, record_id: int, content_hash: str):
    kb_dir = _legacy_department_kb_index_dir(department_id, kb_name)
    kb_index_path = _legacy_department_kb_index_path(department_id, kb_name)
    document_id = _document_index_id(record_id, content_hash)
    document_dir = safe_child_path(kb_dir, "documents", document_id)
    if os.path.isdir(document_dir):
        for root, dirs, files in os.walk(document_dir, topdown=False):
            for filename in files:
                try:
                    os.remove(os.path.join(root, filename))
                except OSError:
                    pass
            for dirname in dirs:
                try:
                    os.rmdir(os.path.join(root, dirname))
                except OSError:
                    pass
        try:
            os.rmdir(document_dir)
        except OSError:
            pass

    if not os.path.exists(kb_index_path):
        return
    try:
        with open(kb_index_path, "r", encoding="utf-8") as file_obj:
            kb_index = json.load(file_obj)
    except (OSError, json.JSONDecodeError):
        return
    kb_index["documents"] = [
        doc for doc in kb_index.get("documents", [])
        if doc.get("record_id") != record_id
    ]
    with open(kb_index_path, "w", encoding="utf-8") as file_obj:
        json.dump(kb_index, file_obj, ensure_ascii=False, indent=2)


def _json_loads(value: str, default):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return default
