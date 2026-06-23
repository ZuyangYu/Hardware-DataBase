import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass

import config.settings


@dataclass
class RAGFlowDocumentRecord:
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
    local_path: str = ""
    file_size: int = 0
    content_hash: str = ""
    upload_status: str = ""
    ragflow_error: str = ""
    last_status_checked_at: str = ""
    parse_started_at: str = ""
    parse_completed_at: str = ""
    created_at: str = ""
    updated_at: str = ""


class RAGFlowStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.path.join(config.settings.STORAGE_DIR, "ragflow_mappings.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ragflow_datasets (
                    kind TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    dataset_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ragflow_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    local_path TEXT NOT NULL DEFAULT '',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL DEFAULT '',
                    upload_status TEXT NOT NULL DEFAULT '',
                    ragflow_error TEXT NOT NULL DEFAULT '',
                    last_status_checked_at TEXT NOT NULL DEFAULT '',
                    parse_started_at TEXT NOT NULL DEFAULT '',
                    parse_completed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(kb_name, department_id, document_name, dataset_kind)
                )
            """)
            self._ensure_columns(conn)
            self._migrate_department_unique_constraint(conn)

    def _ensure_columns(self, conn):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(ragflow_documents)").fetchall()}
        migrations = {
            "original_file_name": "ALTER TABLE ragflow_documents ADD COLUMN original_file_name TEXT NOT NULL DEFAULT ''",
            "local_path": "ALTER TABLE ragflow_documents ADD COLUMN local_path TEXT NOT NULL DEFAULT ''",
            "file_size": "ALTER TABLE ragflow_documents ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0",
            "content_hash": "ALTER TABLE ragflow_documents ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
            "upload_status": "ALTER TABLE ragflow_documents ADD COLUMN upload_status TEXT NOT NULL DEFAULT ''",
            "ragflow_error": "ALTER TABLE ragflow_documents ADD COLUMN ragflow_error TEXT NOT NULL DEFAULT ''",
            "last_status_checked_at": "ALTER TABLE ragflow_documents ADD COLUMN last_status_checked_at TEXT NOT NULL DEFAULT ''",
            "parse_started_at": "ALTER TABLE ragflow_documents ADD COLUMN parse_started_at TEXT NOT NULL DEFAULT ''",
            "parse_completed_at": "ALTER TABLE ragflow_documents ADD COLUMN parse_completed_at TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)

    def _migrate_department_unique_constraint(self, conn):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ragflow_documents'"
        ).fetchone()
        table_sql = row["sql"] if row else ""
        if "UNIQUE(kb_name, document_name, dataset_kind)" not in table_sql:
            return

        conn.execute("ALTER TABLE ragflow_documents RENAME TO ragflow_documents_old")
        conn.execute("""
            CREATE TABLE ragflow_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                local_path TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT NOT NULL DEFAULT '',
                upload_status TEXT NOT NULL DEFAULT '',
                ragflow_error TEXT NOT NULL DEFAULT '',
                last_status_checked_at TEXT NOT NULL DEFAULT '',
                parse_started_at TEXT NOT NULL DEFAULT '',
                parse_completed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(kb_name, department_id, document_name, dataset_kind)
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO ragflow_documents (
                id, kb_name, document_name, original_file_name, dataset_kind,
                dataset_id, document_id, source_group, department_id, uploaded_by,
                status, local_path, file_size, content_hash, upload_status,
                ragflow_error, last_status_checked_at, parse_started_at,
                parse_completed_at, created_at, updated_at
            )
            SELECT
                id, kb_name, document_name, original_file_name, dataset_kind,
                dataset_id, document_id, source_group, department_id, uploaded_by,
                status, local_path, file_size, content_hash, upload_status,
                ragflow_error, last_status_checked_at, parse_started_at,
                parse_completed_at, created_at, updated_at
            FROM ragflow_documents_old
        """)
        conn.execute("DROP TABLE ragflow_documents_old")

    def get_dataset_id(self, kind: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT dataset_id FROM ragflow_datasets WHERE kind = ?", (kind,)).fetchone()
        return row["dataset_id"] if row else None

    def save_dataset(self, kind: str, dataset_id: str, dataset_name: str):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO ragflow_datasets (kind, dataset_id, dataset_name, updated_at)
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
        status: str = "uploaded",
        original_file_name: str = "",
        local_path: str = "",
        file_size: int = 0,
        content_hash: str = "",
        upload_status: str = "",
        ragflow_error: str = "",
    ):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO ragflow_documents (
                    kb_name, document_name, original_file_name, dataset_kind, dataset_id, document_id,
                    source_group, department_id, uploaded_by, status, local_path, file_size,
                    content_hash, upload_status, ragflow_error, parse_started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(kb_name, department_id, document_name, dataset_kind) DO UPDATE SET
                    department_id = excluded.department_id,
                    original_file_name = excluded.original_file_name,
                    dataset_id = excluded.dataset_id,
                    document_id = excluded.document_id,
                    source_group = excluded.source_group,
                    uploaded_by = excluded.uploaded_by,
                    status = excluded.status,
                    local_path = excluded.local_path,
                    file_size = excluded.file_size,
                    content_hash = excluded.content_hash,
                    upload_status = excluded.upload_status,
                    ragflow_error = excluded.ragflow_error,
                    parse_started_at = excluded.parse_started_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
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
                    local_path,
                    file_size,
                    content_hash,
                    upload_status,
                    ragflow_error,
                ),
            )

    def list_documents(self, kb_name: str, department_id: str | None = None) -> list[RAGFlowDocumentRecord]:
        with closing(self._connect()) as conn:
            if department_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM ragflow_documents
                    WHERE kb_name = ?
                    ORDER BY document_name
                    """,
                    (kb_name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM ragflow_documents
                    WHERE kb_name = ? AND department_id = ?
                    ORDER BY document_name
                    """,
                    (kb_name, department_id),
                ).fetchall()
        return [RAGFlowDocumentRecord(**dict(row)) for row in rows]

    def get_document_by_id(self, record_id: int) -> RAGFlowDocumentRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM ragflow_documents WHERE id = ?",
                (record_id,),
            ).fetchone()
        return RAGFlowDocumentRecord(**dict(row)) if row else None

    def get_document(
        self,
        kb_name: str,
        document_name: str,
        dataset_kind: str | None = None,
    ) -> RAGFlowDocumentRecord | None:
        if dataset_kind:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    """
                    SELECT * FROM ragflow_documents
                    WHERE kb_name = ? AND document_name = ? AND dataset_kind = ?
                    """,
                    (kb_name, document_name, dataset_kind),
                ).fetchone()
            return RAGFlowDocumentRecord(**dict(row)) if row else None

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM ragflow_documents
                WHERE kb_name = ? AND document_name = ?
                """,
                (kb_name, document_name),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Ambiguous RAGFlow document name in {kb_name}: {document_name}")
        return RAGFlowDocumentRecord(**dict(rows[0])) if rows else None

    def find_by_hash(
        self,
        kb_name: str,
        dataset_kind: str,
        content_hash: str,
        department_id: str,
    ) -> RAGFlowDocumentRecord | None:
        if not content_hash:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM ragflow_documents
                WHERE kb_name = ? AND dataset_kind = ? AND content_hash = ? AND department_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (kb_name, dataset_kind, content_hash, department_id),
            ).fetchone()
        return RAGFlowDocumentRecord(**dict(row)) if row else None

    def delete_document(self, kb_name: str, document_name: str, dataset_kind: str | None = None):
        if dataset_kind:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    DELETE FROM ragflow_documents
                    WHERE kb_name = ? AND document_name = ? AND dataset_kind = ?
                    """,
                    (kb_name, document_name, dataset_kind),
                )
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM ragflow_documents WHERE kb_name = ? AND document_name = ?",
                (kb_name, document_name),
            )

    def delete_document_by_id(self, record_id: int):
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM ragflow_documents WHERE id = ?", (record_id,))

    def delete_document_by_remote_id(self, dataset_id: str, document_id: str):
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM ragflow_documents WHERE dataset_id = ? AND document_id = ?",
                (dataset_id, document_id),
            )

    def update_document_status(self, dataset_id: str, document_id: str, status: str, error_message: str = ""):
        completed_expr = "CURRENT_TIMESTAMP" if status in {"parsed", "failed", "deleted"} else "parse_completed_at"
        with closing(self._connect()) as conn:
            conn.execute(
                f"""
                UPDATE ragflow_documents
                SET status = ?,
                    upload_status = ?,
                    ragflow_error = ?,
                    last_status_checked_at = CURRENT_TIMESTAMP,
                    parse_completed_at = {completed_expr},
                    updated_at = CURRENT_TIMESTAMP
                WHERE dataset_id = ? AND document_id = ?
                """,
                (status, status, error_message, dataset_id, document_id),
            )
