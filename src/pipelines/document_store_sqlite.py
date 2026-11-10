import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from contextlib import closing
from typing import Any
from dataclasses import dataclass, field

import src.settings
from src.services.document_routing import PROCESSOR_KIND_SPREADSHEET, TABLE_STATUS_ARCHIVED, TABLE_STATUS_PROCESSING
from src.pipelines.document_rag.schemas import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DEAD_LETTER,
    TASK_STATUS_QUEUED,
    normalize_parse_status,
)


def _fire_shadow_asset_hook(record_id: int | None) -> None:
    """解析首次到达 completed 时为文件立文档资产影子档(fail-soft, 不阻断主流程)。"""
    if record_id is None:
        return
    try:
        from src.core.doc_assets import ensure_shadow_asset_for_record

        ensure_shadow_asset_for_record(int(record_id))
    except Exception:  # noqa: BLE001
        pass

WORKER_STALE_SECONDS = 30 * 60
WORKER_MAX_RETRIES = 3
DIRECTORY_IMPORT_LEASE_SECONDS = max(
    60, int(os.getenv("HDB_DIRECTORY_IMPORT_LEASE_SECONDS", "1800"))
)
TERMINAL_PARSE_STATUSES = {"parsed", "failed", "deleted", "indexed", "degraded"}


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
    shared_read_only: bool = False
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
    # Additive P1 scope metadata. This keeps the existing pipeline catalog
    # able to pre-filter by the same identity during the migration.
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
    # P1-1: embedded child documents carry the depth of the extraction and the
    # JSON list of parents that reference them, so shared children are deleted
    # only after the last parent reference goes away.
    embed_depth: int = 0
    parent_document: str = ""
    created_at: str = ""
    updated_at: str = ""
    directory_id: int = 0


class PipelineDocumentStore:
    def __init__(self, db_path: str | None = None):
        from src.storage import domain_pg

        self._backend, self._dsn = domain_pg.resolve_domain_backend("PIPELINE_DOCUMENT")
        if db_path:
            # 显式 db_path（测试/工具）永远走 SQLite。
            self._backend = "sqlite"
        if self._backend == "sqlite":
            self.db_path = db_path or os.path.join(src.settings.STORAGE_DIR, "pipeline_documents.db")
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._init_db()
            return
        self.db_path = None
        domain_pg.ensure_domain_ready(
            dsn=self._dsn,
            store_cls=type(self),
            sqlite_db_path=os.path.join(src.settings.STORAGE_DIR, "pipeline_documents.db"),
            primary_table="pipeline_documents",
            migration_command=(
                "uv run python scripts/production_hardening/migrate_sqlite_to_postgres.py "
                "--group pipeline --dsn $CONTROL_PLANE_DATABASE_URL"
            ),
        )

    def _connect(self):
        if self._backend == "postgres":
            from src.storage.domain_pg import open_pg_connection

            return open_pg_connection(self._dsn)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
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
                CREATE TABLE IF NOT EXISTS pipeline_document_profiles (
                    record_id INTEGER PRIMARY KEY,
                    kb_name TEXT NOT NULL DEFAULT '',
                    department_id TEXT NOT NULL DEFAULT '',
                    remote_document_id TEXT NOT NULL DEFAULT '',
                    parse_status TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    source_version_id TEXT NOT NULL DEFAULT '',
                    revision TEXT NOT NULL DEFAULT '',
                    mpn_values TEXT NOT NULL DEFAULT '[]',
                    manufacturer TEXT NOT NULL DEFAULT '',
                    identity_origin TEXT NOT NULL DEFAULT '{}',
                    confirmed_at TEXT DEFAULT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_profile_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER,
                    event_kind TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT DEFAULT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_directories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_id INTEGER NOT NULL DEFAULT 0,
                    kb_name TEXT NOT NULL,
                    department_id TEXT NOT NULL,
                    parent_id INTEGER NOT NULL DEFAULT 0,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    depth INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(kb_name, department_id, parent_id, normalized_name)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_pipeline_directories_children "
                "ON pipeline_directories(kb_name, department_id, parent_id, name)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS directory_import_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_id INTEGER NOT NULL DEFAULT 0,
                    kb_name TEXT NOT NULL,
                    department_id TEXT NOT NULL,
                    directory_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    total_files INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS directory_import_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    source_group TEXT NOT NULL DEFAULT '',
                    directory_id INTEGER NOT NULL DEFAULT 0,
                    original_name TEXT NOT NULL DEFAULT '',
                    staged_path TEXT NOT NULL DEFAULT '',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    record_id INTEGER,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT NOT NULL DEFAULT '',
                    worker_started_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(job_id, relative_path)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_directory_import_jobs_scope "
                "ON directory_import_jobs(kb_name, department_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_directory_import_files_claim "
                "ON directory_import_files(status, worker_id, updated_at, id)"
            )
            self._create_pipeline_documents_table(conn)
            self._migrate_document_schema(conn)
            self._ensure_columns(conn)
            # Claim/sweep paths filter on processor_kind/status/worker_id and
            # order by updated_at; without these the worker full-scans every
            # 0.5s poll.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_pipeline_documents_claim"
                " ON pipeline_documents(processor_kind, status, worker_id, retry_count, updated_at, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_pipeline_documents_lease"
                " ON pipeline_documents(processor_kind, worker_id, worker_heartbeat_at)"
            )

    @staticmethod
    def _create_pipeline_documents_table(conn, *, include_unique: bool = True) -> None:
        """Create the complete current ledger shape.

        Keep this definition complete rather than relying on ``_ensure_columns``
        for normal boots.  The same shape is used by the SQLite-to-PostgreSQL
        domain template, and migrations can copy every shared column without
        silently dropping the asset/revision/embedding fields added over time.
        """
        unique_clause = (
            ",\n                    UNIQUE(kb_name, department_id, directory_id, document_name)"
            if include_unique
            else ""
        )
        conn.execute(
            f"""
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
                directory_id INTEGER NOT NULL DEFAULT 0,
                content_kind TEXT NOT NULL DEFAULT 'document_text',
                processor_kind TEXT NOT NULL DEFAULT 'ragflow',
                shared_read_only INTEGER NOT NULL DEFAULT 0,
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
                asset_id TEXT NOT NULL DEFAULT '',
                logical_document_id TEXT NOT NULL DEFAULT '',
                source_version_id TEXT NOT NULL DEFAULT '',
                project_id TEXT NOT NULL DEFAULT '',
                document_role TEXT NOT NULL DEFAULT '',
                module_scope_json TEXT NOT NULL DEFAULT '[]',
                revision TEXT NOT NULL DEFAULT '',
                approval_status TEXT NOT NULL DEFAULT '',
                effective_from TEXT NOT NULL DEFAULT '',
                effective_to TEXT NOT NULL DEFAULT '',
                usage_type TEXT NOT NULL DEFAULT '',
                embed_depth INTEGER NOT NULL DEFAULT 0,
                parent_document TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                {unique_clause}
            )
            """
        )

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
            "directory_id": f"{add_column} directory_id INTEGER NOT NULL DEFAULT 0",
            "content_kind": f"{add_column} content_kind TEXT NOT NULL DEFAULT 'document_text'",
            "processor_kind": f"{add_column} processor_kind TEXT NOT NULL DEFAULT 'ragflow'",
            "shared_read_only": f"{add_column} shared_read_only INTEGER NOT NULL DEFAULT 0",
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
            "embed_depth": f"{add_column} embed_depth INTEGER NOT NULL DEFAULT 0",
            "parent_document": f"{add_column} parent_document TEXT NOT NULL DEFAULT ''",
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
        values["shared_read_only"] = bool(values.get("shared_read_only", 0))
        return PipelineDocumentRecord(**values)

    def _migrate_document_schema(self, conn):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pipeline_documents'"
        ).fetchone()
        table_sql = row["sql"] if row else ""
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(pipeline_documents)").fetchall()
        }
        normalized_sql = " ".join(str(table_sql).upper().split())
        index_names = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(pipeline_documents)").fetchall()
        }
        if (
            "directory_id" in columns
            and "UNIQUE(KB_NAME, DEPARTMENT_ID, DIRECTORY_ID, DOCUMENT_NAME)" in normalized_sql
        ) or "ix_pipeline_documents_path_identity" in index_names:
            return

        old_columns = columns
        duplicate_rows = conn.execute(
            """
            SELECT kb_name, department_id,
                   COALESCE(directory_id, 0) AS directory_id,
                   document_name, COUNT(*) AS count
            FROM pipeline_documents
            GROUP BY kb_name, department_id, COALESCE(directory_id, 0), document_name
            HAVING COUNT(*) > 1
            LIMIT 1
            """
            if "directory_id" in columns
            else """
            SELECT kb_name, department_id, 0 AS directory_id,
                   document_name, COUNT(*) AS count
            FROM pipeline_documents
            GROUP BY kb_name, department_id, document_name
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        # Very old installations allowed the same path in different dataset
        # partitions.  Never discard one of those rows during migration.  The
        # clean shape gets a table UNIQUE constraint; a legacy-duplicate shape
        # keeps all rows and uses the application-level path check until an
        # operator resolves the historical ambiguity.
        include_unique = duplicate_rows is None

        conn.execute("ALTER TABLE pipeline_documents RENAME TO pipeline_documents_old")
        self._create_pipeline_documents_table(conn, include_unique=include_unique)
        target_columns = [
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(pipeline_documents)").fetchall()
        ]
        shared_columns = [column for column in target_columns if column in old_columns]
        if shared_columns:
            quoted = ", ".join(shared_columns)
            conn.execute(
                f"INSERT INTO pipeline_documents ({quoted}) "
                f"SELECT {quoted} FROM pipeline_documents_old"
            )
        conn.execute("DROP TABLE pipeline_documents_old")
        if not include_unique:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_pipeline_documents_path_identity "
                "ON pipeline_documents(kb_name, department_id, directory_id, document_name)"
            )

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

    # -- directory tree -----------------------------------------------------

    def list_directories(
        self,
        kb_name: str,
        department_id: str | int | None,
        *,
        parent_id: int = 0,
    ) -> list[dict[str, Any]]:
        department_id = _require_department_id(department_id, "directory listing")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, kb_id, kb_name, department_id, parent_id, name,
                       normalized_name, depth, created_by, created_at, updated_at
                FROM pipeline_directories
                WHERE kb_name = ? AND department_id = ? AND parent_id = ?
                ORDER BY normalized_name, id
                """,
                (kb_name, department_id, int(parent_id or 0)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_directory(
        self,
        directory_id: int,
        kb_name: str,
        department_id: str | int | None,
    ) -> dict[str, Any] | None:
        department_id = _require_department_id(department_id, "directory lookup")
        directory_id = int(directory_id or 0)
        if directory_id == 0:
            return {
                "id": 0,
                "kb_id": 0,
                "kb_name": kb_name,
                "department_id": department_id,
                "parent_id": 0,
                "name": "",
                "normalized_name": "",
                "depth": 0,
                "created_by": "",
                "created_at": "",
                "updated_at": "",
            }
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT id, kb_id, kb_name, department_id, parent_id, name,
                       normalized_name, depth, created_by, created_at, updated_at
                FROM pipeline_directories
                WHERE id = ? AND kb_name = ? AND department_id = ?
                """,
                (directory_id, kb_name, department_id),
            ).fetchone()
        return dict(row) if row else None

    def create_directory(
        self,
        *,
        kb_id: int | None,
        kb_name: str,
        department_id: str | int,
        parent_id: int,
        name: str,
        normalized_name: str,
        depth: int,
        created_by: str,
    ) -> dict[str, Any]:
        department_id = _require_department_id(department_id, "directory creation")
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO pipeline_directories (
                    kb_id, kb_name, department_id, parent_id, name,
                    normalized_name, depth, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(kb_id or 0), kb_name, department_id, int(parent_id or 0),
                    name, normalized_name, int(depth), created_by,
                ),
            )
            directory_id = int(cursor.lastrowid)
        created = self.get_directory(directory_id, kb_name, department_id)
        if created is None:  # pragma: no cover - defensive against broken stores
            raise RuntimeError("directory was inserted but could not be read back")
        return created

    def rename_directory(
        self,
        directory_id: int,
        kb_name: str,
        department_id: str | int,
        *,
        name: str,
        normalized_name: str,
    ) -> dict[str, Any] | None:
        department_id = _require_department_id(department_id, "directory rename")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE pipeline_directories
                SET name = ?, normalized_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND kb_name = ? AND department_id = ?
                """,
                (name, normalized_name, int(directory_id), kb_name, department_id),
            )
        return self.get_directory(directory_id, kb_name, department_id)

    def move_directory(
        self,
        directory_id: int,
        kb_name: str,
        department_id: str | int,
        *,
        parent_id: int,
        depth: int,
    ) -> dict[str, Any] | None:
        department_id = _require_department_id(department_id, "directory move")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE pipeline_directories
                SET parent_id = ?, depth = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND kb_name = ? AND department_id = ?
                """,
                (int(parent_id), int(depth), int(directory_id), kb_name, department_id),
            )
        return self.get_directory(directory_id, kb_name, department_id)

    def move_directory_subtree(
        self,
        directory_id: int,
        kb_name: str,
        department_id: str | int,
        *,
        parent_id: int,
        depth: int,
        name: str | None = None,
        normalized_name: str | None = None,
        descendant_depths: dict[int, int] | None = None,
    ) -> dict[str, Any] | None:
        """Move/rename one directory and its subtree in a single transaction."""

        department_id = _require_department_id(department_id, "directory move")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                from src.storage.domain_pg import advisory_xact_lock

                advisory_xact_lock(
                    conn, f"directory-move:{kb_name}:{department_id}"
                )
                if name is not None and normalized_name is not None:
                    conn.execute(
                        """
                        UPDATE pipeline_directories
                        SET parent_id = ?, depth = ?, name = ?,
                            normalized_name = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND kb_name = ? AND department_id = ?
                        """,
                        (
                            int(parent_id), int(depth), name, normalized_name,
                            int(directory_id), kb_name, department_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE pipeline_directories
                        SET parent_id = ?, depth = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND kb_name = ? AND department_id = ?
                        """,
                        (
                            int(parent_id), int(depth),
                            int(directory_id), kb_name, department_id,
                        ),
                    )
                for child_id, child_depth in (descendant_depths or {}).items():
                    conn.execute(
                        """
                        UPDATE pipeline_directories
                        SET depth = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND kb_name = ? AND department_id = ?
                        """,
                        (
                            int(child_depth), int(child_id), kb_name, department_id,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_directory(directory_id, kb_name, department_id)

    def update_directory_depth(
        self,
        directory_id: int,
        kb_name: str,
        department_id: str | int,
        *,
        depth: int,
    ) -> bool:
        department_id = _require_department_id(department_id, "directory depth update")
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE pipeline_directories
                SET depth = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND kb_name = ? AND department_id = ?
                """,
                (int(depth), int(directory_id), kb_name, department_id),
            )
        return bool(cursor.rowcount)

    def delete_directory(
        self,
        directory_id: int,
        kb_name: str,
        department_id: str | int,
    ) -> bool:
        department_id = _require_department_id(department_id, "directory deletion")
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                DELETE FROM pipeline_directories
                WHERE id = ? AND kb_name = ? AND department_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM pipeline_directories AS child
                      WHERE child.kb_name = pipeline_directories.kb_name
                        AND child.department_id = pipeline_directories.department_id
                        AND child.parent_id = pipeline_directories.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM pipeline_documents AS file
                      WHERE file.kb_name = pipeline_directories.kb_name
                        AND file.department_id = pipeline_directories.department_id
                        AND file.directory_id = pipeline_directories.id
                  )
                """,
                (int(directory_id), kb_name, department_id),
            )
        return bool(cursor.rowcount)

    def directory_file_count(
        self,
        directory_id: int,
        kb_name: str,
        department_id: str | int,
    ) -> int:
        department_id = _require_department_id(department_id, "directory file count")
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM pipeline_documents
                WHERE kb_name = ? AND department_id = ? AND directory_id = ?
                """,
                (kb_name, department_id, int(directory_id)),
            ).fetchone()
        return int(row["count"] or 0)

    def directory_child_count(
        self,
        directory_id: int,
        kb_name: str,
        department_id: str | int,
    ) -> int:
        department_id = _require_department_id(department_id, "directory child count")
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM pipeline_directories
                WHERE kb_name = ? AND department_id = ? AND parent_id = ?
                """,
                (kb_name, department_id, int(directory_id)),
            ).fetchone()
        return int(row["count"] or 0)

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
        directory_id: int = 0,
        original_file_name: str = "",
        local_path: str = "",
        file_size: int = 0,
        content_hash: str = "",
        upload_status: str = "",
        error_message: str = "",
        ragflow_error: str = "",
        content_kind: str = "document_text",
        processor_kind: str = "ragflow",
        shared_read_only: bool = False,
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
            existing_rows = conn.execute(
                """
                SELECT id, shared_read_only, content_hash
                FROM pipeline_documents
                WHERE kb_name = ? AND department_id = ? AND directory_id = ? AND document_name = ?
                """,
                (kb_name, department_id, int(directory_id or 0), document_name),
            ).fetchall()
            existing = existing_rows[0] if existing_rows else None
            if existing and bool(existing["shared_read_only"]) and not shared_read_only:
                raise PermissionError(
                    f"共享只读文档 '{document_name}' 不能被 develop 实例覆盖，请使用新文件名上传。"
                )
            if any(
                row["content_hash"]
                and content_hash
                and row["content_hash"] != content_hash
                for row in existing_rows
            ):
                raise ValueError(
                    f"文件路径冲突: directory={int(directory_id or 0)}/{document_name}"
                )
            values = (
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
                int(directory_id or 0),
                content_kind,
                processor_kind,
                int(bool(shared_read_only)),
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
            )
            if existing_rows:
                # Do not rely on a particular SQLite UNIQUE index here.  Some
                # legacy databases legitimately contain duplicate paths across
                # dataset partitions; the migration preserves those rows and
                # this explicit update keeps future writes deterministic.
                conn.execute(
                    """
                    UPDATE pipeline_documents SET
                        kb_id = ?, original_file_name = ?, dataset_id = ?, document_id = ?,
                        source_group = ?, uploaded_by = ?, status = ?, content_kind = ?,
                        processor_kind = ?, shared_read_only = ?, local_path = ?, file_size = ?,
                        content_hash = ?, upload_status = ?, error_message = ?, ragflow_error = ?,
                        parse_progress = ?, parse_stage = ?, asset_id = ?, logical_document_id = ?,
                        source_version_id = ?, project_id = ?, document_role = ?, module_scope_json = ?,
                        revision = ?, approval_status = ?, effective_from = ?, effective_to = ?,
                        usage_type = ?, worker_id = '', worker_started_at = '',
                        worker_heartbeat_at = '', parse_started_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        values[0], values[3], values[5], values[6], values[7], values[9], values[10],
                        values[12], values[13], values[14], values[15], values[16], values[17],
                        values[18], values[19], values[20], values[21], values[22], values[23],
                        values[24], values[25], values[26], values[27], values[28], values[29],
                        values[30], values[31], values[32], values[33], existing_rows[0]["id"],
                    ),
                )
                return
            insert_placeholders = ", ".join("?" for _ in values) + ", CURRENT_TIMESTAMP"
            conn.execute(
                f"""
                INSERT INTO pipeline_documents (
                    kb_id, kb_name, document_name, original_file_name, dataset_kind,
                    dataset_id, document_id, source_group, department_id,
                    uploaded_by, status, directory_id, content_kind, processor_kind,
                    shared_read_only,
                    local_path, file_size, content_hash, upload_status,
                    error_message, ragflow_error, parse_progress, parse_stage,
                    asset_id, logical_document_id, source_version_id, project_id, document_role,
                    module_scope_json, revision, approval_status, effective_from, effective_to, usage_type,
                    parse_started_at
                )
                VALUES ({insert_placeholders})
                """,
                # The placeholder list is generated from the values tuple so
                # schema additions cannot silently create a 34/35 mismatch.
                # SQLite accepts the current timestamp expression as the final
                # non-bound value.
                values,
            )

    def list_documents(
        self,
        kb_name: str,
        department_id: str | int | None = None,
        *,
        directory_id: int | None = None,
    ) -> list[PipelineDocumentRecord]:
        department_id = _require_department_id(department_id, "listing")
        conditions = ["kb_name = ?", "department_id = ?"]
        params: list[object] = [kb_name, department_id]
        if directory_id is not None:
            conditions.append("directory_id = ?")
            params.append(int(directory_id))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM pipeline_documents WHERE {' AND '.join(conditions)} "
                "ORDER BY directory_id, document_name",
                params,
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def list_documents_unscoped(self, kb_name: str) -> list[PipelineDocumentRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ?
                ORDER BY department_id, directory_id, document_name
                """,
                (kb_name,),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def last_parse_worker_heartbeat_age_seconds(self) -> float | None:
        """最近一次解析 worker 心跳距今的秒数；从未有过心跳时返回 None。

        H6: 表格解析的唯一执行者是外部 worker，此处给上传路径一个
        低成本存活性探测依据，避免 worker 缺位时任务静默滞留队列。
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT MAX(strftime('%s', worker_heartbeat_at)) AS last_seen,
                       strftime('%s', 'now') AS now_ts
                FROM pipeline_documents
                WHERE worker_heartbeat_at != ''
                """
            ).fetchone()
            if row is None or row["last_seen"] is None:
                return None
            return max(0.0, float(row["now_ts"]) - float(row["last_seen"]))

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
        directory_id: int = 0,
    ) -> PipelineDocumentRecord | None:
        department_id = _require_department_id(department_id, "lookup")
        if dataset_kind:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    """
                    SELECT * FROM pipeline_documents
                    WHERE kb_name = ? AND document_name = ? AND dataset_kind = ?
                      AND department_id = ? AND directory_id = ?
                    """,
                    (kb_name, document_name, dataset_kind, department_id, int(directory_id or 0)),
                ).fetchone()
            return self._to_record(row) if row else None

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ? AND document_name = ? AND department_id = ? AND directory_id = ?
                """,
                (kb_name, document_name, department_id, int(directory_id or 0)),
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
        directory_id: int = 0,
    ) -> PipelineDocumentRecord | None:
        department_id = _require_department_id(department_id, "hash lookup")
        if not content_hash:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ? AND dataset_kind = ? AND content_hash = ?
                  AND department_id = ? AND directory_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (kb_name, dataset_kind, content_hash, department_id, int(directory_id or 0)),
            ).fetchone()
        return self._to_record(row) if row else None

    def find_document_by_path(
        self,
        kb_name: str,
        department_id: str | int | None,
        directory_id: int,
        document_name: str,
    ) -> PipelineDocumentRecord | None:
        """Return the logical file occupying one directory path, if any."""
        department_id = _require_department_id(department_id, "path lookup")
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ? AND department_id = ? AND directory_id = ? AND document_name = ?
                LIMIT 1
                """,
                (kb_name, department_id, int(directory_id or 0), document_name),
            ).fetchone()
        return self._to_record(row) if row else None

    @staticmethod
    def _parse_parent_documents(raw: str) -> list[str]:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def update_embedding_meta(self, record_id: int, parent_document: str, embed_depth: int) -> None:
        """Attach P1-1 embedded-child metadata to a just-created record."""
        parents = [str(parent_document).strip()] if str(parent_document or "").strip() else []
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE pipeline_documents SET embed_depth = ?, parent_document = ? WHERE id = ?",
                (max(0, int(embed_depth)), json.dumps(parents, ensure_ascii=False), int(record_id)),
            )

    def add_parent_reference(self, record_id: int, parent_document: str) -> None:
        """Register an additional parent for a shared embedded child."""
        parent_document = str(parent_document or "").strip()
        if not parent_document:
            return
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT parent_document FROM pipeline_documents WHERE id = ?",
                (int(record_id),),
            ).fetchone()
            parents = self._parse_parent_documents(row["parent_document"] if row else "")
            if parent_document in parents:
                return
            parents.append(parent_document)
            conn.execute(
                "UPDATE pipeline_documents SET parent_document = ? WHERE id = ?",
                (json.dumps(parents, ensure_ascii=False), int(record_id)),
            )

    def list_embedded_children(
        self,
        kb_name: str,
        department_id: str | int | None,
        parent_document: str,
    ) -> list[PipelineDocumentRecord]:
        department_id = _require_department_id(department_id, "embedded child lookup")
        parent_document = str(parent_document or "").strip()
        if not parent_document:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM pipeline_documents
                WHERE kb_name = ? AND department_id = ? AND embed_depth > 0
                """,
                (kb_name, department_id),
            ).fetchall()
        return [
            record
            for record in (self._to_record(row) for row in rows)
            if parent_document in self._parse_parent_documents(record.parent_document)
        ]

    def remove_parent_reference(self, record_id: int, parent_document: str) -> bool:
        """Drop one parent reference; return True when the child is orphaned."""
        parent_document = str(parent_document or "").strip()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT parent_document FROM pipeline_documents WHERE id = ?",
                (int(record_id),),
            ).fetchone()
            if row is None:
                return True
            parents = [
                item
                for item in self._parse_parent_documents(row["parent_document"])
                if item != parent_document
            ]
            conn.execute(
                "UPDATE pipeline_documents SET parent_document = ? WHERE id = ?",
                (json.dumps(parents, ensure_ascii=False), int(record_id)),
            )
        return not parents

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
            self._write_profile_event(conn, record_id, "deleted")

    def delete_document_by_id_scoped(self, record_id: int, department_id: str | int | None):
        department_id = _require_department_id(department_id, "id deletion")
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM pipeline_documents WHERE id = ? AND department_id = ?",
                (record_id, department_id),
            )
            self._write_profile_event(conn, record_id, "deleted")

    def delete_document_by_remote_id(self, dataset_id: str, document_id: str):
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM pipeline_documents WHERE dataset_id = ? AND document_id = ?",
                (dataset_id, document_id),
            )
            self._write_profile_event(conn, None, "deleted", {"document_id": document_id})

    def delete_documents_by_kb(self, kb_name: str, department_id: str):
        if department_id in (None, ""):
            raise ValueError("department_id is required for scoped knowledge-base document deletion")
        with closing(self._connect()) as conn:
            ids = [
                item["id"]
                for item in conn.execute(
                    "SELECT id FROM pipeline_documents WHERE kb_name = ? AND department_id = ?",
                    (kb_name, department_id),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM pipeline_documents WHERE kb_name = ? AND department_id = ?",
                (kb_name, department_id),
            )
            for record_id in ids:
                self._write_profile_event(conn, record_id, "deleted")

    def delete_directories_by_kb(self, kb_name: str, department_id: str | int | None):
        department_id = _require_department_id(department_id, "directory knowledge-base deletion")
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM pipeline_directories WHERE kb_name = ? AND department_id = ?",
                (kb_name, department_id),
            )

    # ------------------------------------------------------------------
    # Durable directory-import batches (P1)
    # ------------------------------------------------------------------
    def create_directory_import_job(
        self,
        *,
        kb_id: int | None,
        kb_name: str,
        department_id: str | int,
        directory_id: int,
        created_by: str,
        files: list[dict],
    ) -> dict:
        department_id = _require_department_id(department_id, "directory import creation")
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO directory_import_jobs
                    (kb_id, kb_name, department_id, directory_id, status,
                     total_files, created_by)
                VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    int(kb_id or 0), kb_name, department_id, int(directory_id or 0),
                    len(files), str(created_by or ""),
                ),
            )
            job_id = int(cursor.lastrowid)
            for item in files:
                conn.execute(
                    """
                    INSERT INTO directory_import_files
                        (job_id, relative_path, source_group, directory_id,
                         original_name, staged_path, file_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        str(item.get("relative_path") or ""),
                        str(item.get("source_group") or ""),
                        int(item.get("directory_id", directory_id) or 0),
                        str(item.get("original_name") or ""),
                        str(item.get("staged_path") or ""),
                        int(item.get("file_size", 0) or 0),
                    ),
                )
        job = self.get_directory_import_job(job_id, kb_name, department_id)
        if job is None:  # pragma: no cover - defensive
            raise RuntimeError("directory import job was inserted but could not be read")
        return job

    def get_directory_import_job(
        self,
        job_id: int,
        kb_name: str,
        department_id: str | int | None,
    ) -> dict | None:
        department_id = _require_department_id(department_id, "directory import lookup")
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM directory_import_jobs
                WHERE id = ? AND kb_name = ? AND department_id = ?
                """,
                (int(job_id), kb_name, department_id),
            ).fetchone()
        return dict(row) if row else None

    def list_directory_import_jobs(
        self,
        kb_name: str,
        department_id: str | int | None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        department_id = _require_department_id(department_id, "directory import listing")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM directory_import_jobs
                WHERE kb_name = ? AND department_id = ?
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (kb_name, department_id, max(1, min(int(limit), 200)), max(0, int(offset))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_directory_import_files(
        self,
        job_id: int,
        kb_name: str,
        department_id: str | int | None,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict]:
        department_id = _require_department_id(department_id, "directory import file listing")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT file.*
                FROM directory_import_files AS file
                JOIN directory_import_jobs AS job ON job.id = file.job_id
                WHERE file.job_id = ? AND job.kb_name = ? AND job.department_id = ?
                ORDER BY file.id LIMIT ? OFFSET ?
                """,
                (int(job_id), kb_name, department_id, max(1, min(int(limit), 5000)), max(0, int(offset))),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_directory_import_file_stage(
        self,
        file_id: int,
        job_id: int,
        staged_path: str,
        *,
        file_size: int = 0,
    ) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE directory_import_files
                SET staged_path = ?, file_size = ?, status = 'queued',
                    error_code = '', error_message = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND job_id = ?
                """,
                (str(staged_path), int(file_size or 0), int(file_id), int(job_id)),
            )
        return bool(cursor.rowcount)

    def claim_directory_import_file(self, worker_id: str) -> dict | None:
        """Claim one queued file while respecting its job cancellation flag."""
        with closing(self._connect()) as conn:
            # A worker crash must not pin a staged file in ``uploading``
            # forever.  Reclaim leases older than the configured window.
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(seconds=DIRECTORY_IMPORT_LEASE_SECONDS)
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                UPDATE directory_import_files
                SET status = 'queued', worker_id = '',
                    worker_started_at = '', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'uploading'
                  AND worker_started_at <> ''
                  AND worker_started_at < ?
                """,
                (cutoff,),
            )
            conn.execute(
                """
                UPDATE directory_import_files
                SET status = 'uploading', worker_id = ?,
                    worker_started_at = CURRENT_TIMESTAMP,
                    attempt_count = attempt_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT file.id
                    FROM directory_import_files AS file
                    JOIN directory_import_jobs AS job ON job.id = file.job_id
                    WHERE file.status = 'queued'
                      AND file.staged_path <> ''
                      AND job.cancel_requested = 0
                      AND job.status NOT IN ('completed', 'failed', 'cancelled')
                    ORDER BY file.id
                    LIMIT 1
                )
                """,
                (str(worker_id),),
            )
            row = conn.execute(
                """
                SELECT file.*, job.kb_id, job.kb_name, job.department_id,
                       job.directory_id AS job_directory_id, job.created_by,
                       job.cancel_requested
                FROM directory_import_files AS file
                JOIN directory_import_jobs AS job ON job.id = file.job_id
                WHERE file.worker_id = ? AND file.status = 'uploading'
                ORDER BY file.updated_at, file.id LIMIT 1
                """,
                (str(worker_id),),
            ).fetchone()
        return dict(row) if row else None

    def finish_directory_import_file(
        self,
        *,
        job_id: int,
        file_id: int,
        status: str,
        record_id: int | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> dict | None:
        allowed = {"success", "reused", "failed", "skipped", "cancelled"}
        if status not in allowed:
            raise ValueError(f"invalid directory import file status: {status}")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE directory_import_files
                SET status = ?, record_id = ?, error_code = ?, error_message = ?,
                    worker_id = '', worker_started_at = '', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND job_id = ?
                """,
                (
                    status, record_id, str(error_code or ""), str(error_message or ""),
                    int(file_id), int(job_id),
                ),
            )
        return self._refresh_directory_import_job(int(job_id))

    def request_directory_import_cancel(self, job_id: int, kb_name: str, department_id: str | int | None) -> dict | None:
        department_id = _require_department_id(department_id, "directory import cancellation")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE directory_import_jobs
                SET cancel_requested = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND kb_name = ? AND department_id = ?
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (int(job_id), kb_name, department_id),
            )
            conn.execute(
                """
                UPDATE directory_import_files
                SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND status = 'queued'
                """,
                (int(job_id),),
            )
        return self._refresh_directory_import_job(int(job_id), kb_name=kb_name, department_id=department_id)

    def retry_directory_import(
        self,
        job_id: int,
        kb_name: str,
        department_id: str | int | None,
        *,
        file_id: int | None = None,
    ) -> dict | None:
        department_id = _require_department_id(department_id, "directory import retry")
        with closing(self._connect()) as conn:
            job = conn.execute(
                """
                SELECT id FROM directory_import_jobs
                WHERE id = ? AND kb_name = ? AND department_id = ?
                """,
                (int(job_id), kb_name, department_id),
            ).fetchone()
            if job is None:
                return None
            if file_id is None:
                conn.execute(
                    """
                    UPDATE directory_import_files
                    SET status = 'queued', error_code = '', error_message = '',
                        worker_id = '', worker_started_at = '', updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = ? AND status IN ('failed', 'skipped', 'cancelled')
                    """,
                    (int(job_id),),
                )
            else:
                conn.execute(
                    """
                    UPDATE directory_import_files
                    SET status = 'queued', error_code = '', error_message = '',
                        worker_id = '', worker_started_at = '', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND job_id = ? AND status IN ('failed', 'skipped', 'cancelled')
                    """,
                    (int(file_id), int(job_id)),
                )
            conn.execute(
                """
                UPDATE directory_import_jobs
                SET status = 'queued', cancel_requested = 0, last_error = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(job_id),),
            )
        return self._refresh_directory_import_job(int(job_id), kb_name=kb_name, department_id=department_id)

    def _refresh_directory_import_job(
        self,
        job_id: int,
        *,
        kb_name: str | None = None,
        department_id: str | None = None,
    ) -> dict | None:
        with closing(self._connect()) as conn:
            job = conn.execute(
                "SELECT * FROM directory_import_jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
            if job is None:
                return None
            files = conn.execute(
                "SELECT status, error_message FROM directory_import_files WHERE job_id = ?",
                (int(job_id),),
            ).fetchall()
            statuses = [str(row["status"] if hasattr(row, "keys") else row[0]) for row in files]
            success_count = sum(status in {"success", "reused"} for status in statuses)
            failed_count = sum(status == "failed" for status in statuses)
            skipped_count = sum(status in {"skipped", "cancelled"} for status in statuses)
            active = any(status in {"queued", "uploading"} for status in statuses)
            cancel_requested = bool(job["cancel_requested"] if hasattr(job, "keys") else job[10])
            if active:
                status = "running" if success_count or failed_count or skipped_count else "queued"
            elif cancel_requested and skipped_count:
                status = "cancelled"
            elif failed_count and success_count:
                status = "partial"
            elif failed_count:
                status = "failed"
            elif success_count and skipped_count:
                status = "partial"
            elif success_count:
                status = "completed"
            elif skipped_count:
                status = "failed"
            else:
                status = "queued"
            last_error = next(
                (
                    str(row["error_message"] if hasattr(row, "keys") else row[1])
                    for row in files
                    if str(row["error_message"] if hasattr(row, "keys") else row[1])
                ),
                "",
            )
            conn.execute(
                """
                UPDATE directory_import_jobs
                SET status = ?, success_count = ?, failed_count = ?, skipped_count = ?,
                    last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, success_count, failed_count, skipped_count, last_error, int(job_id)),
            )
            refreshed = conn.execute(
                "SELECT * FROM directory_import_jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
        if refreshed is None:
            return None
        if kb_name is not None and str(refreshed["kb_name"]) != kb_name:
            return None
        if department_id is not None and str(refreshed["department_id"]) != str(department_id):
            return None
        return dict(refreshed)

    def delete_directory_imports_by_kb(
        self, kb_name: str, department_id: str | int | None
    ) -> list[str]:
        """Delete import batches and return their staged paths for cleanup."""
        department_id = _require_department_id(department_id, "directory import knowledge-base deletion")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT file.staged_path
                FROM directory_import_files AS file
                JOIN directory_import_jobs AS job ON job.id = file.job_id
                WHERE job.kb_name = ? AND job.department_id = ?
                """,
                (kb_name, department_id),
            ).fetchall()
            paths = [str(row["staged_path"]) for row in rows if row["staged_path"]]
            conn.execute(
                """
                DELETE FROM directory_import_files
                WHERE job_id IN (
                    SELECT id FROM directory_import_jobs
                    WHERE kb_name = ? AND department_id = ?
                )
                """,
                (kb_name, department_id),
            )
            conn.execute(
                "DELETE FROM directory_import_jobs WHERE kb_name = ? AND department_id = ?",
                (kb_name, department_id),
            )
        return paths

    def delete_directory_import_job(
        self,
        job_id: int,
        kb_name: str,
        department_id: str | int | None,
    ) -> list[str]:
        """Delete one terminal import-history row and return its staged paths."""
        department_id = _require_department_id(department_id, "directory import deletion")
        with closing(self._connect()) as conn:
            job = conn.execute(
                """
                SELECT status FROM directory_import_jobs
                WHERE id = ? AND kb_name = ? AND department_id = ?
                """,
                (int(job_id), kb_name, department_id),
            ).fetchone()
            if job is None:
                raise LookupError("directory import job not found")
            if str(job["status"]) in {"queued", "running"}:
                raise ValueError("导入批次仍在运行，请先取消再删除记录")
            rows = conn.execute(
                """
                SELECT staged_path FROM directory_import_files
                WHERE job_id = ?
                """,
                (int(job_id),),
            ).fetchall()
            paths = [str(row["staged_path"]) for row in rows if row["staged_path"]]
            conn.execute("DELETE FROM directory_import_files WHERE job_id = ?", (int(job_id),))
            conn.execute(
                "DELETE FROM directory_import_jobs WHERE id = ? AND kb_name = ? AND department_id = ?",
                (int(job_id), kb_name, department_id),
            )
        return paths

    def delete_documents_by_kb_unscoped(self, kb_name: str):
        with closing(self._connect()) as conn:
            ids = [
                item["id"]
                for item in conn.execute(
                    "SELECT id FROM pipeline_documents WHERE kb_name = ?", (kb_name,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM pipeline_documents WHERE kb_name = ?", (kb_name,))
            for record_id in ids:
                self._write_profile_event(conn, record_id, "deleted")

    def update_document_status(self, dataset_id: str, document_id: str, status: str, error_message: str = ""):
        completed_expr = "CURRENT_TIMESTAMP" if status in TERMINAL_PARSE_STATUSES else "parse_completed_at"
        with closing(self._connect()) as conn:
            prev = conn.execute(
                "SELECT id, status FROM pipeline_documents WHERE dataset_id = ? AND document_id = ?",
                (dataset_id, document_id),
            ).fetchone()
            record_id = prev["id"] if prev is not None else None
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
            self._write_profile_event(conn, record_id, "parse_status", {"status": status})
        if normalize_parse_status(status) == TASK_STATUS_COMPLETED and (
            prev is None or normalize_parse_status(prev["status"]) != TASK_STATUS_COMPLETED
        ):
            _fire_shadow_asset_hook(record_id)

    def update_document_status_by_id(self, record_id: int, status: str, error_message: str = ""):
        completed_expr = "CURRENT_TIMESTAMP" if status in TERMINAL_PARSE_STATUSES else "parse_completed_at"
        with closing(self._connect()) as conn:
            prev = conn.execute(
                "SELECT status FROM pipeline_documents WHERE id = ?", (record_id,)
            ).fetchone()
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
            self._write_profile_event(conn, record_id, "parse_status", {"status": status})
        if normalize_parse_status(status) == TASK_STATUS_COMPLETED and (
            prev is None or normalize_parse_status(prev["status"]) != TASK_STATUS_COMPLETED
        ):
            _fire_shadow_asset_hook(record_id)

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
            # H5: 每次进度推进都滑动重置超时基准，避免按上传时刻计时的
            # 静态窗口把仍在缓慢推进的长解析误杀。
            "parse_started_at = CURRENT_TIMESTAMP",
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
            prev = conn.execute(
                "SELECT status FROM pipeline_documents WHERE id = ?", (record_id,)
            ).fetchone()
            conn.execute(
                f"""
                UPDATE pipeline_documents
                SET {', '.join(assignments)}
                WHERE id = ?
                """,
                params,
            )
        if status is not None and normalize_parse_status(status) == TASK_STATUS_COMPLETED and normalize_parse_status(
            (prev["status"] if prev is not None else "")
        ) != TASK_STATUS_COMPLETED:
            _fire_shadow_asset_hook(record_id)

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
        # 过期回收的心跳判断：SQLite 用 datetime('now', ?) 修饰符；PG 用
        # NULLIF + 显式 timestamptz 转型（'' 哨兵必须转 NULL，否则 cast 报错）。
        if self._backend == "postgres":
            stale_filter = (
                "  AND (NULLIF(worker_heartbeat_at, ''))::timestamptz"
                " < clock_timestamp() - (%s * interval '1 second')"
            )
            stale_params: tuple = (stale_after_seconds,)
        else:
            stale_filter = "  AND datetime(worker_heartbeat_at) < datetime('now', ?)"
            stale_params = (f"-{stale_after_seconds} seconds",)
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
                  {stale_filter}
                """,
                (TASK_STATUS_QUEUED, TASK_STATUS_QUEUED, *processor_kinds, *stale_params),
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
                    parse_started_at = CURRENT_TIMESTAMP,
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

    def list_status_refresh_candidates(
        self,
        processor_kinds: list[str],
        statuses: list[str],
        *,
        limit: int = 8,
        min_interval_seconds: int = 60,
    ) -> list[PipelineDocumentRecord]:
        """Records still waiting on a remote parse status, throttled by last check."""
        processor_kinds = tuple(processor_kinds)
        statuses = tuple(statuses)
        if not processor_kinds or not statuses:
            return []
        processor_placeholders = ", ".join("?" for _ in processor_kinds)
        status_placeholders = ", ".join("?" for _ in statuses)
        if self._backend == "postgres":
            refresh_filter = (
                "OR (NULLIF(last_status_checked_at, ''))::timestamptz"
                " < clock_timestamp() - (%s * interval '1 second')"
            )
            refresh_params = (max(0, int(min_interval_seconds)),)
        else:
            refresh_filter = "OR datetime(last_status_checked_at) < datetime('now', ?)"
            refresh_params = (f"-{max(0, int(min_interval_seconds))} seconds",)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM pipeline_documents
                WHERE processor_kind IN ({processor_placeholders})
                  AND status IN ({status_placeholders})
                  AND document_id != ''
                  AND (
                    last_status_checked_at = ''
                    {refresh_filter}
                  )
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (
                    *processor_kinds,
                    *statuses,
                    *refresh_params,
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def mark_status_checked(self, record_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE pipeline_documents SET last_status_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(record_id),),
            )

    def requeue_dead_letter_records(
        self,
        kb_name: str | None = None,
        department_id: str | int | None = None,
        processor_kinds: Sequence[str] | None = None,
    ) -> list[str]:
        """Re-queue dead-lettered parse records so workers can retry them.

        Background parsing fails into ``dead_letter`` after
        ``WORKER_MAX_RETRIES`` attempts.  Transient causes (remote backend
        hiccups, stale worker processes from an old code deployment) leave
        recoverable records stranded; this resets them to ``queued`` with a
        clean retry budget so the standing worker can pick them up again.
        Returns the re-queued document names.
        """

        kinds = tuple(processor_kinds or (PROCESSOR_KIND_SPREADSHEET,))
        if not kinds:
            return []
        placeholders = ", ".join("?" for _ in kinds)
        conditions = [f"processor_kind IN ({placeholders})", "status = ?"]
        params: list[Any] = [*kinds, TASK_STATUS_DEAD_LETTER]
        if kb_name:
            conditions.append("kb_name = ?")
            params.append(kb_name)
        if department_id not in (None, ""):
            conditions.append("department_id = ?")
            params.append(str(department_id))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT id, document_name FROM pipeline_documents WHERE {' AND '.join(conditions)}",
                params,
            ).fetchall()
            if not rows:
                return []
            conn.execute(
                f"""
                UPDATE pipeline_documents
                SET status = ?,
                    upload_status = ?,
                    retry_count = 0,
                    worker_id = '',
                    worker_started_at = '',
                    worker_heartbeat_at = '',
                    error_message = '',
                    ragflow_error = '',
                    parse_stage = '重新排队等待解析',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({", ".join("?" for _ in rows)})
                """,
                (
                    TASK_STATUS_QUEUED,
                    TASK_STATUS_QUEUED,
                    *[int(row["id"]) for row in rows],
                ),
            )
        return [str(row["document_name"]) for row in rows]

    def mark_document_failed_by_id(self, record_id: int, message: str):
        self.update_document_progress_by_id(
            record_id,
            100,
            "解析失败",
            status="failed",
            error_message=message,
        )
        self.release_parse_claim(record_id)

    # ── governed document profiles + invalidation outbox (task 5a) ────────

    def _profile_row_to_dict(self, row) -> dict[str, Any]:
        try:
            mpn_values = json.loads(row["mpn_values"] or "[]")
            identity_origin = json.loads(row["identity_origin"] or "{}")
        except ValueError:
            mpn_values, identity_origin = [], {}
        return {
            "record_id": row["record_id"],
            "kb_name": row["kb_name"],
            "department_id": row["department_id"],
            "remote_document_id": row["remote_document_id"],
            "parse_status": row["parse_status"],
            "content_hash": row["content_hash"],
            "source_version_id": row["source_version_id"],
            "revision": row["revision"],
            "mpn_values": mpn_values if isinstance(mpn_values, list) else [],
            "manufacturer": row["manufacturer"],
            "identity_origin": identity_origin if isinstance(identity_origin, dict) else {},
            "confirmed_at": row["confirmed_at"],
            "updated_at": row["updated_at"],
        }

    def _sync_profile_snapshot(self, conn, record_id: int) -> None:
        """Re-derive the governed stamps for one profile from the record."""
        row = conn.execute(
            """
            SELECT kb_name, department_id, document_id, status, content_hash,
                   source_version_id, revision
            FROM pipeline_documents WHERE id = ?
            """,
            (record_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute(
            """
            UPDATE pipeline_document_profiles
            SET kb_name = ?, department_id = ?, remote_document_id = ?,
                parse_status = ?, content_hash = ?, source_version_id = ?,
                revision = ?, updated_at = CURRENT_TIMESTAMP
            WHERE record_id = ?
            """,
            (
                row["kb_name"], row["department_id"], row["document_id"],
                row["status"], row["content_hash"], row["source_version_id"],
                row["revision"], record_id,
            ),
        )

    def confirm_document_identity(
        self,
        record_id: int,
        mpn_values: list[str] | None = None,
        manufacturer: str = "",
        origin: dict[str, Any] | None = None,
    ) -> None:
        """Write a governed identity snapshot; only explicit/confirmed data."""
        record = self.get_document_by_id(record_id)
        if record is None:
            raise ValueError(f"document record not found: {record_id}")
        payload = json.dumps(list(mpn_values or []), ensure_ascii=False)
        origin_payload = json.dumps(origin or {}, ensure_ascii=False)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO pipeline_document_profiles (
                    record_id, kb_name, department_id, remote_document_id,
                    parse_status, content_hash, source_version_id, revision,
                    mpn_values, manufacturer, identity_origin, confirmed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(record_id) DO UPDATE SET
                    kb_name = excluded.kb_name,
                    department_id = excluded.department_id,
                    remote_document_id = excluded.remote_document_id,
                    parse_status = excluded.parse_status,
                    content_hash = excluded.content_hash,
                    source_version_id = excluded.source_version_id,
                    revision = excluded.revision,
                    mpn_values = excluded.mpn_values,
                    manufacturer = excluded.manufacturer,
                    identity_origin = excluded.identity_origin,
                    confirmed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    record_id, record.kb_name, record.department_id, record.document_id,
                    record.status, record.content_hash, record.source_version_id,
                    record.revision, payload, str(manufacturer or ""), origin_payload,
                ),
            )
            self._write_profile_event(conn, record_id, "identity_confirmed")

    def get_document_profile(self, record_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM pipeline_document_profiles WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return self._profile_row_to_dict(row) if row is not None else None

    def rebuild_document_profiles(self) -> int:
        """Re-derive every stored profile's stamps from lifecycle records.

        Confirmed MPN/manufacturer identity is preserved; the version stamps
        are always copied fresh from ``pipeline_documents``.
        """
        with closing(self._connect()) as conn:
            ids = [
                item["record_id"]
                for item in conn.execute("SELECT record_id FROM pipeline_document_profiles").fetchall()
            ]
            for record_id in ids:
                self._sync_profile_snapshot(conn, record_id)
        return len(ids)

    def _write_profile_event(self, conn, record_id: int | None, event_kind: str, payload: dict[str, Any] | None = None) -> None:
        conn.execute(
            """
            INSERT INTO pipeline_profile_outbox (record_id, event_kind, payload)
            VALUES (?, ?, ?)
            """,
            (record_id, event_kind, json.dumps(payload or {}, ensure_ascii=False)),
        )

    def record_profile_event(self, record_id: int | None, event_kind: str, payload: dict[str, Any] | None = None) -> None:
        with closing(self._connect()) as conn:
            self._write_profile_event(conn, record_id, event_kind, payload)

    def list_pending_profile_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, record_id, event_kind, payload, created_at
                FROM pipeline_profile_outbox
                WHERE processed_at IS NULL
                ORDER BY id LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            events.append(
                {
                    "id": row["id"],
                    "record_id": row["record_id"],
                    "event_kind": row["event_kind"],
                    "payload": payload,
                    "created_at": row["created_at"],
                }
            )
        return events

    def mark_profile_event_processed(self, event_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE pipeline_profile_outbox SET processed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (event_id,),
            )
