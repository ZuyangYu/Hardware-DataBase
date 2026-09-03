"""SQLite-backed snapshots, export jobs and private artifact files."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import zipfile

import src.settings
from src.result_exports.models import (
    EXPORT_JOB_STATUSES,
    Artifact,
    ArtifactHistoryEntry,
    ExportJob,
    ResultEnvelope,
    ResultSnapshot,
    is_export_format_enabled,
    normalize_content_shape,
    normalize_export_format,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _as_id(value: str | int) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError("export owner/source identifiers are required")
    return result


def _safe_filename(value: str, extension: str) -> str:
    name = os.path.basename(str(value or "")).replace("\r", " ").replace("\n", " ").strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name).strip(" .")
    if not name:
        name = "export"
    if not name.lower().endswith(f".{extension}"):
        name = f"{name}.{extension}"
    return name[:180]


_ARTIFACT_MIME_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _validate_artifact_payload(format: str, content: bytes, mime_type: str) -> None:
    expected_mime = _ARTIFACT_MIME_TYPES.get(format)
    if expected_mime is None or mime_type != expected_mime:
        raise ValueError("artifact MIME type does not match export format")
    if format == "md":
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("artifact signature is not valid UTF-8 Markdown") from exc
        return
    if format == "pdf":
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-128:]:
            raise ValueError("artifact signature is not a valid PDF")
        return
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise ValueError("artifact signature is not a valid Office package")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        required = {
            "xlsx": {"[Content_Types].xml", "xl/workbook.xml"},
            "docx": {"[Content_Types].xml", "word/document.xml"},
            "pptx": {"[Content_Types].xml", "ppt/presentation.xml"},
        }[format]
        if not required.issubset(names):
            raise ValueError("artifact signature is not a valid Office package")
        lowered_names = {name.lower() for name in names}
        if any("vbaproject" in name or "externallink" in name for name in lowered_names):
            raise ValueError("unsafe Office package content")
        for name in names:
            if name.endswith(".rels"):
                relationship_xml = archive.read(name).lower()
                if b'targetmode="external"' in relationship_xml:
                    raise ValueError("external Office package links are not allowed")


class ResultExportStore:
    """Durable repository with idempotent creation and lease-based claims."""

    def __init__(self, db_path: str | None = None, storage_dir: str | None = None):
        self.db_path = db_path or src.settings.AUTH_DB_PATH
        self.storage_dir = Path(
            storage_dir
            or getattr(src.settings, "RESULT_EXPORT_STORAGE_DIR", os.path.join(src.settings.STORAGE_DIR, "exports"))
        ).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.storage_dir, 0o700)
        except OSError:
            pass
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS result_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    department_id TEXT,
                    knowledge_base_name TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    assistant_message_id INTEGER,
                    schema_version TEXT NOT NULL DEFAULT 'v1',
                    source_hash TEXT NOT NULL DEFAULT '',
                    envelope_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_user_id, turn_id)
                );
                CREATE INDEX IF NOT EXISTS idx_result_snapshots_session
                    ON result_snapshots(owner_user_id, session_id, created_at);
                CREATE TABLE IF NOT EXISTS result_export_jobs (
                    export_job_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    department_id TEXT,
                    knowledge_base_name TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL,
                    assistant_message_id INTEGER,
                    format TEXT NOT NULL,
                    content_shape TEXT NOT NULL,
                    client_request_id TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_token INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    artifact_id TEXT,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(snapshot_id) REFERENCES result_snapshots(snapshot_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_result_export_jobs_idempotency
                    ON result_export_jobs(owner_user_id, client_request_id, format);
                CREATE INDEX IF NOT EXISTS idx_result_export_jobs_queue
                    ON result_export_jobs(status, available_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_result_export_jobs_session
                    ON result_export_jobs(owner_user_id, session_id, created_at);
                CREATE TABLE IF NOT EXISTS result_export_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    export_job_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT,
                    FOREIGN KEY(export_job_id) REFERENCES result_export_jobs(export_job_id)
                );
                CREATE TABLE IF NOT EXISTS result_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    export_job_id TEXT NOT NULL UNIQUE,
                    owner_user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    department_id TEXT,
                    knowledge_base_name TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL,
                    format TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_ref TEXT NOT NULL,
                    preview_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    FOREIGN KEY(export_job_id) REFERENCES result_export_jobs(export_job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_result_artifacts_owner
                    ON result_artifacts(owner_user_id, session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_result_artifacts_job_history
                    ON result_artifacts(export_job_id, created_at DESC);
                """
            )
            self._ensure_column(conn, "result_snapshots", "department_id", "TEXT")
            self._ensure_column(conn, "result_snapshots", "knowledge_base_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "result_snapshots", "assistant_message_id", "INTEGER")
            self._ensure_column(conn, "result_snapshots", "schema_version", "TEXT NOT NULL DEFAULT 'v1'")
            self._ensure_column(conn, "result_snapshots", "source_hash", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "result_export_jobs", "department_id", "TEXT")
            self._ensure_column(conn, "result_export_jobs", "knowledge_base_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "result_export_jobs", "assistant_message_id", "INTEGER")
            self._ensure_column(conn, "result_artifacts", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column(conn, "result_artifacts", "department_id", "TEXT")
            self._ensure_column(conn, "result_artifacts", "knowledge_base_name", "TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_result_artifacts_expires ON result_artifacts(expires_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_result_snapshots_scope "
                "ON result_snapshots(owner_user_id, tenant_id, department_id, knowledge_base_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_result_export_jobs_scope "
                "ON result_export_jobs(owner_user_id, tenant_id, department_id, knowledge_base_name)"
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _create_snapshot_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        owner_user_id: str | int,
        tenant_id: str,
        department_id: str | int | None,
        knowledge_base_name: str | None,
        session_id: str | int,
        turn_id: str,
        assistant_message_id: int | None,
        envelope: ResultEnvelope | dict[str, Any],
    ) -> ResultSnapshot:
        owner = _as_id(owner_user_id)
        tenant = _as_id(tenant_id)
        department = None if department_id in (None, "") else str(department_id)
        session = _as_id(session_id)
        turn = _as_id(turn_id)
        envelope_obj = envelope if isinstance(envelope, ResultEnvelope) else ResultEnvelope.from_dict(envelope)
        envelope_obj = envelope_obj.normalized()
        envelope_json = _json(envelope_obj.to_dict())
        source_hash = hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
        knowledge_base = str(
            knowledge_base_name
            or (envelope_obj.metadata or {}).get("knowledge_base")
            or ""
        ).strip()
        existing = conn.execute(
            "SELECT * FROM result_snapshots WHERE owner_user_id = ? AND turn_id = ?",
            (owner, turn),
        ).fetchone()
        if existing is not None:
            if str(existing["envelope_json"]) != envelope_json:
                raise ValueError("result snapshot is immutable")
            return _row_to_snapshot(existing)
        snapshot_id = f"snapshot-{uuid.uuid4().hex}"
        now = _iso()
        conn.execute(
            """INSERT INTO result_snapshots(
                snapshot_id, owner_user_id, tenant_id, department_id, knowledge_base_name,
                session_id, turn_id, assistant_message_id, schema_version, source_hash,
                envelope_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                owner,
                tenant,
                department,
                knowledge_base,
                session,
                turn,
                assistant_message_id,
                envelope_obj.schema_version or "v1",
                source_hash,
                envelope_json,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM result_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        return _row_to_snapshot(row)

    def create_snapshot(
        self,
        *,
        owner_user_id: str | int,
        tenant_id: str,
        department_id: str | int | None = None,
        knowledge_base_name: str | None = None,
        session_id: str | int,
        turn_id: str,
        assistant_message_id: int | None = None,
        envelope: ResultEnvelope | dict[str, Any],
    ) -> ResultSnapshot:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                snapshot = self._create_snapshot_in_connection(
                    conn,
                    owner_user_id=owner_user_id,
                    tenant_id=tenant_id,
                    department_id=department_id,
                    knowledge_base_name=knowledge_base_name,
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_message_id=assistant_message_id,
                    envelope=envelope,
                )
                conn.execute("COMMIT")
                return snapshot
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_snapshot(self, owner_user_id: str | int, snapshot_id: str) -> ResultSnapshot | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM result_snapshots WHERE snapshot_id = ? AND owner_user_id = ?",
                (str(snapshot_id), str(owner_user_id)),
            ).fetchone()
        return _row_to_snapshot(row) if row else None

    def _create_export_job_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        owner_user_id: str | int,
        tenant_id: str | None,
        department_id: str | int | None,
        knowledge_base_name: str | None,
        session_id: str | int,
        snapshot_id: str,
        format: str,
        content_shape: str = "report",
        client_request_id: str,
        options: dict[str, Any] | None = None,
        max_attempts: int = 3,
        available_at: datetime | None = None,
    ) -> ExportJob:
        owner = _as_id(owner_user_id)
        session = _as_id(session_id)
        request_id = _as_id(client_request_id)[:128]
        normalized_format = normalize_export_format(format)
        if not is_export_format_enabled(normalized_format):
            raise ValueError(f"export format is disabled: {normalized_format}")
        shape = normalize_content_shape(content_shape)
        options_value = dict(options or {})
        max_attempts = max(1, min(int(max_attempts), 20))
        available = (available_at or _now()).astimezone(timezone.utc)
        now = _iso()
        snapshot = conn.execute(
            "SELECT * FROM result_snapshots WHERE snapshot_id = ? AND owner_user_id = ?",
            (snapshot_id, owner),
        ).fetchone()
        if snapshot is None:
            raise KeyError("result snapshot not found")
        if str(snapshot["session_id"]) != session:
            raise ValueError("export session does not match result snapshot")
        tenant_value = _as_id(tenant_id or snapshot["tenant_id"])
        department_value = (
            str(department_id)
            if department_id not in (None, "")
            else snapshot["department_id"]
        )
        knowledge_base_value = str(
            knowledge_base_name
            or snapshot["knowledge_base_name"]
            or ""
        ).strip()
        assistant_message_value = snapshot["assistant_message_id"]
        existing = conn.execute(
            """SELECT * FROM result_export_jobs
               WHERE owner_user_id = ? AND client_request_id = ? AND format = ?""",
            (owner, request_id, normalized_format),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["snapshot_id"]) != str(snapshot_id)
                or str(existing["content_shape"]) != shape
                or _load(existing["options_json"], {}) != options_value
            ):
                raise ValueError("export idempotency key conflicts with existing payload")
            return _row_to_job(existing)
        job_id = f"export-job-{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO result_export_jobs(
                export_job_id, snapshot_id, owner_user_id, tenant_id, department_id,
                knowledge_base_name, session_id, assistant_message_id, format,
                content_shape, client_request_id, options_json, status,
                max_attempts, available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (
                job_id,
                snapshot_id,
                owner,
                tenant_value,
                department_value,
                knowledge_base_value,
                session,
                assistant_message_value,
                normalized_format,
                shape,
                request_id,
                _json(options_value),
                max_attempts,
                _iso(available),
                now,
                now,
            ),
        )
        conn.execute(
            """INSERT INTO result_export_outbox(
                outbox_id, export_job_id, available_at, created_at
            ) VALUES (?, ?, ?, ?)""",
            (f"export-outbox-{uuid.uuid4().hex}", job_id, _iso(available), now),
        )
        row = conn.execute("SELECT * FROM result_export_jobs WHERE export_job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row)

    def create_export_job(
        self,
        *,
        owner_user_id: str | int,
        tenant_id: str | None,
        department_id: str | int | None = None,
        knowledge_base_name: str | None = None,
        session_id: str | int,
        snapshot_id: str,
        format: str,
        content_shape: str = "report",
        client_request_id: str,
        options: dict[str, Any] | None = None,
        max_attempts: int = 3,
        available_at: datetime | None = None,
    ) -> ExportJob:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._create_export_job_in_connection(
                    conn,
                    owner_user_id=owner_user_id,
                    tenant_id=tenant_id,
                    department_id=department_id,
                    knowledge_base_name=knowledge_base_name,
                    session_id=session_id,
                    snapshot_id=snapshot_id,
                    format=format,
                    content_shape=content_shape,
                    client_request_id=client_request_id,
                    options=options,
                    max_attempts=max_attempts,
                    available_at=available_at,
                )
                conn.execute("COMMIT")
                return job
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def enqueue_completed_turn_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        owner_user_id: str | int,
        tenant_id: str,
        department_id: str | int | None,
        knowledge_base_name: str | None,
        session_id: str | int,
        turn_id: str,
        assistant_message_id: int | None,
        envelope: ResultEnvelope | dict[str, Any],
        export_plan: dict[str, Any],
    ) -> tuple[ResultSnapshot, list[ExportJob]]:
        """Create one snapshot and all format jobs inside the caller's tx."""

        formats = list(dict.fromkeys(
            normalize_export_format(value)
            for value in (export_plan.get("formats") or [])
            if is_export_format_enabled(value)
        ))
        if not formats:
            return (
                self._create_snapshot_in_connection(
                    conn,
                    owner_user_id=owner_user_id,
                    tenant_id=tenant_id,
                    department_id=department_id,
                    knowledge_base_name=knowledge_base_name,
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_message_id=assistant_message_id,
                    envelope=envelope,
                ),
                [],
            )
        if len(formats) > 5:
            raise ValueError("at most 5 export formats are allowed")
        snapshot = self._create_snapshot_in_connection(
            conn,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            department_id=department_id,
            knowledge_base_name=knowledge_base_name,
            session_id=session_id,
            turn_id=turn_id,
            assistant_message_id=assistant_message_id,
            envelope=envelope,
        )
        options = dict(export_plan.get("options") or {})
        options["include_citations"] = bool(export_plan.get("include_citations", True))
        title = export_plan.get("title")
        if title:
            options["render_title"] = str(title)[:160]
        request_id = str(export_plan.get("client_request_id") or f"turn-export-{turn_id}")
        jobs = [
            self._create_export_job_in_connection(
                conn,
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                department_id=department_id,
                knowledge_base_name=knowledge_base_name,
                session_id=session_id,
                snapshot_id=snapshot.snapshot_id,
                format=format,
                content_shape=str(export_plan.get("content_shape") or "report"),
                client_request_id=request_id,
                options=options,
            )
            for format in formats
        ]
        return snapshot, jobs

    def enqueue_turn_exports(
        self,
        *,
        owner_user_id: str | int,
        tenant_id: str,
        department_id: str | int | None,
        knowledge_base_name: str | None,
        session_id: str | int,
        turn_id: str,
        assistant_message_id: int | None,
        envelope: ResultEnvelope | dict[str, Any],
        formats: list[str],
        content_shape: str = "report",
        client_request_id: str,
        title: str | None = None,
        include_citations: bool = True,
        options: dict[str, Any] | None = None,
    ) -> tuple[ResultSnapshot, list[ExportJob]]:
        """Create a snapshot and all manual export jobs atomically."""

        export_plan = {
            "formats": list(formats),
            "content_shape": content_shape,
            "title": title,
            "include_citations": include_citations,
            "options": dict(options or {}),
            "client_request_id": client_request_id,
        }
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self.enqueue_completed_turn_in_connection(
                    conn,
                    owner_user_id=owner_user_id,
                    tenant_id=tenant_id,
                    department_id=department_id,
                    knowledge_base_name=knowledge_base_name,
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_message_id=assistant_message_id,
                    envelope=envelope,
                    export_plan=export_plan,
                )
                conn.execute("COMMIT")
                return result
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_export_job(self, owner_user_id: str | int, export_job_id: str) -> ExportJob | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM result_export_jobs WHERE export_job_id = ? AND owner_user_id = ?",
                (str(export_job_id), str(owner_user_id)),
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_export_jobs(
        self,
        owner_user_id: str | int,
        *,
        session_id: str | int | None = None,
        status: str | None = None,
        limit: int = 64,
    ) -> list[ExportJob]:
        clauses = ["owner_user_id = ?"]
        params: list[Any] = [str(owner_user_id)]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if status:
            if status not in EXPORT_JOB_STATUSES:
                raise ValueError("unsupported export job status")
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, min(int(limit), 200)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM result_export_jobs WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC, export_job_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_pending(self, limit: int = 16) -> list[ExportJob]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM result_export_jobs
                   WHERE (status = 'queued' AND datetime(available_at) <= datetime('now'))
                      OR (status = 'running' AND lease_expires_at IS NOT NULL
                          AND datetime(lease_expires_at) <= datetime('now'))
                   ORDER BY created_at, export_job_id LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def queue_state(self) -> tuple[int, float]:
        """Return queued depth and age of the oldest queued export job."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS depth, MIN(created_at) AS oldest_created_at
                   FROM result_export_jobs WHERE status = 'queued'"""
            ).fetchone()
        depth = int(row["depth"] or 0)
        oldest = row["oldest_created_at"]
        if not oldest:
            return depth, 0.0
        try:
            age = (_now() - datetime.fromisoformat(str(oldest))).total_seconds()
        except (TypeError, ValueError):
            age = 0.0
        return depth, max(0.0, age)

    def claim(self, export_job_id: str, worker_id: str, lease_seconds: int = 60) -> ExportJob | None:
        worker = str(worker_id or "").strip()
        if not worker:
            raise ValueError("worker_id is required")
        now = _now()
        expires = now + timedelta(seconds=max(5, int(lease_seconds)))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM result_export_jobs WHERE export_job_id = ?", (export_job_id,)
                ).fetchone()
                if row is None or row["status"] in {"succeeded", "cancelled", "failed", "dead_letter"}:
                    conn.execute("COMMIT")
                    return None
                available = row["status"] == "queued" and str(row["available_at"]) <= _iso(now)
                expired = row["status"] == "running" and row["lease_expires_at"] and str(row["lease_expires_at"]) <= _iso(now)
                if not (available or expired):
                    conn.execute("COMMIT")
                    return None
                if row["status"] == "queued":
                    max_running = max(1, int(getattr(src.settings, "RESULT_EXPORT_MAX_RUNNING_JOBS", 4)))
                    active_running = conn.execute(
                        """SELECT COUNT(*) AS count FROM result_export_jobs
                           WHERE status = 'running' AND lease_expires_at IS NOT NULL
                             AND datetime(lease_expires_at) > datetime(?)""",
                        (_iso(now),),
                    ).fetchone()["count"]
                    if int(active_running or 0) >= max_running:
                        conn.execute("COMMIT")
                        return None
                attempt = int(row["attempt"] or 0) + 1
                if attempt > int(row["max_attempts"] or 1):
                    conn.execute(
                        """UPDATE result_export_jobs SET status = 'dead_letter', error_message = ?,
                           lease_owner = NULL, lease_expires_at = NULL, completed_at = ?, updated_at = ?
                           WHERE export_job_id = ?""",
                        ("maximum_attempts_exceeded", _iso(now), _iso(now), export_job_id),
                    )
                    conn.execute(
                        "UPDATE result_export_outbox SET status = 'failed', last_error = ?, dispatched_at = ? WHERE export_job_id = ?",
                        ("maximum_attempts_exceeded", _iso(now), export_job_id),
                    )
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """UPDATE result_export_jobs SET status = 'running', attempt = ?, lease_owner = ?,
                       lease_token = lease_token + 1, lease_expires_at = ?, updated_at = ?, error_message = ''
                       WHERE export_job_id = ?""",
                    (attempt, worker, _iso(expires), _iso(now), export_job_id),
                )
                claimed = conn.execute(
                    "SELECT * FROM result_export_jobs WHERE export_job_id = ?", (export_job_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return _row_to_job(claimed)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def heartbeat(self, export_job_id: str, worker_id: str, lease_token: int, lease_seconds: int = 60) -> ExportJob:
        now = _now()
        expires = now + timedelta(seconds=max(5, int(lease_seconds)))
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """UPDATE result_export_jobs SET lease_expires_at = ?, updated_at = ?
                   WHERE export_job_id = ? AND status = 'running' AND lease_owner = ? AND lease_token = ?""",
                (_iso(expires), _iso(now), export_job_id, str(worker_id), int(lease_token)),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("export job lease lost")
        job = self._get_job_unscoped(export_job_id)
        if job is None:
            raise KeyError(export_job_id)
        return job

    def publish_artifact(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: int,
        content: bytes,
        filename: str,
        mime_type: str,
        preview: dict[str, Any] | None = None,
    ) -> Artifact:
        max_bytes = max(1, int(getattr(src.settings, "RESULT_EXPORT_MAX_BYTES", 25 * 1024 * 1024)))
        if len(content) > max_bytes:
            raise ValueError("export artifact exceeds size limit")
        artifact_id = f"artifact-{uuid.uuid4().hex}"
        extension = normalize_export_format(os.path.splitext(filename)[1].lstrip(".") or "md")
        safe_name = _safe_filename(filename, extension)
        storage_ref = f"{artifact_id}.{extension}"
        path = self.storage_dir / storage_ref
        temporary = self.storage_dir / f".{storage_ref}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "wb") as handle:
                handle.write(content)
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            now = _iso()
            digest = hashlib.sha256(content).hexdigest()
            retention_days = max(1, int(getattr(src.settings, "RESULT_EXPORT_RETENTION_DAYS", 30)))
            expires_at = _iso(_now() + timedelta(days=retention_days))
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    job = conn.execute(
                        "SELECT * FROM result_export_jobs WHERE export_job_id = ?", (job_id,)
                    ).fetchone()
                    if (
                        job is None
                        or job["status"] != "running"
                        or job["lease_owner"] != str(worker_id)
                        or int(job["lease_token"] or 0) != int(lease_token)
                    ):
                        raise RuntimeError("export job lease lost")
                    if extension != str(job["format"]):
                        raise ValueError("artifact format does not match export job")
                    _validate_artifact_payload(str(job["format"]), content, mime_type)
                    conn.execute(
                        """INSERT INTO result_artifacts(
                            artifact_id, export_job_id, owner_user_id, tenant_id, department_id,
                            knowledge_base_name, session_id, format, filename, mime_type, size,
                            sha256, storage_ref, preview_json, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            artifact_id, job_id, job["owner_user_id"], job["tenant_id"], job["department_id"],
                            job["knowledge_base_name"], job["session_id"], extension, safe_name, mime_type,
                            len(content), digest, storage_ref, _json(preview or {}), now, expires_at,
                        ),
                    )
                    conn.execute(
                        """UPDATE result_export_jobs SET status = 'succeeded', artifact_id = ?,
                           lease_owner = NULL, lease_expires_at = NULL, completed_at = ?, updated_at = ?
                           WHERE export_job_id = ?""",
                        (artifact_id, now, now, job_id),
                    )
                    conn.execute(
                        "UPDATE result_export_outbox SET status = 'dispatched', dispatched_at = ? WHERE export_job_id = ?",
                        (now, job_id),
                    )
                    row = conn.execute("SELECT * FROM result_artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            return _row_to_artifact(row)
        except Exception:
            try:
                path.unlink(missing_ok=True)
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def fail(
        self,
        job_id: str,
        worker_id: str,
        lease_token: int,
        message: str,
        *,
        retryable: bool = True,
        backoff_seconds: int = 5,
    ) -> ExportJob:
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM result_export_jobs WHERE export_job_id = ?", (job_id,)).fetchone()
                if (
                    row is None
                    or row["status"] != "running"
                    or row["lease_owner"] != str(worker_id)
                    or int(row["lease_token"] or 0) != int(lease_token)
                ):
                    raise RuntimeError("export job lease lost")
                can_retry = retryable and int(row["attempt"] or 0) < int(row["max_attempts"] or 1)
                status = "queued" if can_retry else "dead_letter"
                available = now + timedelta(seconds=max(0, int(backoff_seconds)))
                completed_at = None if can_retry else _iso(now)
                conn.execute(
                    """UPDATE result_export_jobs SET status = ?, available_at = ?, error_message = ?,
                       lease_owner = NULL, lease_expires_at = NULL, completed_at = ?, updated_at = ?
                       WHERE export_job_id = ?""",
                    (status, _iso(available), str(message or "export failed")[:1000], completed_at, _iso(now), job_id),
                )
                conn.execute(
                    """UPDATE result_export_outbox SET status = ?, attempt = attempt + 1,
                       available_at = ?, last_error = ?, dispatched_at = CASE WHEN ? = 'queued' THEN NULL ELSE dispatched_at END
                       WHERE export_job_id = ?""",
                    ("pending" if can_retry else "failed", _iso(available), str(message or "export failed")[:1000], status, job_id),
                )
                updated = conn.execute("SELECT * FROM result_export_jobs WHERE export_job_id = ?", (job_id,)).fetchone()
                conn.execute("COMMIT")
                return _row_to_job(updated)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def cancel(self, owner_user_id: str | int, job_id: str, reason: str = "user cancelled") -> ExportJob | None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM result_export_jobs WHERE export_job_id = ? AND owner_user_id = ?",
                    (job_id, str(owner_user_id)),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                if row["status"] in {"queued", "running"}:
                    now = _iso()
                    conn.execute(
                        """UPDATE result_export_jobs SET status = 'cancelled', error_message = ?,
                           lease_owner = NULL, lease_expires_at = NULL, completed_at = ?, updated_at = ?
                           WHERE export_job_id = ?""",
                        (str(reason or "user cancelled")[:1000], now, now, job_id),
                    )
                    conn.execute(
                        "UPDATE result_export_outbox SET status = 'cancelled', last_error = ?, dispatched_at = ? WHERE export_job_id = ?",
                        (str(reason or "user cancelled")[:1000], now, job_id),
                    )
                updated = conn.execute("SELECT * FROM result_export_jobs WHERE export_job_id = ?", (job_id,)).fetchone()
                conn.execute("COMMIT")
                return _row_to_job(updated)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def retry(self, owner_user_id: str | int, job_id: str) -> ExportJob | None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM result_export_jobs WHERE export_job_id = ? AND owner_user_id = ?",
                    (job_id, str(owner_user_id)),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                if row["status"] not in {"failed", "dead_letter", "cancelled"}:
                    conn.execute("COMMIT")
                    return _row_to_job(row)
                now = _iso()
                conn.execute(
                    """UPDATE result_export_jobs SET status = 'queued', attempt = 0, error_message = '',
                       lease_owner = NULL, lease_expires_at = NULL, completed_at = NULL, available_at = ?, updated_at = ?
                       WHERE export_job_id = ?""",
                    (now, now, job_id),
                )
                conn.execute(
                    """UPDATE result_export_outbox SET status = 'pending', available_at = ?, last_error = '', dispatched_at = NULL
                       WHERE export_job_id = ?""",
                    (now, job_id),
                )
                updated = conn.execute("SELECT * FROM result_export_jobs WHERE export_job_id = ?", (job_id,)).fetchone()
                conn.execute("COMMIT")
                return _row_to_job(updated)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_artifact(self, owner_user_id: str | int, artifact_id: str) -> Artifact | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM result_artifacts
                   WHERE artifact_id = ? AND owner_user_id = ?
                     AND storage_ref != ''
                     AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))""",
                (artifact_id, str(owner_user_id)),
            ).fetchone()
        return _row_to_artifact(row) if row else None

    def list_artifact_history(
        self,
        owner_user_id: str | int,
        *,
        session_id: str | int | None = None,
        snapshot_id: str | None = None,
        format: str | None = None,
        limit: int = 100,
    ) -> list[ArtifactHistoryEntry]:
        """List immutable artifact metadata, including expired revisions.

        ``get_artifact`` intentionally exposes only downloadable artifacts.
        This companion query is the history surface: retention removes the
        binary and clears its storage reference, while the source/hash/status
        metadata remains available for audit and a UI revision timeline.
        """

        clauses = ["a.owner_user_id = ?"]
        params: list[Any] = [str(owner_user_id)]
        if session_id is not None:
            clauses.append("a.session_id = ?")
            params.append(str(session_id))
        if snapshot_id:
            clauses.append("j.snapshot_id = ?")
            params.append(str(snapshot_id))
        if format:
            normalized_format = normalize_export_format(format)
            clauses.append("a.format = ?")
            params.append(normalized_format)
        params.append(max(1, min(int(limit), 200)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT a.*, j.snapshot_id, s.turn_id
                   FROM result_artifacts a
                   JOIN result_export_jobs j ON j.export_job_id = a.export_job_id
                   JOIN result_snapshots s ON s.snapshot_id = j.snapshot_id
                   WHERE """ + " AND ".join(clauses) +
                " ORDER BY a.created_at DESC, a.artifact_id DESC LIMIT ?",
                params,
            ).fetchall()
        now = _now()
        entries: list[ArtifactHistoryEntry] = []
        for row in rows:
            artifact = _row_to_artifact(row)
            available = bool(artifact.storage_ref)
            if available and artifact.expires_at:
                try:
                    available = datetime.fromisoformat(artifact.expires_at) > now
                except ValueError:
                    available = False
            if available:
                try:
                    path = (self.storage_dir / artifact.storage_ref).resolve()
                    path.relative_to(self.storage_dir)
                    available = path.is_file()
                except (OSError, ValueError):
                    available = False
            entries.append(
                ArtifactHistoryEntry(
                    artifact=artifact,
                    snapshot_id=str(row["snapshot_id"]),
                    turn_id=str(row["turn_id"]),
                    available=available,
                )
            )
        return entries

    def cleanup_expired(self, *, now: datetime | None = None, limit: int = 100) -> list[Artifact]:
        """Delete expired binaries while retaining the job/snapshot audit trail."""
        current = now or _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """SELECT * FROM result_artifacts
                       WHERE expires_at IS NOT NULL AND datetime(expires_at) <= datetime(?)
                         AND storage_ref != ''
                       ORDER BY expires_at, artifact_id LIMIT ?""",
                    (_iso(current), max(1, min(int(limit), 1000))),
                ).fetchall()
                artifacts = [_row_to_artifact(row) for row in rows]
                for artifact in artifacts:
                    # Retain the immutable metadata for history/audit.  An
                    # empty storage_ref makes the row non-downloadable and
                    # prevents the next retention sweep from returning it
                    # repeatedly.
                    conn.execute(
                        "UPDATE result_artifacts SET storage_ref = '', expires_at = ? WHERE artifact_id = ?",
                        (_iso(current), artifact.artifact_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        for artifact in artifacts:
            path = (self.storage_dir / artifact.storage_ref).resolve()
            try:
                path.relative_to(self.storage_dir)
                path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        return artifacts

    def get_snapshot_for_artifact(self, owner_user_id: str | int, artifact_id: str) -> ResultSnapshot | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT s.* FROM result_snapshots s
                   JOIN result_export_jobs j ON j.snapshot_id = s.snapshot_id
                   JOIN result_artifacts a ON a.export_job_id = j.export_job_id
                   WHERE a.artifact_id = ? AND a.owner_user_id = ?""",
                (artifact_id, str(owner_user_id)),
            ).fetchone()
        return _row_to_snapshot(row) if row else None

    def read_artifact(self, owner_user_id: str | int, artifact_id: str) -> bytes:
        artifact = self.get_artifact(owner_user_id, artifact_id)
        if artifact is None:
            raise KeyError("artifact not found")
        if artifact.expires_at:
            try:
                if datetime.fromisoformat(artifact.expires_at) <= _now():
                    raise KeyError("artifact expired")
            except ValueError:
                pass
        path = (self.storage_dir / artifact.storage_ref).resolve()
        try:
            path.relative_to(self.storage_dir)
        except ValueError as exc:
            raise PermissionError("artifact storage reference is outside export storage") from exc
        try:
            if not artifact.storage_ref:
                raise KeyError("artifact file is unavailable")
            content = path.read_bytes()
        except OSError as exc:
            raise KeyError("artifact file is unavailable") from exc
        if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ValueError("artifact integrity check failed")
        return content

    def _get_job_unscoped(self, job_id: str) -> ExportJob | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM result_export_jobs WHERE export_job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None


def _row_to_snapshot(row: sqlite3.Row) -> ResultSnapshot:
    envelope = ResultEnvelope.from_dict(_load(row["envelope_json"], {}))
    source_hash = str(row["source_hash"] or "") if "source_hash" in row.keys() else ""
    if source_hash:
        actual_hash = hashlib.sha256(
            _json(envelope.normalized().to_dict()).encode("utf-8")
        ).hexdigest()
        if actual_hash != source_hash:
            raise ValueError("result snapshot integrity check failed")
    return ResultSnapshot(
        snapshot_id=row["snapshot_id"],
        owner_user_id=str(row["owner_user_id"]),
        tenant_id=str(row["tenant_id"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
        envelope=envelope,
        created_at=row["created_at"],
        schema_version=str(row["schema_version"] or "v1") if "schema_version" in row.keys() else "v1",
        source_hash=source_hash,
        department_id=(str(row["department_id"]) if row["department_id"] not in (None, "") else None)
        if "department_id" in row.keys() else None,
        knowledge_base_name=str(row["knowledge_base_name"] or "") if "knowledge_base_name" in row.keys() else "",
        assistant_message_id=(int(row["assistant_message_id"]) if row["assistant_message_id"] is not None else None)
        if "assistant_message_id" in row.keys() else None,
    )


def _row_to_job(row: sqlite3.Row) -> ExportJob:
    return ExportJob(
        export_job_id=row["export_job_id"],
        snapshot_id=row["snapshot_id"],
        owner_user_id=str(row["owner_user_id"]),
        tenant_id=str(row["tenant_id"]),
        session_id=str(row["session_id"]),
        format=row["format"],
        content_shape=row["content_shape"],
        client_request_id=row["client_request_id"],
        options=_load(row["options_json"], {}) if isinstance(_load(row["options_json"], {}), dict) else {},
        status=row["status"],
        attempt=int(row["attempt"] or 0),
        max_attempts=int(row["max_attempts"] or 1),
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_token=int(row["lease_token"] or 0),
        lease_expires_at=row["lease_expires_at"],
        artifact_id=row["artifact_id"],
        error_message=row["error_message"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        department_id=(str(row["department_id"]) if row["department_id"] not in (None, "") else None)
        if "department_id" in row.keys() else None,
        knowledge_base_name=str(row["knowledge_base_name"] or "") if "knowledge_base_name" in row.keys() else "",
        assistant_message_id=(int(row["assistant_message_id"]) if row["assistant_message_id"] is not None else None)
        if "assistant_message_id" in row.keys() else None,
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    preview = _load(row["preview_json"], {})
    return Artifact(
        artifact_id=row["artifact_id"],
        export_job_id=row["export_job_id"],
        owner_user_id=str(row["owner_user_id"]),
        session_id=str(row["session_id"]),
        format=row["format"],
        filename=row["filename"],
        mime_type=row["mime_type"],
        size=int(row["size"] or 0),
        sha256=row["sha256"],
        storage_ref=row["storage_ref"],
        preview=preview if isinstance(preview, dict) else {},
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        tenant_id=str(row["tenant_id"] or "default") if "tenant_id" in row.keys() else "default",
        department_id=(str(row["department_id"]) if row["department_id"] not in (None, "") else None)
        if "department_id" in row.keys() else None,
        knowledge_base_name=str(row["knowledge_base_name"] or "") if "knowledge_base_name" in row.keys() else "",
    )
