"""Long-lived personal MCP credentials.

The token is a bearer credential owned by one employee.  It is hashed for
lookup and exchanged for a short-lived platform session by
``POST /api/v1/mcp/session``.  The plaintext is also kept so the owner can view
and copy it again later; when ``MCP_CREDENTIALS_KEY`` is configured it is
encrypted at rest with Fernet.  Keeping this store separate from the auth
domain avoids a schema migration in the PostgreSQL auth store while preserving
per-user identity and revocation.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

import src.settings as settings

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()
_PREFIX = "hdbmcp_"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    override = os.getenv("MCP_TOKEN_DB_PATH", "").strip()
    path = override or os.path.join(str(settings.STORAGE_DIR), "mcp_tokens.db")
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    path = _db_path()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    with _INIT_LOCK:
        if path not in _INITIALIZED:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_tokens (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL DEFAULT 'personal-mcp',
                    token_hash TEXT NOT NULL UNIQUE,
                    token_plain TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_used_at TEXT
                )
                """
            )
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(mcp_tokens)")
            }
            if "token_plain" not in columns:
                conn.execute("ALTER TABLE mcp_tokens ADD COLUMN token_plain TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id)"
            )
            conn.commit()
            _INITIALIZED.add(path)
    return conn


def _hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _fernet() -> Fernet | None:
    key = str(getattr(settings, "MCP_CREDENTIALS_KEY", "") or "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError):
        derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
        return Fernet(derived)


def _seal(token: str) -> str:
    """Encrypt the plaintext token when a credentials key is configured."""
    cipher = _fernet()
    if cipher is None:
        return token
    return "enc:v1:" + cipher.encrypt(token.encode("utf-8")).decode("ascii")


def _unseal(value: str) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if not raw.startswith("enc:v1:"):
        return raw
    cipher = _fernet()
    if cipher is None:
        return ""
    try:
        return cipher.decrypt(raw[len("enc:v1:") :].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def _view(row: sqlite3.Row) -> dict[str, Any]:
    token = _unseal(row["token_plain"]) if "token_plain" in row.keys() else ""
    return {
        "token_id": str(row["id"]),
        "name": str(row["name"]),
        "masked_token": f"{_PREFIX}{row['id']}_••••",
        "token": token,
        "created_at": str(row["created_at"]),
        "expires_at": str(row["expires_at"]),
        "last_used_at": str(row["last_used_at"] or ""),
    }


def get_status(user_id: int | str) -> dict[str, Any]:
    now = _now()
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT * FROM mcp_tokens
            WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(user_id), now),
        ).fetchone()
    if row is None:
        return {"has_token": False}
    return {"has_token": True, **_view(row)}


def create_or_rotate(
    user_id: int | str,
    *,
    name: str = "个人 MCP",
    ttl_days: int | None = None,
) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    days = max(1, min(int(ttl_days or settings.MCP_TOKEN_TTL_DAYS), 3650))
    expires_at = (now_dt + timedelta(days=days)).isoformat()
    token_id = secrets.token_hex(8)
    token = f"{_PREFIX}{token_id}_{secrets.token_urlsafe(32)}"
    now = now_dt.isoformat()
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE mcp_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now, str(user_id)),
        )
        conn.execute(
            """
            INSERT INTO mcp_tokens(
                id, user_id, name, token_hash, token_plain, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                str(user_id),
                str(name or "个人 MCP")[:120],
                _hash(token),
                _seal(token),
                now,
                expires_at,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM mcp_tokens WHERE id = ?", (token_id,)).fetchone()
    return {
        "token": token,
        "expires_at": expires_at,
        **_view(row),
    }


def revoke(user_id: int | str) -> int:
    now = _now()
    with closing(_connect()) as conn:
        result = conn.execute(
            "UPDATE mcp_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now, str(user_id)),
        )
        conn.commit()
        return int(result.rowcount or 0)


def resolve(token: str) -> tuple[int | None, str]:
    raw = str(token or "").strip()
    if not raw:
        return None, "invalid"
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM mcp_tokens WHERE token_hash = ? LIMIT 1",
            (_hash(raw),),
        ).fetchone()
        if row is None:
            return None, "invalid"
        if row["revoked_at"]:
            return None, "revoked"
        if str(row["expires_at"]) <= _now():
            return None, "expired"
        conn.execute(
            "UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
        conn.commit()
        return int(row["user_id"]), "ok"


__all__ = ["create_or_rotate", "get_status", "resolve", "revoke"]
