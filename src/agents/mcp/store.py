"""SQLite store for external MCP plugin configuration and user subscriptions.

The table lives in ``AUTH_DB_PATH`` next to users/departments/audit_events:
MCP configuration is governance metadata and must not be split across the
conversation control plane.  Credentials are encrypted with
``MCP_CREDENTIALS_KEY`` before insert; a missing or invalid key fails closed
when headers are present, and a later key rotation degrades only the affected
server (``headers_error``) instead of breaking the whole catalog.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Protocol

import src.settings as settings

from src.agents.mcp.models import McpServerRecord, McpSubscription


class McpCredentialError(RuntimeError):
    """Stored MCP credentials cannot be encrypted/decrypted safely."""


class McpServerNotFoundError(ValueError):
    """The requested slug does not exist."""


class McpServerExistsError(ValueError):
    """The requested slug already exists."""


# Public sentinel: "field omitted, keep the stored value".  The API layer maps
# missing JSON fields to this value; ``None`` means "clear" for nullable
# fields and "clear credentials" for ``headers``.
UNSET: Any = object()

_JSON_LIST = "[]"
_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalize_int_list(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _normalize_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): str(val) for key, val in value.items() if str(key).strip()}


def canonical_connection_payload(
    *,
    transport: str,
    url: str,
    timeout_s: int | None,
    sse_read_timeout_s: int | None,
    headers: dict[str, str],
) -> str:
    """Canonical JSON for connection identity; credentials are not echoed."""

    return json.dumps(
        {
            "transport": str(transport or ""),
            "url": str(url or ""),
            "timeout_s": timeout_s,
            "sse_read_timeout_s": sse_read_timeout_s,
            "headers": {str(key): str(val) for key, val in (headers or {}).items()},
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _fernet():
    key = str(getattr(settings, "MCP_CREDENTIALS_KEY", "") or "").strip()
    if not key:
        raise McpCredentialError("MCP_CREDENTIALS_KEY is not configured")
    from cryptography.fernet import Fernet

    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 - any key parse failure is the same policy outcome
        raise McpCredentialError("MCP_CREDENTIALS_KEY is not a valid Fernet key") from exc


def encrypt_headers(headers: dict[str, str] | None) -> str:
    """Encrypt a header map for storage; empty input stores an empty string."""

    normalized = _normalize_headers(headers)
    if not normalized:
        return ""
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return _fernet().encrypt(payload.encode("utf-8")).decode("ascii")


def decrypt_headers(ciphertext: str | None) -> dict[str, str]:
    """Decrypt a stored header map; raises :class:`McpCredentialError`."""

    text = str(ciphertext or "").strip()
    if not text:
        return {}
    parsed = decrypt_json(text)
    if not isinstance(parsed, dict):
        raise McpCredentialError("MCP credentials payload is not a header map")
    return {str(key): str(val) for key, val in parsed.items()}


def encrypt_json(value: Any) -> str:
    """Encrypt an arbitrary JSON value (phase-2 write-proposal arguments)."""

    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str
    )
    return _fernet().encrypt(payload.encode("utf-8")).decode("ascii")


def decrypt_json(ciphertext: str | None) -> Any:
    """Decrypt an arbitrary JSON value; raises :class:`McpCredentialError`."""

    text = str(ciphertext or "").strip()
    if not text:
        return None
    try:
        raw = _fernet().decrypt(text.encode("ascii"))
    except McpCredentialError:
        raise
    except Exception as exc:  # noqa: BLE001 - invalid token == key mismatch/rotation
        raise McpCredentialError(
            "MCP payload cannot be decrypted; check MCP_CREDENTIALS_KEY"
        ) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise McpCredentialError("MCP payload is not valid JSON") from exc


def connection_fingerprint(
    *,
    transport: str,
    url: str,
    timeout_s: int | None,
    sse_read_timeout_s: int | None,
    headers: dict[str, str],
) -> str:
    """HMAC-SHA256 of the connection identity, keyed when a key is configured.

    The fingerprint is stored and compared, never logged with its inputs, so it
    cannot be used to confirm guessed credentials offline.
    """

    payload = canonical_connection_payload(
        transport=transport,
        url=url,
        timeout_s=timeout_s,
        sse_read_timeout_s=sse_read_timeout_s,
        headers=headers,
    )
    key = str(getattr(settings, "MCP_CREDENTIALS_KEY", "") or "").strip()
    if headers and not key:
        raise McpCredentialError("MCP_CREDENTIALS_KEY is required when headers are configured")
    if key:
        return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class McpServerStore(Protocol):
    """Storage contract consumed by the service/policy layers."""

    def list_all(self) -> list[McpServerRecord]: ...

    def get(self, slug: str) -> McpServerRecord | None: ...

    def list_enabled(self) -> list[McpServerRecord]: ...

    def create(self, **fields: Any) -> McpServerRecord: ...

    def update(self, slug: str, **fields: Any) -> McpServerRecord: ...

    def delete(self, slug: str) -> bool: ...

    def set_enabled(self, slug: str, enabled: bool, updated_by: str = "") -> McpServerRecord: ...

    def record_test_result(
        self, slug: str, *, ok: bool, fingerprint: str, error: str = ""
    ) -> McpServerRecord: ...

    def toggle_tool(self, slug: str, tool_name: str, updated_by: str = "") -> tuple[bool, McpServerRecord]: ...

    def set_write_tools(
        self, slug: str, write_tools: list[str], updated_by: str = ""
    ) -> McpServerRecord: ...

    def list_subscribed_slugs(self, user_id: int) -> set[str]: ...

    def list_subscriptions(self, user_id: int) -> list[McpSubscription]: ...

    def upsert_subscription(
        self, user_id: int, slug: str, *, allow_write: bool = False
    ) -> McpSubscription: ...

    def delete_subscription(self, user_id: int, slug: str) -> bool: ...

    def delete_user_subscriptions(self, user_id: int) -> int: ...

    def count_subscriptions(self) -> int: ...


class SqliteMcpServerStore:
    """SQLite implementation backed by ``AUTH_DB_PATH``."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.AUTH_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        init_key = os.path.abspath(self.db_path)
        if init_key in _INITIALIZED_PATHS:
            return
        with _INIT_LOCK:
            if init_key in _INITIALIZED_PATHS:
                return
            self._init_db()
            _INITIALIZED_PATHS.add(init_key)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    transport TEXT NOT NULL,
                    url TEXT NOT NULL,
                    headers_ciphertext TEXT NOT NULL DEFAULT '',
                    credentials_key_version TEXT NOT NULL DEFAULT '1',
                    timeout_s INTEGER,
                    sse_read_timeout_s INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    department_scope TEXT NOT NULL DEFAULT 'none',
                    department_ids TEXT NOT NULL DEFAULT '[]',
                    disabled_tools TEXT NOT NULL DEFAULT '[]',
                    write_tools TEXT NOT NULL DEFAULT '[]',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    icon TEXT NOT NULL DEFAULT '',
                    connection_fingerprint TEXT NOT NULL DEFAULT '',
                    last_test_fingerprint TEXT NOT NULL DEFAULT '',
                    last_test_ok_at TEXT NOT NULL DEFAULT '',
                    last_test_error TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            for column, ddl in (
                ("write_tools", "TEXT NOT NULL DEFAULT '[]'"),
                ("credentials_key_version", "TEXT NOT NULL DEFAULT '1'"),
                ("last_test_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ):
                self._ensure_column(conn, "mcp_servers", column, ddl)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcp_servers_enabled ON mcp_servers(enabled)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_user_subscriptions (
                    user_id INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    allow_write INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, slug)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcp_subscriptions_user"
                " ON mcp_user_subscriptions(user_id)"
            )

    @staticmethod
    def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ------------------------------------------------------------------
    # row mapping

    def _row_to_record(self, row: sqlite3.Row) -> McpServerRecord:
        headers: dict[str, str] = {}
        headers_error = ""
        try:
            headers = decrypt_headers(row["headers_ciphertext"])
        except McpCredentialError as exc:
            # Degrade one server, not the catalog; the admin view surfaces the
            # reason and the runtime policy skips it.
            headers_error = str(exc)
        return McpServerRecord(
            id=int(row["id"]),
            slug=str(row["slug"]),
            name=str(row["name"]),
            description=str(row["description"] or ""),
            transport=str(row["transport"]),
            url=str(row["url"]),
            headers=headers,
            timeout_s=int(row["timeout_s"]) if row["timeout_s"] is not None else None,
            sse_read_timeout_s=(
                int(row["sse_read_timeout_s"]) if row["sse_read_timeout_s"] is not None else None
            ),
            enabled=bool(row["enabled"]),
            department_scope=str(row["department_scope"] or "none"),
            department_ids=_normalize_int_list(json.loads(row["department_ids"] or _JSON_LIST)),
            disabled_tools=_normalize_str_list(json.loads(row["disabled_tools"] or _JSON_LIST)),
            write_tools=_normalize_str_list(json.loads(row["write_tools"] or _JSON_LIST)),
            tags=_normalize_str_list(json.loads(row["tags_json"] or _JSON_LIST)),
            icon=str(row["icon"] or ""),
            connection_fingerprint=str(row["connection_fingerprint"] or ""),
            last_test_fingerprint=str(row["last_test_fingerprint"] or ""),
            last_test_ok_at=str(row["last_test_ok_at"] or ""),
            last_test_error=str(row["last_test_error"] or ""),
            headers_error=headers_error,
            created_by=str(row["created_by"] or ""),
            updated_by=str(row["updated_by"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    @staticmethod
    def _row_to_subscription(row: sqlite3.Row) -> McpSubscription:
        return McpSubscription(
            user_id=int(row["user_id"]),
            slug=str(row["slug"]),
            allow_write=bool(row["allow_write"]),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    # ------------------------------------------------------------------
    # server CRUD

    def list_all(self) -> list[McpServerRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_servers ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_enabled(self) -> list[McpServerRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_servers WHERE enabled = 1 ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get(self, slug: str) -> McpServerRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM mcp_servers WHERE slug = ?", (str(slug),)
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def create(
        self,
        *,
        slug: str,
        name: str,
        transport: str,
        url: str,
        description: str = "",
        headers: dict[str, str] | None = None,
        timeout_s: int | None = None,
        sse_read_timeout_s: int | None = None,
        department_scope: str = "none",
        department_ids: list[int] | None = None,
        disabled_tools: list[str] | None = None,
        write_tools: list[str] | None = None,
        tags: list[str] | None = None,
        icon: str = "",
        created_by: str = "",
    ) -> McpServerRecord:
        slug = str(slug or "").strip()
        if not slug:
            raise ValueError("slug 不能为空")
        headers = _normalize_headers(headers)
        now = _utc_now()
        ciphertext = encrypt_headers(headers)
        fingerprint = connection_fingerprint(
            transport=transport,
            url=url,
            timeout_s=timeout_s,
            sse_read_timeout_s=sse_read_timeout_s,
            headers=headers,
        )
        with closing(self._connect()) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO mcp_servers (
                        slug, name, description, transport, url, headers_ciphertext,
                        timeout_s, sse_read_timeout_s, enabled, department_scope,
                        department_ids, disabled_tools, write_tools, tags_json, icon,
                        connection_fingerprint, created_by, updated_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        str(name or slug),
                        str(description or ""),
                        str(transport or ""),
                        str(url or ""),
                        ciphertext,
                        timeout_s,
                        sse_read_timeout_s,
                        str(department_scope or "none"),
                        json.dumps(_normalize_int_list(department_ids)),
                        json.dumps(_normalize_str_list(disabled_tools)),
                        json.dumps(_normalize_str_list(write_tools)),
                        json.dumps(_normalize_str_list(tags)),
                        str(icon or ""),
                        fingerprint,
                        str(created_by or ""),
                        str(created_by or ""),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise McpServerExistsError(f"MCP server '{slug}' already exists") from exc
        record = self.get(slug)
        if record is None:  # pragma: no cover - insert just committed
            raise McpServerNotFoundError(slug)
        return record

    def update(
        self,
        slug: str,
        *,
        name: Any = UNSET,
        description: Any = UNSET,
        transport: Any = UNSET,
        url: Any = UNSET,
        headers: Any = UNSET,
        timeout_s: Any = UNSET,
        sse_read_timeout_s: Any = UNSET,
        department_scope: Any = UNSET,
        department_ids: Any = UNSET,
        disabled_tools: Any = UNSET,
        write_tools: Any = UNSET,
        tags: Any = UNSET,
        icon: Any = UNSET,
        updated_by: str = "",
    ) -> McpServerRecord:
        """Patch one server; connection changes auto-disable and void the test.

        ``headers=UNSET`` keeps stored credentials, ``headers=None`` clears
        them, and a dict replaces them.
        """

        slug = str(slug or "").strip()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM mcp_servers WHERE slug = ?", (slug,)
                ).fetchone()
                if row is None:
                    raise McpServerNotFoundError(slug)
                old = self._row_to_record(row)

                next_headers = (
                    dict(old.headers)
                    if headers is UNSET
                    else ({} if headers is None else _normalize_headers(headers))
                )
                next_transport = old.transport if transport is UNSET else str(transport or "")
                next_url = old.url if url is UNSET else str(url or "")
                next_timeout = old.timeout_s if timeout_s is UNSET else timeout_s
                next_sse = old.sse_read_timeout_s if sse_read_timeout_s is UNSET else sse_read_timeout_s

                connection_changed = (
                    next_transport != old.transport
                    or next_url != old.url
                    or next_timeout != old.timeout_s
                    or next_sse != old.sse_read_timeout_s
                    or next_headers != old.headers
                )
                fingerprint = connection_fingerprint(
                    transport=next_transport,
                    url=next_url,
                    timeout_s=next_timeout,
                    sse_read_timeout_s=next_sse,
                    headers=next_headers,
                )
                enabled = 0 if connection_changed else (1 if old.enabled else 0)
                last_test_fingerprint = "" if connection_changed else old.last_test_fingerprint
                last_test_error = "" if connection_changed else old.last_test_error
                ciphertext = encrypt_headers(next_headers)

                conn.execute(
                    """
                    UPDATE mcp_servers SET
                        name = ?, description = ?, transport = ?, url = ?,
                        headers_ciphertext = ?, timeout_s = ?, sse_read_timeout_s = ?,
                        enabled = ?, department_scope = ?, department_ids = ?,
                        disabled_tools = ?, write_tools = ?, tags_json = ?, icon = ?,
                        connection_fingerprint = ?, last_test_fingerprint = ?,
                        last_test_error = ?, updated_by = ?, updated_at = ?
                    WHERE slug = ?
                    """,
                    (
                        old.name if name is UNSET else str(name or ""),
                        old.description if description is UNSET else str(description or ""),
                        next_transport,
                        next_url,
                        ciphertext,
                        next_timeout,
                        next_sse,
                        enabled,
                        old.department_scope if department_scope is UNSET else str(department_scope or "none"),
                        json.dumps(
                            old.department_ids
                            if department_ids is UNSET
                            else _normalize_int_list(department_ids)
                        ),
                        json.dumps(
                            old.disabled_tools
                            if disabled_tools is UNSET
                            else _normalize_str_list(disabled_tools)
                        ),
                        json.dumps(
                            old.write_tools if write_tools is UNSET else _normalize_str_list(write_tools)
                        ),
                        json.dumps(old.tags if tags is UNSET else _normalize_str_list(tags)),
                        old.icon if icon is UNSET else str(icon or ""),
                        fingerprint,
                        last_test_fingerprint,
                        last_test_error,
                        str(updated_by or old.updated_by),
                        _utc_now(),
                        slug,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        record = self.get(slug)
        if record is None:  # pragma: no cover - update just committed
            raise McpServerNotFoundError(slug)
        return record

    def delete(self, slug: str) -> bool:
        slug = str(slug or "").strip()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM mcp_user_subscriptions WHERE slug = ?", (slug,))
                cursor = conn.execute("DELETE FROM mcp_servers WHERE slug = ?", (slug,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return bool(cursor.rowcount)

    def set_enabled(self, slug: str, enabled: bool, updated_by: str = "") -> McpServerRecord:
        return self._update_flags(
            slug,
            {"enabled": 1 if enabled else 0, "updated_by": str(updated_by or "")},
        )

    def record_test_result(
        self, slug: str, *, ok: bool, fingerprint: str, error: str = ""
    ) -> McpServerRecord:
        now = _utc_now()
        fields: dict[str, Any] = {
            "last_test_fingerprint": str(fingerprint or "") if ok else "",
            "last_test_error": "" if ok else str(error or "")[:500],
        }
        if ok:
            fields["last_test_ok_at"] = now
        return self._update_flags(slug, fields)

    def toggle_tool(
        self, slug: str, tool_name: str, updated_by: str = ""
    ) -> tuple[bool, McpServerRecord]:
        tool_name = str(tool_name or "").strip()
        if not tool_name:
            raise ValueError("tool_name 不能为空")
        record = self.get(slug)
        if record is None:
            raise McpServerNotFoundError(slug)
        disabled = list(record.disabled_tools)
        if tool_name in disabled:
            disabled.remove(tool_name)
            enabled = True
        else:
            disabled.append(tool_name)
            enabled = False
        updated = self._update_flags(
            slug,
            {
                "disabled_tools": json.dumps(_normalize_str_list(disabled)),
                "updated_by": str(updated_by or ""),
            },
        )
        return enabled, updated

    def set_write_tools(
        self, slug: str, write_tools: list[str], updated_by: str = ""
    ) -> McpServerRecord:
        return self._update_flags(
            slug,
            {
                "write_tools": json.dumps(_normalize_str_list(write_tools)),
                "updated_by": str(updated_by or ""),
            },
        )

    def _update_flags(self, slug: str, fields: dict[str, Any]) -> McpServerRecord:
        slug = str(slug or "").strip()
        if not fields:
            record = self.get(slug)
            if record is None:
                raise McpServerNotFoundError(slug)
            return record
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    f"UPDATE mcp_servers SET {assignments}, updated_at = ? WHERE slug = ?",
                    (*values, _utc_now(), slug),
                )
                if not cursor.rowcount:
                    raise McpServerNotFoundError(slug)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        record = self.get(slug)
        if record is None:  # pragma: no cover - update just committed
            raise McpServerNotFoundError(slug)
        return record

    # ------------------------------------------------------------------
    # user subscriptions

    def list_subscribed_slugs(self, user_id: int) -> set[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT slug FROM mcp_user_subscriptions WHERE user_id = ?",
                (int(user_id),),
            ).fetchall()
        return {str(row["slug"]) for row in rows}

    def list_subscriptions(self, user_id: int) -> list[McpSubscription]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_user_subscriptions WHERE user_id = ? ORDER BY slug",
                (int(user_id),),
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def upsert_subscription(
        self, user_id: int, slug: str, *, allow_write: bool = False
    ) -> McpSubscription:
        slug = str(slug or "").strip()
        now = _utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                exists = conn.execute(
                    "SELECT 1 FROM mcp_servers WHERE slug = ?", (slug,)
                ).fetchone()
                if exists is None:
                    raise McpServerNotFoundError(slug)
                conn.execute(
                    """
                    INSERT INTO mcp_user_subscriptions (user_id, slug, allow_write, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, slug) DO UPDATE SET
                        allow_write = excluded.allow_write,
                        updated_at = excluded.updated_at
                    """,
                    (int(user_id), slug, 1 if allow_write else 0, now, now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        subscription = self.get_subscription(user_id, slug)
        if subscription is None:  # pragma: no cover - upsert just committed
            raise McpServerNotFoundError(slug)
        return subscription

    def get_subscription(self, user_id: int, slug: str) -> McpSubscription | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM mcp_user_subscriptions WHERE user_id = ? AND slug = ?",
                (int(user_id), str(slug)),
            ).fetchone()
        return self._row_to_subscription(row) if row is not None else None

    def delete_subscription(self, user_id: int, slug: str) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "DELETE FROM mcp_user_subscriptions WHERE user_id = ? AND slug = ?",
                (int(user_id), str(slug)),
            )
        return bool(cursor.rowcount)

    def delete_user_subscriptions(self, user_id: int) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "DELETE FROM mcp_user_subscriptions WHERE user_id = ?", (int(user_id),)
            )
        return int(cursor.rowcount)

    def count_subscriptions(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM mcp_user_subscriptions").fetchone()
        return int(row["n"] if row is not None else 0)


def build_mcp_store(settings_module: Any | None = None) -> SqliteMcpServerStore:
    """Build the MCP store; phase 1 always uses the auth database.

    The PostgreSQL control plane does not own users/departments/audit_events,
    so it must not own MCP configuration either; a PG adapter needs its own
    migration decision and is intentionally not wired here.
    """

    if settings_module is None:
        import src.settings as settings_module  # type: ignore[no-redef]

    return SqliteMcpServerStore(getattr(settings_module, "AUTH_DB_PATH", None))


__all__ = [
    "McpCredentialError",
    "McpServerExistsError",
    "McpServerNotFoundError",
    "McpServerStore",
    "SqliteMcpServerStore",
    "UNSET",
    "build_mcp_store",
    "canonical_connection_payload",
    "connection_fingerprint",
    "decrypt_headers",
    "decrypt_json",
    "encrypt_headers",
    "encrypt_json",
]
