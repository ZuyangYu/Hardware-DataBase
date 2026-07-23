import json
import os
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field

import config.settings
from src.services.document_routing import PROCESSOR_KIND_SPREADSHEET, TABLE_STATUS_ARCHIVED, TABLE_STATUS_PROCESSING
from src.pipelines.document_rag.schemas import TASK_STATUS_DEAD_LETTER, TASK_STATUS_QUEUED

WORKER_STALE_SECONDS = 30 * 60
WORKER_MAX_RETRIES = 3
TERMINAL_PARSE_STATUSES = {"parsed", "failed", "deleted", "indexed"}


def _require_department_id(department_id: str | int | None, action: str) -> str:
    if department_id in (None, ""):
        raise ValueError(f"department_id is required for scoped pipeline document {action}")
    return str(department_id)


@dataclass
class PipelineDocumentRecord:
    id: int
    kb_name: str
    document_name: str
    original_file_name: str
    dataset_kind: str
    dataset_id: str
    document_id: str
    source_group: str
    department_id: str
    uploaded_by: str
    status: str
    kb_id: int = 0
    content_kind: str = "document_text"
    processor_kind: str = "ragflow"
    local_path: str = ""
    file_size: int = 0
    content_hash: str = ""
    upload_status: str = ""
    error_message: str = ""
    ragflow_error: str = ""
    last_status_checked_at: str = ""
    parse_started_at: str = ""
    parse_completed_at: str = ""
    parse_progress: int = 0
    parse_stage: str = ""
    worker_id: str = ""
    worker_started_at: str = ""
    worker_heartbeat_at: str = ""
    retry_count: int = 0
    # Additive P1 scope metadata. The normalized ProjectStore remains the
    # business source of truth; this keeps the existing pipeline catalog able
    # to pre-filter by the same identity during the migration.
    asset_id: str = ""
    logical_document_id: str = ""
    source_version_id: str = ""
    project_id: str = ""
    document_role: str = ""
    module_scope: list[str] = field(default_factory=list)
    revision: str = ""
    approval_status: str = ""
    effective_from: str = ""
    effective_to: str = ""
    usage_type: str = ""
    created_at: str = ""
    updated_at: str = ""


class PipelineDocumentStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.path.join(config.settings.STORAGE_DIR, "pipeline_documents.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_datasets (
                    kind TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    dataset_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_id INTEGER NOT NULL DEFAULT 0,
                    kb_name TEXT NOT NULL,
                    document_name TEXT NOT NULL,
                    original_file_name TEXT NOT NULL DEFAULT '',
                    dataset_kind TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_group TEXT NOT NULL DEFAULT '',
                    department_id TEXT NOT NULL DEFAULT '',
                    uploaded_by TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'uploaded',
                    content_kind TEXT NOT NULL DEFAULT 'document_text',
                    processor_kind TEXT NOT NULL DEFAULT 'ragflow',
                    local_path TEXT NOT NULL DEFAULT '',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL DEFAULT '',
                    upload_status TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    ragflow_error TEXT NOT NULL DEFAULT '',
                    last_status_checked_at TEXT NOT NULL DEFAULT '',
                    parse_started_at TEXT NOT NULL DEFAULT '',
                    parse_completed_at TEXT NOT NULL DEFAULT '',
                    parse_progress INTEGER NOT NULL DEFAULT 0,
                    parse_stage TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    worker_started_at TEXT NOT NULL DEFAULT '',
                    worker_heartbeat_at TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(kb_name, department_id, document_name, dataset_kind)
                )
            """)
            self._migrate_department_unique_constraint(conn)
            self._ensure_columns(conn)

    def _ensure_columns(self, conn):
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(pipeline_documents)").fetchall()
        }
        add_column = "ALTER TABLE pipeline_documents ADD COLUMN"
        migrations = {
            "original_file_name": f"{add_column} original_file_name TEXT NOT NULL DEFAULT ''",
            "kb_id": f"{add_column} kb_id INTEGER NOT NULL DEFAULT 0",
            "local_path": f"{add_column} local_path TEXT NOT NULL DEFAULT ''",
            "file_size": f"{add_column} file_size INTEGER NOT NULL DEFAULT 0",
            "content_hash": f"{add_column} content_hash TEXT NOT NULL DEFAULT ''",
            "upload_status": f"{add_column} upload_status TEXT NOT NULL DEFAULT ''",
            "error_message": f"{add_column} error_message TEXT NOT NULL DEFAULT ''",
            "ragflow_error": f"{add_column} ragflow_error TEXT NOT NULL DEFAULT ''",
            "last_status_checked_at": f"{add_column} last_status_checked_at TEXT NOT NULL DEFAULT ''",
            "parse_started_at": f"{add_column} parse_started_at TEXT NOT NULL DEFAULT ''",
            "parse_completed_at": f"{add_column} parse_completed_at TEXT NOT NULL DEFAULT ''",
            "parse_progress": f"{add_column} parse_progress INTEGER NOT NULL DEFAULT 0",
            "parse_stage": f"{add_column} parse_stage TEXT NOT NULL DEFAULT ''",
            "worker_id": f"{add_column} worker_id TEXT NOT NULL DEFAULT ''",
            "worker_started_at": f"{add_column} worker_started_at TEXT NOT NULL DEFAULT ''",
            "worker_heartbeat_at": f"{add_column} worker_heartbeat_at TEXT NOT NULL DEFAULT ''",
            "retry_count": f"{add_column} retry_count INTEGER NOT NULL DEFAULT 0",
            "content_kind": f"{add_column} content_kind TEXT NOT NULL DEFAULT 'document_text'",
            "processor_kind": f"{add_column} processor_kind TEXT NOT NULL DEFAULT 'ragflow'",
            "asset_id": f"{add_column} asset_id TEXT NOT NULL DEFAULT ''",
            "logical_document_id": f"{add_column} logical_document_id TEXT NOT NULL DEFAULT ''",
            "source_version_id": f"{add_column} source_version_id TEXT NOT NULL DEFAULT ''",
            "project_id": f"{add_column} project_id TEXT NOT NULL DEFAULT ''",
            "document_role": f"{add_column} document_role TEXT NOT NULL DEFAULT ''",
            "module_scope_json": f"{add_column} module_scope_json TEXT NOT NULL DEFAULT '[]'",
            "revision": f"{add_column} revision TEXT NOT NULL DEFAULT ''",
            "approval_status": f"{add_column} approval_status TEXT NOT NULL DEFAULT ''",
            "effective_from": f"{add_column} effective_from TEXT NOT NULL DEFAULT ''",
            "effective_to": f"{add_column} effective_to TEXT NOT NULL DEFAULT ''",
            "usage_type": f"{add_column} usage_type TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)

    @staticmethod
    def _to_record(row) -> PipelineDocumentRecord:
        values = dict(row)
        raw_scope = values.pop("module_scope_json", "[]")
        try:
            scope = json.loads(raw_scope or "[]")
        except (TypeError, json.JSONDecodeError):
            scope = []
        values["module_scope"] = [str(item) for item in scope] if isinstance(scope, list) else []
        return PipelineDocumentRecord(**values)

    def _migrate_department_unique_constraint(self, conn):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pipeline_documents'"
        ).fetchone()
        table_sql = row["sql"] if row else ""
        if "UNIQUE(kb_name, document_name, dataset_kind)" not in table_sql:
            return

        conn.execute("ALTER TABLE pipeline_documents RENAME TO pipeline_documents_old")
        conn.execute("""
            CREATE TABLE pipeline_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kb_id INTEGER NOT NULL DEFAULT 0,
                kb_name TEXT NOT NULL,
                document_name TEXT NOT NULL,
                original_file_name TEXT NOT NULL DEFAULT '',
                dataset_kind TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                source_group TEXT NOT NULL DEFAULT '',
                department_id TEXT NOT NULL DEFAULT '',
                uploaded_by TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'uploaded',
                content_kind TEXT NOT NULL DEFAULT 'document_text',
                processor_kind TEXT NOT NULL DEFAULT 'ragflow',
                local_path TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT NOT NULL DEFAULT '',
                upload_status TEXT NOT NULL DEFAULT '',
                ragflow_error TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                last_status_checked_at TEXT NOT NULL DEFAULT '',
                parse_started_at TEXT NOT NULL DEFAULT '',
                parse_completed_at TEXT NOT NULL DEFAULT '',
                parse_progress INTEGER NOT NULL DEFAULT 0,
                parse_stage TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                worker_started_at TEXT NOT NULL DEFAULT '',
                worker_heartbeat_at TEXT NOT NULL DEFAULT '',
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(kb_name, department_id, document_name, dataset_kind)
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO pipeline_documents (
                id, kb_id, kb_name, document_name, original_file_name, dataset_kind,
                dataset_id, document_id, source_group, department_id, uploaded_by,
                status, content_kind, processor_kind, local_path, file_size,
                content_hash, upload_status, error_message, ragflow_error, last_status_checked_at,
                parse_started_at, parse_completed_at, parse_progress, parse_stage,
                worker_id, worker_started_at, worker_heartbeat_at, retry_count,
                created_at, updated_at
            )
            SELECT
                id, 0, kb_name, document_name, original_file_name, dataset_kind,
                dataset_id, document_id, source_group, department_id, uploaded_by,
                status, 'document_text', 'ragflow', local_path, file_size,
                content_hash, upload_status, ragflow_error, ragflow_error, last_status_checked_at,
                parse_started_at, parse_completed_at, 0, '', '', '', '', 0,
                created_at, updated_at
            FROM pipeline_documents_old
        """)
        conn.execute("DROP TABLE pipeline_documents_old")

    def get_dataset(self, kind: str) -> tuple[str, str] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT dataset_id, dataset_name FROM pipeline_datasets WHERE kind = ?",
                (kind,),
            ).fetchone()
        if not row:
            return None
        return row["dataset_id"], row["dataset_name"]

    def get_dataset_id(self, kind: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT dataset_id FROM pipeline_datasets WHERE kind = ?", (kind,)).fetchone()
        return row["dataset_id"] if row else None

    def save_dataset(self, kind: str, dataset_id: str, dataset_name: str):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO pipeline_datasets (kind, dataset_id, dataset_name, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(kind) DO UPDATE SET
                    dataset_id = excluded.dataset_id,
                    dataset_name = excluded.dataset_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (kind, dataset_id, dataset_name),
            )

    def upsert_document(
        self,
        kb_name: str,
        document_name: str,
        dataset_kind: str,
        dataset_id: str,
        document_id: str,
        source_group: str,
        department_id: str,
        uploaded_by: str,
        kb_id: int | None = None,
        status: str = "uploaded",
        original_file_name: str = "",
        local_path: str = "",
        file_size: int = 0,
        content_hash: str = "",
        upload_status: str = "",
        error_message: str = "",
        ragflow_error: str = "",
        content_kind: str = "document_text",
        processor_kind: str = "ragflow",
        parse_progress: int = 0,
        parse_stage: str = "",
        asset_id: str = "",
        logical_document_id: str = "",
        source_version_id: str = "",
        project_id: str = "",
        document_role: str = "",
        module_scope: list[str] | None = None,
        revision: str = "",
        approval_status: str = "",
        effective_from: str = "",
        effective_to: str = "",
        usage_type: str = "",
    ):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO pipeline_documents (
                    kb_id, kb_name, document_name, original_file_name, dataset_kind,
                    dataset_id, document_id, source_group, department_id,
                    uploaded_by, status, content_kind, processor_kind,
                    local_path, file_size, content_hash, upload_status,
                    error_message, ragflow_error, parse_progress, parse_stage,
                    asset_id, logical_document_id, source_version_id, project_id, document_role,
                    module_scope_json, revision, approval_status, effective_from, effective_to, usage_type,
                    parse_started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(kb_name, department_id, document_name, dataset_kind) DO UPDATE SET
                    kb_id = excluded.kb_id,
                    department_id = excluded.department_id,
                    original_file_name = excluded.original_file_name,
                    dataset_id = excluded.dataset_id,
                    document_id = excluded.document_id,
                    source_group = excluded.source_group,
                    uploaded_by = excluded.uploaded_by,
                    status = excluded.status,
                    content_kind = excluded.content_kind,
                    processor_kind = excluded.processor_kind,
                    local_path = excluded.local_path,
                    file_size = excluded.file_size,
                    content_hash = excluded.content_hash,
                    upload_status = excluded.upload_status,
                    error_message = excluded.error_message,
                    ragflow_error = excluded.ragflow_error,
                    parse_progress = excluded.parse_progress,
                    parse_stage = excluded.parse_stage,
                    asset_id = excluded.asset_id,
                    logical_document_id = excluded.logical_document_id,
                    source_version_id = excluded.source_version_id,
                    project_id = excluded.project_id,
                    document_role = excluded.document_role,
                    module_scope_json = excluded.module_scope_json,
                    revision = excluded.revision,
                    approval_status = excluded.approval_status,
                    effective_from = excluded.effective_from,
                    effective_to = excluded.effective_to,
                    usage_type = excluded.usage_type,
                    worker_id = '',
                    worker_started_at = '',
                    worker_heartbeat_at = '',
                    parse_started_at = excluded.parse_started_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    int(kb_id or 0),
                    kb_name,
                    document_name,
                    original_file_name or document_name,
                    dataset_kind,
                    dataset_id,
                    document_id,
                    source_group,
                    department_id,
                    uploaded_by,
                    status,
                    content_kind,
                    processor_kind,
                    local_path,
                    file_size,
                    content_hash,
                    upload_status,
                    error_message or ragflow_error,
                    ragflow_error,
                    max(0, min(100, int(parse_progress or 0))),
                    parse_stage,
                    asset_id,
                    logical_document_id,
                    source_version_id,
                    project_id,
                    document_role,
                    json.dumps(sorted(set(module_scope or [])), ensure_ascii=False),
                    revision,
                    approval_status,
                    effective_from,
                    effective_to,
                    usage_type,
                ),
            )

    def list_documents(self, kb_name: str, department_id: str | int | None = None) -> list[PipelineDocumentRecord]:
        department_id = _require_department_id(department_id, "listing")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ? AND department_id = ?
                ORDER BY document_name
                """,
                (kb_name, department_id),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def list_documents_unscoped(self, kb_name: str) -> list[PipelineDocumentRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ?
                ORDER BY department_id, document_name
                """,
                (kb_name,),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def document_stats_by_kb(self, department_id: str | int | None = None) -> dict[str, dict[str, int]]:
        """按知识库聚合文档/解析状态统计，供治理视图使用。"""
        department_id = _require_department_id(department_id, "statistics")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT
                    kb_name,
                    COUNT(*) AS files,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'parsing' THEN 1 ELSE 0 END) AS parsing
                FROM pipeline_documents
                WHERE department_id = ?
                GROUP BY kb_name
                """,
                (department_id,),
            ).fetchall()
        return {
            row["kb_name"]: {
                "files": int(row["files"] or 0),
                "failed": int(row["failed"] or 0),
                "parsing": int(row["parsing"] or 0),
            }
            for row in rows
        }

    def document_stats_by_kb_identity(self) -> dict[str, dict[str, int]]:
        """Aggregate document stats by stable KB identity for system governance.

        Normal product paths must keep using scoped methods. This explicit
        maintenance view is intentionally keyed by kb_id when available so
        same-name knowledge bases in different departments do not collapse into
        one row.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT
                    kb_id,
                    department_id,
                    kb_name,
                    COUNT(*) AS files,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'parsing' THEN 1 ELSE 0 END) AS parsing
                FROM pipeline_documents
                GROUP BY kb_id, department_id, kb_name
                """
            ).fetchall()
        stats: dict[str, dict[str, int]] = {}
        for row in rows:
            kb_id = int(row["kb_id"] or 0)
            department_id = str(row["department_id"] or "")
            kb_name = str(row["kb_name"] or "")
            key = f"kb_id:{kb_id}" if kb_id else f"department:{department_id}:kb:{kb_name}"
            stats[key] = {
                "files": int(row["files"] or 0),
                "failed": int(row["failed"] or 0),
                "parsing": int(row["parsing"] or 0),
            }
        return stats

    def get_document_by_id(self, record_id: int) -> PipelineDocumentRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_documents WHERE id = ?",
                (record_id,),
            ).fetchone()
        return self._to_record(row) if row else None

    def get_document_by_id_scoped(self, record_id: int, department_id: str | int | None) -> PipelineDocumentRecord | None:
        department_id = _require_department_id(department_id, "id lookup")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_documents WHERE id = ? AND department_id = ?",
                (record_id, department_id),
            ).fetchone()
        return self._to_record(row) if row else None

    def get_document(
        self,
        kb_name: str,
        document_name: str,
        dataset_kind: str | None = None,
        department_id: str | int | None = None,
    ) -> PipelineDocumentRecord | None:
        department_id = _require_department_id(department_id, "lookup")
        if dataset_kind:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    """
                    SELECT * FROM pipeline_documents
                    WHERE kb_name = ? AND document_name = ? AND dataset_kind = ? AND department_id = ?
                    """,
                    (kb_name, document_name, dataset_kind, department_id),
                ).fetchone()
            return self._to_record(row) if row else None

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ? AND document_name = ? AND department_id = ?
                """,
                (kb_name, document_name, department_id),
            ).fetchone()
        return self._to_record(row) if row else None

    def get_document_unscoped(
        self,
        kb_name: str,
        document_name: str,
        dataset_kind: str | None = None,
    ) -> PipelineDocumentRecord | None:
        params: list[object] = [kb_name, document_name]
        dataset_clause = ""
        if dataset_kind:
            dataset_clause = " AND dataset_kind = ?"
            params.append(dataset_kind)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM pipeline_documents
                WHERE kb_name = ? AND document_name = ?{dataset_clause}
                """,
                params,
            ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Ambiguous pipeline document name in {kb_name}: {document_name}")
        return self._to_record(rows[0]) if rows else None

    def find_by_hash(
        self,
        kb_name: str,
        dataset_kind: str,
        content_hash: str,
        department_id: str | int | None,
    ) -> PipelineDocumentRecord | None:
        department_id = _require_department_id(department_id, "hash lookup")
        if not content_hash:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ? AND dataset_kind = ? AND content_hash = ? AND department_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (kb_name, dataset_kind, content_hash, department_id),
            ).fetchone()
        return self._to_record(row) if row else None

    def delete_document(
        self,
        kb_name: str,
        document_name: str,
        dataset_kind: str | None = None,
        department_id: str | int | None = None,
    ):
        department_id = _require_department_id(department_id, "deletion")
        if dataset_kind:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    DELETE FROM pipeline_documents
                    WHERE kb_name = ? AND document_name = ? AND dataset_kind = ? AND department_id = ?
                    """,
                    (kb_name, document_name, dataset_kind, department_id),
                )
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM pipeline_documents WHERE kb_name = ? AND document_name = ? AND department_id = ?",
                (kb_name, document_name, department_id),
            )

    def delete_document_by_id(self, record_id: int):
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM pipeline_documents WHERE id = ?", (record_id,))

    def delete_document_by_id_scoped(self, record_id: int, department_id: str | int | None):
        department_id = _require_department_id(department_id, "id deletion")
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM pipeline_documents WHERE id = ? AND department_id = ?",
                (record_id, department_id),
            )

    def delete_document_by_remote_id(self, dataset_id: str, document_id: str):
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM pipeline_documents WHERE dataset_id = ? AND document_id = ?",
                (dataset_id, document_id),
            )

    def delete_documents_by_kb(self, kb_name: str, department_id: str):
        if department_id in (None, ""):
            raise ValueError("department_id is required for scoped knowledge-base document deletion")
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM pipeline_documents WHERE kb_name = ? AND department_id = ?",
                (kb_name, department_id),
            )

    def delete_documents_by_kb_unscoped(self, kb_name: str):
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM pipeline_documents WHERE kb_name = ?", (kb_name,))

    def update_document_status(self, dataset_id: str, document_id: str, status: str, error_message: str = ""):
        completed_expr = "CURRENT_TIMESTAMP" if status in TERMINAL_PARSE_STATUSES else "parse_completed_at"
        with closing(self._connect()) as conn:
            conn.execute(
                f"""
                UPDATE pipeline_documents
                SET status = ?,
                    upload_status = ?,
                    error_message = ?,
                    ragflow_error = ?,
                    last_status_checked_at = CURRENT_TIMESTAMP,
                    parse_completed_at = {completed_expr},
                    updated_at = CURRENT_TIMESTAMP
                WHERE dataset_id = ? AND document_id = ?
                """,
                (status, status, error_message, error_message, dataset_id, document_id),
            )

    def update_document_status_by_id(self, record_id: int, status: str, error_message: str = ""):
        completed_expr = "CURRENT_TIMESTAMP" if status in TERMINAL_PARSE_STATUSES else "parse_completed_at"
        with closing(self._connect()) as conn:
            conn.execute(
                f"""
                UPDATE pipeline_documents
                SET status = ?,
                    upload_status = ?,
                    error_message = ?,
                    ragflow_error = ?,
                    last_status_checked_at = CURRENT_TIMESTAMP,
                    parse_completed_at = {completed_expr},
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, status, error_message, error_message, record_id),
            )

    def update_document_progress_by_id(
        self,
        record_id: int,
        progress: int,
        stage: str,
        status: str | None = None,
        error_message: str | None = None,
    ):
        progress = max(0, min(100, int(progress or 0)))
        assignments = [
            "parse_progress = ?",
            "parse_stage = ?",
            "worker_heartbeat_at = CURRENT_TIMESTAMP",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        params: list[object] = [progress, stage]
        if status is not None:
            assignments.extend(["status = ?", "upload_status = ?"])
            params.extend([status, status])
        if error_message is not None:
            assignments.extend(["error_message = ?", "ragflow_error = ?"])
            params.extend([error_message, error_message])
        if status in TERMINAL_PARSE_STATUSES:
            assignments.append("parse_completed_at = CURRENT_TIMESTAMP")
        params.append(record_id)
        with closing(self._connect()) as conn:
            conn.execute(
                f"""
                UPDATE pipeline_documents
                SET {', '.join(assignments)}
                WHERE id = ?
                """,
                params,
            )

    def claim_next_parse_record(
        self,
        worker_id: str,
        processor_kinds: Sequence[str] | None = None,
        stale_after_seconds: int = WORKER_STALE_SECONDS,
        max_retries: int = WORKER_MAX_RETRIES,
    ) -> PipelineDocumentRecord | None:
        processor_kinds = tuple(processor_kinds or (PROCESSOR_KIND_SPREADSHEET,))
        if not processor_kinds:
            return None
        processor_placeholders = ", ".join("?" for _ in processor_kinds)
        stale_after_seconds = max(60, int(stale_after_seconds or WORKER_STALE_SECONDS))
        max_retries = max(1, int(max_retries or WORKER_MAX_RETRIES))
        with closing(self._connect()) as conn:
            conn.execute(
                f"""
                UPDATE pipeline_documents
                SET worker_id = '',
                    worker_started_at = '',
                    worker_heartbeat_at = '',
                    status = ?,
                    upload_status = ?,
                    parse_stage = 'Worker timeout; queued for retry',
                    updated_at = CURRENT_TIMESTAMP
                WHERE processor_kind IN ({processor_placeholders})
                  AND worker_id != ''
                  AND worker_heartbeat_at != ''
                  AND datetime(worker_heartbeat_at) < datetime('now', ?)
                """,
                (TASK_STATUS_QUEUED, TASK_STATUS_QUEUED, *processor_kinds, f"-{stale_after_seconds} seconds"),
            )
            conn.execute(
                f"""
                UPDATE pipeline_documents
                SET status = ?,
                    upload_status = ?,
                    worker_id = '',
                    worker_started_at = '',
                    worker_heartbeat_at = '',
                    error_message = 'Exceeded maximum background parse retries',
                    ragflow_error = 'Exceeded maximum background parse retries',
                    parse_stage = 'Exceeded maximum background parse retries',
                    parse_completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE processor_kind IN ({processor_placeholders})
                  AND status IN ('{TABLE_STATUS_ARCHIVED}', '{TASK_STATUS_QUEUED}')
                  AND worker_id = ''
                  AND retry_count >= ?
                """,
                (TASK_STATUS_DEAD_LETTER, TASK_STATUS_DEAD_LETTER, *processor_kinds, max_retries),
            )
            row = conn.execute(
                f"""
                SELECT * FROM pipeline_documents
                WHERE processor_kind IN ({processor_placeholders})
                  AND status IN ('{TABLE_STATUS_ARCHIVED}', '{TASK_STATUS_QUEUED}')
                  AND worker_id = ''
                  AND retry_count < ?
                ORDER BY updated_at, id
                LIMIT 1
                """,
                (*processor_kinds, max_retries),
            ).fetchone()
            if row is None:
                return None
            record_id = int(row["id"])
            conn.execute(
                f"""
                UPDATE pipeline_documents
                SET worker_id = ?,
                    worker_started_at = CURRENT_TIMESTAMP,
                    worker_heartbeat_at = CURRENT_TIMESTAMP,
                    retry_count = retry_count + 1,
                    status = '{TABLE_STATUS_PROCESSING}',
                    upload_status = '{TABLE_STATUS_PROCESSING}',
                    parse_progress = CASE WHEN parse_progress < 10 THEN 10 ELSE parse_progress END,
                    parse_stage = '准备解析 Excel',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND worker_id = ''
                """,
                (worker_id, record_id),
            )
            claimed = conn.execute(
                "SELECT * FROM pipeline_documents WHERE id = ? AND worker_id = ?",
                (record_id, worker_id),
            ).fetchone()
        return self._to_record(claimed) if claimed else None

    def release_parse_claim(self, record_id: int):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE pipeline_documents
                SET worker_id = '',
                    worker_started_at = '',
                    worker_heartbeat_at = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (record_id,),
            )

    def mark_document_failed_by_id(self, record_id: int, message: str):
        self.update_document_progress_by_id(
            record_id,
            100,
            "解析失败",
            status="failed",
            error_message=message,
        )
        self.release_parse_claim(record_id)
