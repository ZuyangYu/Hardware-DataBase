"""Durable attachment records (SQLite, attachments-scoped database).

Tables (design doc §4):

- ``chat_attachments``      user-visible upload records (idempotency key)
- ``chat_attachment_assets`` physical file + parse facts (asset reuse)
- ``chat_attachment_parts``  canonical parsed chunks
- ``chat_attachment_jobs``   lease-claimed background parse jobs

Schema follows the repo convention: ``CREATE TABLE IF NOT EXISTS`` executed at
store construction, additive-only migrations, WAL + NORMAL sync, and partial
unique indexes for idempotency (mirroring ``chat_turns`` / ``memory_jobs``).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

import src.settings
from src.attachments.models import (
    AttachmentAsset,
    AttachmentJob,
    AttachmentPart,
    AttachmentRecord,
    PARSER_VERSION,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _attachment_expires_at(now: str) -> str:
    """Calculate the per-upload retention deadline from live settings."""
    try:
        retention_seconds = int(getattr(src.settings, "CHAT_ATTACHMENT_RETENTION_SECONDS", 0))
    except (TypeError, ValueError):
        retention_seconds = 0
    if retention_seconds <= 0:
        return now
    return (datetime.now(timezone.utc) + timedelta(seconds=retention_seconds)).isoformat()


class AttachmentStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH
        parent = self.db_path.rsplit("/", 1)[0] if "/" in self.db_path else "."
        import os

        os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_attachments (
                        attachment_id TEXT PRIMARY KEY,
                        session_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        asset_id TEXT NOT NULL,
                        client_request_id TEXT,
                        filename TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        extension TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        sha256 TEXT NOT NULL,
                        usage_hint TEXT NOT NULL DEFAULT 'reference',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        expires_at TEXT,
                        deleted_at TEXT
                    )
                    """
                )
                # Design §4.1: one attachment per (session, client_request_id).
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_attachments_session_request
                    ON chat_attachments(session_id, client_request_id)
                    WHERE client_request_id IS NOT NULL
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_attachments_session_status
                    ON chat_attachments(session_id, status, created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_attachment_assets (
                        asset_id TEXT PRIMARY KEY,
                        session_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        sha256 TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        extension TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        storage_key TEXT NOT NULL,
                        parse_status TEXT NOT NULL DEFAULT 'queued',
                        parser_version TEXT NOT NULL DEFAULT '',
                        manifest_json TEXT NOT NULL DEFAULT '{}',
                        error_code TEXT NOT NULL DEFAULT '',
                        error_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                # Design §4.2: dedupe is per (tenant, user, session, sha256).
                # Cross-user/cross-session dedupe is deliberately NOT enforced.
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_attachment_assets_content
                    ON chat_attachment_assets(tenant_id, user_id, session_id, sha256)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_attachment_parts (
                        part_id TEXT PRIMARY KEY,
                        asset_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        part_type TEXT NOT NULL,
                        text_content TEXT NOT NULL DEFAULT '',
                        locator_json TEXT NOT NULL DEFAULT '{}',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        content_hash TEXT NOT NULL DEFAULT '',
                        parser_version TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_attachment_parts_asset
                    ON chat_attachment_parts(asset_id, ordinal)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_attachment_embeddings (
                        asset_id TEXT NOT NULL,
                        part_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        content_hash TEXT NOT NULL DEFAULT '',
                        vector_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(asset_id, part_id, provider, model)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_attachment_embeddings_asset
                    ON chat_attachment_embeddings(asset_id, provider, model)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_attachment_visual_cache (
                        asset_id TEXT NOT NULL,
                        page_number INTEGER NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        question_hash TEXT NOT NULL,
                        content TEXT NOT NULL,
                        request_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(asset_id, page_number, provider, model, question_hash)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_attachment_visual_cache_asset
                    ON chat_attachment_visual_cache(asset_id, page_number)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_attachment_jobs (
                        job_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        user_id INTEGER NOT NULL,
                        session_id INTEGER NOT NULL,
                        asset_id TEXT NOT NULL,
                        job_type TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'queued',
                        attempt INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        lease_owner TEXT NOT NULL DEFAULT '',
                        lease_expires_at TEXT,
                        parser_version TEXT NOT NULL DEFAULT '',
                        error_code TEXT NOT NULL DEFAULT '',
                        error_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    )
                    """
                )
                # Idempotency (design §4.4): one live job per (asset, type).
                # Retries reuse the same row; a completed job rows stays as the
                # provenance record for its parser_version.
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_attachment_jobs_live
                    ON chat_attachment_jobs(asset_id, job_type)
                    WHERE status IN ('queued', 'running')
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_attachment_jobs_pending
                    ON chat_attachment_jobs(status, created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_cleanup_outbox (
                        outbox_id TEXT PRIMARY KEY,
                        session_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        status TEXT NOT NULL DEFAULT 'pending',
                        targets_json TEXT NOT NULL DEFAULT '{}',
                        idempotency_key TEXT NOT NULL UNIQUE,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        max_retries INTEGER NOT NULL DEFAULT 5,
                        lease_owner TEXT NOT NULL DEFAULT '',
                        lease_expires_at TEXT,
                        last_error TEXT NOT NULL DEFAULT '',
                        next_retry_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_session_cleanup_outbox_pending
                    ON session_cleanup_outbox(status, next_retry_at, created_at)
                    """
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # -- attachments -------------------------------------------------------

    def create_attachment(
        self,
        *,
        session_id: int,
        user_id: int,
        asset_id: str,
        client_request_id: str | None,
        filename: str,
        media_type: str,
        extension: str,
        size_bytes: int,
        sha256: str,
        usage_hint: str = "reference",
        tenant_id: str = "default",
    ) -> tuple[AttachmentRecord, bool]:
        """Insert an attachment row; return (record, created).

        On a duplicate (session, client_request_id) the existing row is
        returned with ``created=False`` so uploads are idempotent.
        """
        now = utc_now()
        expires_at = _attachment_expires_at(now)
        attachment_id = _new_id("att")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if client_request_id:
                    existing = conn.execute(
                        """
                        SELECT * FROM chat_attachments
                        WHERE session_id = ? AND client_request_id = ?
                        """,
                        (session_id, client_request_id),
                    ).fetchone()
                    if existing is not None:
                        conn.execute("COMMIT")
                        return self._row_to_attachment(existing), False
                conn.execute(
                    """
                    INSERT INTO chat_attachments (
                        attachment_id, session_id, user_id, tenant_id, asset_id,
                        client_request_id, filename, media_type, extension,
                        size_bytes, sha256, usage_hint, status, created_at, updated_at,
                        expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        attachment_id, session_id, user_id, tenant_id, asset_id,
                        client_request_id, filename, media_type, extension,
                        size_bytes, sha256, usage_hint, now, now, expires_at,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM chat_attachments WHERE attachment_id = ?",
                    (attachment_id,),
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_attachment(row), True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_attachment(
        self,
        *,
        attachment_id: str,
        session_id: int | None = None,
        user_id: int | None = None,
        include_deleted: bool = False,
    ) -> AttachmentRecord | None:
        clauses = ["att.attachment_id = ?"]
        params: list[Any] = [attachment_id]
        if session_id is not None:
            clauses.append("att.session_id = ?")
            params.append(session_id)
        if user_id is not None:
            clauses.append("att.user_id = ?")
            params.append(user_id)
        if not include_deleted:
            clauses.append("att.status = 'active'")
            clauses.append("(att.expires_at IS NULL OR datetime(att.expires_at) > datetime(?))")
            params.append(utc_now())
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""
                SELECT att.*, a.parse_status AS asset_parse_status,
                       a.error_code AS asset_error_code,
                       a.error_message AS asset_error_message,
                       a.manifest_json AS asset_manifest_json
                FROM chat_attachments att
                LEFT JOIN chat_attachment_assets a ON a.asset_id = att.asset_id
                WHERE {' AND '.join(clauses)}
                """,
                params,
            ).fetchone()
        return self._row_to_attachment(row) if row else None

    def get_attachment_by_client_request_id(
        self,
        *,
        session_id: int,
        user_id: int,
        client_request_id: str,
        include_deleted: bool = False,
    ) -> AttachmentRecord | None:
        """Find an idempotent upload before consuming its request body.

        The session and user predicates keep this lookup within the same ACL
        boundary as the normal attachment reads.  Deleted rows remain part of
        the idempotency history when ``include_deleted`` is requested, because
        the database uniqueness rule intentionally preserves the request key.
        """
        clauses = [
            "att.session_id = ?",
            "att.user_id = ?",
            "att.client_request_id = ?",
        ]
        params: list[Any] = [session_id, user_id, client_request_id]
        if not include_deleted:
            clauses.append("att.status = 'active'")
            clauses.append("(att.expires_at IS NULL OR datetime(att.expires_at) > datetime(?))")
            params.append(utc_now())
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""
                SELECT att.*, a.parse_status AS asset_parse_status,
                       a.error_code AS asset_error_code,
                       a.error_message AS asset_error_message,
                       a.manifest_json AS asset_manifest_json
                FROM chat_attachments att
                LEFT JOIN chat_attachment_assets a ON a.asset_id = att.asset_id
                WHERE {' AND '.join(clauses)}
                """,
                params,
            ).fetchone()
        return self._row_to_attachment(row) if row else None

    def list_attachments(
        self,
        *,
        session_id: int,
        user_id: int,
        include_deleted: bool = False,
    ) -> list[AttachmentRecord]:
        clauses = ["att.session_id = ?", "att.user_id = ?"]
        params: list[Any] = [session_id, user_id]
        if not include_deleted:
            clauses.append("att.status = 'active'")
            clauses.append("(att.expires_at IS NULL OR datetime(att.expires_at) > datetime(?))")
            params.append(utc_now())
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT att.*, a.parse_status AS asset_parse_status,
                       a.error_code AS asset_error_code,
                       a.error_message AS asset_error_message,
                       a.manifest_json AS asset_manifest_json
                FROM chat_attachments att
                LEFT JOIN chat_attachment_assets a ON a.asset_id = att.asset_id
                WHERE {' AND '.join(clauses)}
                ORDER BY att.created_at, att.attachment_id
                """,
                params,
            ).fetchall()
        return [self._row_to_attachment(row) for row in rows]

    def soft_delete_attachment(
        self, *, attachment_id: str, session_id: int, user_id: int
    ) -> bool:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    UPDATE chat_attachments
                    SET status = 'deleted', deleted_at = ?, updated_at = ?
                    WHERE attachment_id = ? AND session_id = ? AND user_id = ?
                      AND status = 'active'
                      AND (expires_at IS NULL OR datetime(expires_at) > datetime(?))
                    """,
                    (now, now, attachment_id, session_id, user_id, now),
                )
                conn.execute("COMMIT")
                return int(cursor.rowcount or 0) > 0
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def expire_due_attachments(
        self, *, limit: int = 200, now: str | None = None
    ) -> list[tuple[str, str]]:
        """Mark due active rows expired and return ``(attachment, asset)`` pairs."""
        current = now or utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT attachment_id, asset_id FROM chat_attachments
                    WHERE status = 'active' AND expires_at IS NOT NULL
                      AND datetime(expires_at) <= datetime(?)
                    ORDER BY expires_at, attachment_id
                    LIMIT ?
                    """,
                    (current, max(1, min(int(limit), 1000))),
                ).fetchall()
                if rows:
                    ids = [row["attachment_id"] for row in rows]
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"""
                        UPDATE chat_attachments
                        SET status = 'expired', deleted_at = ?, updated_at = ?
                        WHERE attachment_id IN ({placeholders}) AND status = 'active'
                        """,
                        (current, current, *ids),
                    )
                conn.execute("COMMIT")
                return [(row["attachment_id"], row["asset_id"]) for row in rows]
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def count_active_attachments(self, *, session_id: int, user_id: int) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM chat_attachments
                WHERE session_id = ? AND user_id = ? AND status = 'active'
                  AND (expires_at IS NULL OR datetime(expires_at) > datetime(?))
                """,
                (session_id, user_id, utc_now()),
            ).fetchone()
        return int(row["n"] or 0)

    def sum_active_attachment_bytes(
        self,
        *,
        session_id: int | None = None,
        user_id: int | None = None,
        tenant_id: str | None = None,
    ) -> int:
        """Return live attachment bytes for a session or tenant quota."""
        clauses = [
            "status = 'active'",
            "(expires_at IS NULL OR datetime(expires_at) > datetime(?))",
        ]
        params: list[Any] = [utc_now()]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(int(session_id))
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(int(user_id))
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(str(tenant_id))
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(size_bytes), 0) AS total FROM chat_attachments WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return int(row["total"] or 0)

    def asset_reference_count(self, *, asset_id: str) -> int:
        """Active attachment rows still pointing at the asset."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM chat_attachments
                WHERE asset_id = ? AND status = 'active'
                  AND (expires_at IS NULL OR datetime(expires_at) > datetime(?))
                """,
                (asset_id, utc_now()),
            ).fetchone()
        return int(row["n"] or 0)

    def delete_asset(self, asset_id: str) -> bool:
        """Remove an unreferenced asset and its parse-job rows atomically."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM chat_attachment_jobs WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_parts WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_embeddings WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_visual_cache WHERE asset_id = ?", (asset_id,))
                cursor = conn.execute(
                    "DELETE FROM chat_attachment_assets WHERE asset_id = ?",
                    (asset_id,),
                )
                conn.execute("COMMIT")
                return int(cursor.rowcount or 0) > 0
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def sync_attachment_parse_state(self, *, asset_id: str) -> None:
        """Deprecated no-op retained for API symmetry.

        Parse state is joined from the asset at read time (single source of
        truth); no denormalized copy exists on attachment rows.
        """
        return None

    # -- assets ------------------------------------------------------------

    def find_asset(
        self, *, tenant_id: str, user_id: int, session_id: int, sha256: str
    ) -> AttachmentAsset | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM chat_attachment_assets
                WHERE tenant_id = ? AND user_id = ? AND session_id = ? AND sha256 = ?
                """,
                (tenant_id, user_id, session_id, sha256),
            ).fetchone()
        return self._row_to_asset(row) if row else None

    def get_asset(self, asset_id: str) -> AttachmentAsset | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM chat_attachment_assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return self._row_to_asset(row) if row else None

    def create_asset(
        self,
        *,
        session_id: int,
        user_id: int,
        sha256: str,
        media_type: str,
        extension: str,
        size_bytes: int,
        storage_key: str,
        tenant_id: str = "default",
    ) -> tuple[AttachmentAsset, bool]:
        """Insert an asset; return (asset, created).

        On a duplicate (tenant, user, session, sha256) the existing asset is
        returned so identical files in one session share parse work.
        """
        now = utc_now()
        asset_id = _new_id("asset")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT * FROM chat_attachment_assets
                    WHERE tenant_id = ? AND user_id = ? AND session_id = ? AND sha256 = ?
                    """,
                    (tenant_id, user_id, session_id, sha256),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return self._row_to_asset(existing), False
                conn.execute(
                    """
                    INSERT INTO chat_attachment_assets (
                        asset_id, session_id, user_id, tenant_id, sha256,
                        media_type, extension, size_bytes, storage_key,
                        parse_status, parser_version, manifest_json,
                        error_code, error_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', '', '{}', '', '', ?, ?)
                    """,
                    (
                        asset_id, session_id, user_id, tenant_id, sha256,
                        media_type, extension, size_bytes, storage_key, now, now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM chat_attachment_assets WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_asset(row), True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def update_asset_status(
        self,
        asset_id: str,
        *,
        parse_status: str,
        parser_version: str | None = None,
        manifest: dict[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE chat_attachment_assets
                    SET parse_status = ?, error_code = ?, error_message = ?, updated_at = ?
                    WHERE asset_id = ?
                    """,
                    (parse_status, error_code, error_message[:1000], now, asset_id),
                )
                if parser_version is not None:
                    conn.execute(
                        "UPDATE chat_attachment_assets SET parser_version = ? WHERE asset_id = ?",
                        (parser_version, asset_id),
                    )
                if manifest is not None:
                    conn.execute(
                        """
                        UPDATE chat_attachment_assets SET manifest_json = ?
                        WHERE asset_id = ?
                        """,
                        (
                            json.dumps(manifest, ensure_ascii=False, default=str),
                            asset_id,
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # -- parts -------------------------------------------------------------

    def replace_parts(self, asset_id: str, parts: list[AttachmentPart]) -> None:
        """Atomically swap the parsed parts of an asset (rebuild-safe)."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM chat_attachment_parts WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_embeddings WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM chat_attachment_visual_cache WHERE asset_id = ?", (asset_id,))
                for part in parts:
                    conn.execute(
                        """
                        INSERT INTO chat_attachment_parts (
                            part_id, asset_id, ordinal, part_type, text_content,
                            locator_json, metadata_json, content_hash, parser_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            part.part_id or _new_id("part"),
                            asset_id,
                            int(part.ordinal),
                            part.part_type,
                            part.text_content,
                            json.dumps(part.locator, ensure_ascii=False, default=str),
                            json.dumps(part.metadata, ensure_ascii=False, default=str),
                            part.content_hash,
                            part.parser_version or PARSER_VERSION,
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_parts(self, asset_id: str) -> list[AttachmentPart]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM chat_attachment_parts WHERE asset_id = ?
                ORDER BY ordinal
                """,
                (asset_id,),
            ).fetchall()
        return [
            AttachmentPart(
                part_id=row["part_id"],
                asset_id=row["asset_id"],
                ordinal=int(row["ordinal"]),
                part_type=row["part_type"],
                text_content=row["text_content"] or "",
                locator=json.loads(row["locator_json"] or "{}"),
                metadata=json.loads(row["metadata_json"] or "{}"),
                content_hash=row["content_hash"] or "",
                parser_version=row["parser_version"] or "",
            )
            for row in rows
        ]

    def list_embeddings(
        self,
        *,
        asset_ids: list[str],
        provider: str,
        model: str,
    ) -> dict[str, tuple[str, list[float]]]:
        """Return cached vectors keyed by part id within an asset allow-list."""
        normalized_assets = list(dict.fromkeys(str(item) for item in asset_ids if str(item)))
        if not normalized_assets:
            return {}
        placeholders = ",".join("?" for _ in normalized_assets)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT part_id, content_hash, vector_json
                FROM chat_attachment_embeddings
                WHERE asset_id IN ({placeholders}) AND provider = ? AND model = ?
                """,
                (*normalized_assets, provider, model),
            ).fetchall()
        vectors: dict[str, tuple[str, list[float]]] = {}
        for row in rows:
            try:
                vector = json.loads(row["vector_json"] or "[]")
                if not isinstance(vector, list):
                    continue
                vectors[row["part_id"]] = (
                    row["content_hash"] or "",
                    [float(value) for value in vector],
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return vectors

    def save_embeddings(
        self,
        *,
        asset_id: str,
        provider: str,
        model: str,
        embeddings: list[tuple[str, str, list[float]]],
    ) -> None:
        """Upsert vectors for one asset and embedding configuration."""
        if not embeddings:
            return
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for part_id, content_hash, vector in embeddings:
                    conn.execute(
                        """
                        INSERT INTO chat_attachment_embeddings (
                            asset_id, part_id, provider, model, content_hash,
                            vector_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(asset_id, part_id, provider, model)
                        DO UPDATE SET content_hash = excluded.content_hash,
                                      vector_json = excluded.vector_json,
                                      updated_at = excluded.updated_at
                        """,
                        (
                            asset_id,
                            part_id,
                            provider,
                            model,
                            content_hash,
                            json.dumps([float(value) for value in vector]),
                            now,
                            now,
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_visual_cache(
        self,
        *,
        asset_id: str,
        page_number: int,
        provider: str,
        model: str,
        question_hash: str,
    ) -> dict[str, str] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT content, request_id FROM chat_attachment_visual_cache
                WHERE asset_id = ? AND page_number = ? AND provider = ? AND model = ?
                  AND question_hash = ?
                """,
                (asset_id, int(page_number), provider, model, question_hash),
            ).fetchone()
        if row is None:
            return None
        return {"content": row["content"] or "", "request_id": row["request_id"] or ""}

    def save_visual_cache(
        self,
        *,
        asset_id: str,
        page_number: int,
        provider: str,
        model: str,
        question_hash: str,
        content: str,
        request_id: str = "",
    ) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO chat_attachment_visual_cache (
                    asset_id, page_number, provider, model, question_hash,
                    content, request_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id, page_number, provider, model, question_hash)
                DO UPDATE SET content = excluded.content,
                              request_id = excluded.request_id,
                              updated_at = excluded.updated_at
                """,
                (
                    asset_id,
                    int(page_number),
                    provider,
                    model,
                    question_hash,
                    str(content)[:12000],
                    str(request_id)[:200],
                    now,
                    now,
                ),
            )

    # -- jobs --------------------------------------------------------------

    def enqueue_parse_job(
        self,
        *,
        asset_id: str,
        session_id: int,
        user_id: int,
        parser_version: str = PARSER_VERSION,
        payload: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> tuple[AttachmentJob, bool]:
        """Enqueue a parse job; a live job for the same asset returns as-is."""
        now = utc_now()
        job_id = _new_id("job")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT * FROM chat_attachment_jobs
                    WHERE asset_id = ? AND job_type = 'parse'
                      AND status IN ('queued', 'running')
                    """,
                    (asset_id,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return self._row_to_job(existing), False
                conn.execute(
                    """
                    INSERT INTO chat_attachment_jobs (
                        job_id, tenant_id, user_id, session_id, asset_id, job_type,
                        status, attempt, max_attempts, payload_json, parser_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'parse', 'queued', 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id, tenant_id, user_id, session_id, asset_id,
                        max(1, int(src.settings.CHAT_ATTACHMENT_JOB_MAX_ATTEMPTS)),
                        json.dumps(payload or {}, ensure_ascii=False),
                        parser_version, now, now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM chat_attachment_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_job(row), True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_pending_jobs(self, limit: int = 8) -> list[AttachmentJob]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM chat_attachment_jobs
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_expires_at IS NOT NULL
                       AND datetime(lease_expires_at) < datetime('now'))
                ORDER BY created_at, job_id
                LIMIT ?
                """,
                (max(1, min(int(limit), 64)),),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def claim_job(
        self, job_id: str, worker_id: str, lease_seconds: int | None = None
    ) -> AttachmentJob | None:
        now = utc_now()
        lease = max(15, int(lease_seconds or src.settings.CHAT_ATTACHMENT_JOB_LEASE_SECONDS))
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease)).isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    UPDATE chat_attachment_jobs
                    SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                        attempt = attempt + 1, updated_at = ?
                    WHERE job_id = ?
                      AND (status = 'queued'
                           OR (status = 'running' AND lease_expires_at IS NOT NULL
                               AND datetime(lease_expires_at) < datetime('now')))
                      AND attempt < max_attempts
                    """,
                    (worker_id, expires, now, job_id),
                )
                if int(cursor.rowcount or 0) == 0:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """
                    UPDATE chat_attachment_assets
                    SET parse_status = 'running', updated_at = ?
                    WHERE asset_id = (
                        SELECT asset_id FROM chat_attachment_jobs WHERE job_id = ?
                    )
                    """,
                    (now, job_id),
                )
                row = conn.execute(
                    "SELECT * FROM chat_attachment_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_job(row)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def heartbeat_job(self, job_id: str, worker_id: str, lease_seconds: int) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=max(15, int(lease_seconds)))).isoformat()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE chat_attachment_jobs SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND lease_owner = ? AND status = 'running'
                """,
                (expires, utc_now(), job_id, worker_id),
            )

    def complete_job(
        self, job_id: str, worker_id: str, result: dict[str, Any] | None = None
    ) -> None:
        now = utc_now()
        result = result or {}
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    UPDATE chat_attachment_jobs
                    SET status = 'completed', result_json = ?, lease_owner = '',
                        lease_expires_at = NULL, completed_at = ?, updated_at = ?
                    WHERE job_id = ? AND lease_owner = ? AND status = 'running'
                    """,
                    (
                        json.dumps(result or {}, ensure_ascii=False, default=str),
                        now, now, job_id, worker_id,
                    ),
                )
                parse_status = str(result.get("parse_status") or "").strip()
                if int(cursor.rowcount or 0) > 0 and parse_status in {
                    "queued", "running", "ready", "degraded", "failed"
                }:
                    conn.execute(
                        """
                        UPDATE chat_attachment_assets
                        SET parse_status = ?,
                            error_code = CASE WHEN ? IN ('ready', 'degraded') THEN '' ELSE error_code END,
                            error_message = CASE WHEN ? IN ('ready', 'degraded') THEN '' ELSE error_message END,
                            updated_at = ?
                        WHERE asset_id = (
                            SELECT asset_id FROM chat_attachment_jobs WHERE job_id = ?
                        )
                        """,
                        (parse_status, parse_status, parse_status, now, job_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def fail_job(
        self, job_id: str, worker_id: str, error_code: str, error_message: str
    ) -> None:
        """Mark a failed attempt; retries requeue until max_attempts, then dead-letter."""
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT j.asset_id, j.attempt, j.max_attempts,
                           a.parse_status AS asset_parse_status
                    FROM chat_attachment_jobs j
                    LEFT JOIN chat_attachment_assets a ON a.asset_id = j.asset_id
                    WHERE j.job_id = ? AND j.lease_owner = ?
                    """,
                    (job_id, worker_id),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return
                exhausted = int(row["attempt"]) >= int(row["max_attempts"])
                status = "failed" if exhausted else "queued"
                conn.execute(
                    """
                    UPDATE chat_attachment_jobs
                    SET status = ?, error_code = ?, error_message = ?, lease_owner = '',
                        lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, error_code, error_message[:1000], now, job_id),
                )
                # The executor records deterministic parser/storage failures
                # before returning a failed result. Preserve that terminal
                # state instead of hiding it as a retryable queued state. A
                # worker exception leaves the asset in running and is the
                # case that should be requeued until the attempt budget is
                # exhausted.
                if row["asset_parse_status"] in {"queued", "running"}:
                    conn.execute(
                        """
                        UPDATE chat_attachment_assets
                        SET parse_status = ?, error_code = ?, error_message = ?, updated_at = ?
                        WHERE asset_id = ?
                        """,
                        (
                            "failed" if exhausted else "queued",
                            error_code,
                            error_message[:1000],
                            now,
                            row["asset_id"],
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_job(self, job_id: str) -> AttachmentJob | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM chat_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def cancel_session_jobs(self, *, session_id: int) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE chat_attachment_jobs
                SET status = 'failed', error_code = 'session_deleted',
                    error_message = 'session deleted before parse completed',
                    lease_owner = '', lease_expires_at = NULL, updated_at = ?
                WHERE session_id = ? AND status IN ('queued', 'running')
                """,
                (utc_now(), session_id),
            )
            return int(cursor.rowcount or 0)

    # -- cleanup outbox ----------------------------------------------------

    def enqueue_session_cleanup(
        self, *, session_id: int, user_id: int, tenant_id: str = "default",
        targets: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        key = f"session-cleanup-{session_id}"
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT 1 FROM session_cleanup_outbox WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    # Re-open the entry if a retry is still needed.
                    conn.execute(
                        """
                        UPDATE session_cleanup_outbox
                        SET status = 'pending', next_retry_at = NULL, updated_at = ?
                        WHERE idempotency_key = ? AND status IN ('pending', 'retrying', 'running')
                        """,
                        (now, key),
                    )
                    conn.execute("COMMIT")
                    return False
                conn.execute(
                    """
                    INSERT INTO session_cleanup_outbox (
                        outbox_id, session_id, user_id, tenant_id, status,
                        targets_json, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        _new_id("out"), session_id, user_id, tenant_id,
                        json.dumps(targets or {}, ensure_ascii=False),
                        key, now, now,
                    ),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_pending_cleanups(self, limit: int = 8) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM session_cleanup_outbox
                WHERE (status = 'pending')
                   OR (status = 'retrying' AND next_retry_at IS NOT NULL
                       AND datetime(next_retry_at) <= datetime('now'))
                   OR (status = 'running' AND lease_expires_at IS NOT NULL
                       AND datetime(lease_expires_at) < datetime('now'))
                ORDER BY created_at
                LIMIT ?
                """,
                (max(1, min(int(limit), 64)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_cleanup(self, outbox_id: str, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        now = utc_now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=max(15, int(lease_seconds)))).isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    UPDATE session_cleanup_outbox
                    SET status = 'running', lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE outbox_id = ?
                      AND (status = 'pending'
                           OR (status = 'retrying' AND next_retry_at IS NOT NULL
                               AND datetime(next_retry_at) <= datetime('now'))
                           OR (status = 'running' AND lease_expires_at IS NOT NULL
                               AND datetime(lease_expires_at) < datetime('now')))
                    """,
                    (worker_id, expires, now, outbox_id),
                )
                if int(cursor.rowcount or 0) == 0:
                    conn.execute("COMMIT")
                    return None
                row = conn.execute(
                    "SELECT * FROM session_cleanup_outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return dict(row) if row else None
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def complete_cleanup(self, outbox_id: str, worker_id: str) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE session_cleanup_outbox
                SET status = 'completed', completed_at = ?, lease_owner = '',
                    lease_expires_at = NULL, updated_at = ?
                WHERE outbox_id = ? AND lease_owner = ? AND status = 'running'
                """,
                (now, now, outbox_id, worker_id),
            )

    def retry_cleanup(self, outbox_id: str, worker_id: str, error_message: str) -> bool:
        """Requeue a failed cleanup; dead-letter after max_retries."""
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT retry_count, max_retries FROM session_cleanup_outbox WHERE outbox_id = ? AND lease_owner = ?",
                    (outbox_id, worker_id),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return False
                exhausted = int(row["retry_count"]) + 1 >= int(row["max_retries"])
                delay_seconds = min(600, 30 * (2 ** min(int(row["retry_count"]), 4)))
                next_retry = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
                ).isoformat()
                conn.execute(
                    """
                    UPDATE session_cleanup_outbox
                    SET status = ?, retry_count = retry_count + 1, last_error = ?,
                        next_retry_at = ?, lease_owner = '', lease_expires_at = NULL, updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (
                        "dead_letter" if exhausted else "retrying",
                        error_message[:1000],
                        None if exhausted else next_retry,
                        now, outbox_id,
                    ),
                )
                conn.execute("COMMIT")
                return not exhausted
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # -- row mappers -------------------------------------------------------

    @staticmethod
    def _row_to_attachment(row) -> AttachmentRecord:
        try:
            manifest = json.loads(row["asset_manifest_json"] or "{}")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            manifest = {}
        return AttachmentRecord(
            attachment_id=row["attachment_id"],
            session_id=int(row["session_id"]),
            user_id=int(row["user_id"]),
            tenant_id=row["tenant_id"],
            asset_id=row["asset_id"],
            client_request_id=row["client_request_id"],
            filename=row["filename"],
            media_type=row["media_type"],
            extension=row["extension"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            usage_hint=row["usage_hint"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            deleted_at=row["deleted_at"],
            # Denormalized display fields come from the LEFT JOIN in get/list.
            parse_status=row["asset_parse_status"] if "asset_parse_status" in row.keys() else "queued",
            error_code=row["asset_error_code"] if "asset_error_code" in row.keys() else "",
            error_message=row["asset_error_message"] if "asset_error_message" in row.keys() else "",
            manifest=manifest if isinstance(manifest, dict) else {},
        )

    @staticmethod
    def _row_to_asset(row) -> AttachmentAsset:
        try:
            manifest = json.loads(row["manifest_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            manifest = {}
        return AttachmentAsset(
            asset_id=row["asset_id"],
            session_id=int(row["session_id"]),
            user_id=int(row["user_id"]),
            tenant_id=row["tenant_id"],
            sha256=row["sha256"],
            media_type=row["media_type"],
            extension=row["extension"],
            size_bytes=int(row["size_bytes"]),
            storage_key=row["storage_key"],
            parse_status=row["parse_status"],
            parser_version=row["parser_version"] or "",
            manifest=manifest if isinstance(manifest, dict) else {},
            error_code=row["error_code"] or "",
            error_message=row["error_message"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_job(row) -> AttachmentJob:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        try:
            result = json.loads(row["result_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result = {}
        return AttachmentJob(
            job_id=row["job_id"],
            tenant_id=row["tenant_id"],
            user_id=int(row["user_id"]),
            session_id=int(row["session_id"]),
            asset_id=row["asset_id"],
            job_type=row["job_type"],
            status=row["status"],
            attempt=int(row["attempt"] or 0),
            max_attempts=int(row["max_attempts"] or 3),
            payload=payload if isinstance(payload, dict) else {},
            result=result if isinstance(result, dict) else {},
            lease_owner=row["lease_owner"] or "",
            lease_expires_at=row["lease_expires_at"],
            parser_version=row["parser_version"] or "",
            error_code=row["error_code"] or "",
            error_message=row["error_message"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )
