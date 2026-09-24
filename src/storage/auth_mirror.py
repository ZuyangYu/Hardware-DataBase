"""Auth-domain row mirror into the PostgreSQL control plane (interim bridge).

Why this exists: with ``CONTROL_PLANE_BACKEND=postgres`` the control plane
reads ``users.is_active`` / ``users.department_id`` and enforces the
``fk_chat_sessions_user`` foreign key against the **PostgreSQL** copy of
``users``, while the authoritative store for the auth domain stays in SQLite
(``AUTH_DB_PATH``).  Without a mirror, any user/department created *after*
the cutover snapshot only exists in SQLite, so:

- ``create_session`` fails the foreign key outright (the user cannot chat);
- ``list_pending_turn_work`` would silently drop their turns (INNER JOIN).

The mirror keeps the control plane's copy current on every auth mutation.
The authoritative store stays SQLite — the mirror is one-directional
(SQLite -> PostgreSQL), so there is no reconciliation problem: a row exists
in both places only because the control plane needs it for joins/FKs.

Fail-soft by contract: a mirror failure logs an error and returns ``False``;
it must never break the auth mutation itself.  The known consequence of a
missed mirror is the FK failure above, which surfaces loudly on the next
session creation.

Lifecycle: this module is a bridge for the hybrid
(SQLite auth + PostgreSQL control plane) deployment shape.  When the auth
domain converges into PostgreSQL (storage-convergence phase 3), delete this
module together with the ``AuthService._mirror_control_plane_row`` hooks.

Kill switch: set ``HDB_AUTH_MIRROR_ENABLED=0`` to disable without a deploy.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from typing import Any

_MIRROR_ENV_FLAG = "HDB_AUTH_MIRROR_ENABLED"

# Only these auth-domain tables are mirrored: they are the ones the control
# plane reads / references by foreign key.  Everything else in auth.db
# (audit_events, auth_sessions, knowledge_bases, ...) is never read by the
# control plane and stays SQLite-only.
MIRROR_TABLES: tuple[str, ...] = ("users", "departments")
_KEY_COLUMN = "id"

_COLUMNS_CACHE: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
_COLUMNS_LOCK = threading.Lock()


def pg_ident(name: str) -> str:
    """Quote a PostgreSQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def mirror_enabled() -> bool:
    """Whether the mirror should run for the current process settings."""
    flag = os.getenv(_MIRROR_ENV_FLAG, "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    import src.settings as settings

    backend = str(getattr(settings, "CONTROL_PLANE_BACKEND", "sqlite") or "sqlite")
    dsn = str(getattr(settings, "CONTROL_PLANE_DATABASE_URL", "") or "").strip()
    return backend.strip().lower() == "postgres" and bool(dsn)


def _dsn() -> str:
    import src.settings as settings

    return str(getattr(settings, "CONTROL_PLANE_DATABASE_URL", "") or "").strip()


def _pg_columns(dsn: str, table: str) -> tuple[tuple[str, str], ...]:
    """``((column_name, data_type), ...)`` for the PG table, cached per DSN.

    Column *intersection* with the SQLite row is computed per call, so schema
    drift between the auth domain (``AuthService._ensure_column``) and the
    snapshot-generated PG table degrades to "fewer mirrored columns" instead
    of a hard failure.
    """
    cache_key = (dsn, table)
    cached = _COLUMNS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    import psycopg

    with closing(
        psycopg.connect(dsn, connect_timeout=5, options="-c TimeZone=UTC")
    ) as conn:
        rows = conn.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        ).fetchall()
    columns = tuple((str(name), str(data_type)) for name, data_type in rows)
    with _COLUMNS_LOCK:
        _COLUMNS_CACHE[cache_key] = columns
    return columns


def _sqlite_row(auth_db_path: str | None, table: str, key_value: Any) -> dict | None:
    if not auth_db_path:
        return None
    with closing(
        sqlite3.connect(f"file:{auth_db_path}?mode=ro", uri=True, timeout=5)
    ) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {_KEY_COLUMN} = ?", (key_value,)
        ).fetchone()
    return dict(row) if row is not None else None


def _coerce(value: Any, data_type: str) -> Any:
    """Adapt a SQLite value to the PG column type before binding.

    psycopg binds Python ints as smallint, and PostgreSQL has no
    ``smallint -> boolean`` cast (not even explicit), so 0/1 must become a
    real bool for BOOLEAN columns.  Strings for TIMESTAMPTZ/JSONB stay
    strings: the explicit ``::`` cast handles them server-side.
    """
    if value is None:
        return None
    if data_type == "boolean":
        return bool(int(value))
    if data_type in {"smallint", "integer", "bigint"}:
        return int(value)
    if data_type in {"real", "double precision"} or data_type.startswith("numeric"):
        return float(value)
    return value


def mirror_auth_row(
    auth_db_path: str | None,
    table: str,
    key_value: Any,
    *,
    delete: bool = False,
) -> bool:
    """Copy one auth-domain row into the PostgreSQL control plane.

    Returns ``True`` when the control-plane copy is current afterwards,
    ``False`` when the mirror is disabled / skipped / failed (callers must
    treat ``False`` as "the control plane does not have this row").
    """
    if table not in MIRROR_TABLES:
        raise ValueError(f"table {table!r} is not mirrorable (allowed: {MIRROR_TABLES})")
    if not mirror_enabled():
        return False

    import psycopg
    from src.core.logger import error

    dsn = _dsn()
    try:
        columns = _pg_columns(dsn, table)
        if not columns:
            error(f"[auth-mirror] PG 表 {table} 不存在或无列，跳过镜像")
            return False
        column_names = [name for name, _ in columns]

        with closing(
            psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC")
        ) as conn:
            if delete:
                conn.execute(
                    f"DELETE FROM {table} WHERE {pg_ident(_KEY_COLUMN)} = %s",
                    (key_value,),
                )
                return True

            row = _sqlite_row(auth_db_path, table, key_value)
            if row is None:
                error(
                    f"[auth-mirror] SQLite {table}#{key_value} 不存在，无法镜像"
                    "（如需清理控制面副本请手动处理）"
                )
                return False

            shared = [name for name in column_names if name in row]
            if _KEY_COLUMN not in shared:
                error(f"[auth-mirror] PG 表 {table} 缺少 {_KEY_COLUMN} 列，跳过镜像")
                return False
            placeholders = ", ".join(
                f"%s::{data_type}"
                for name, data_type in columns
                if name in shared
            )
            assignments = ", ".join(
                f"{pg_ident(name)} = EXCLUDED.{pg_ident(name)}"
                for name in shared
                if name != _KEY_COLUMN
            )
            params = [_coerce(row[name], data_type) for name, data_type in columns if name in shared]
            conn.execute(
                f"""
                INSERT INTO {table} ({", ".join(pg_ident(n) for n in shared)})
                VALUES ({placeholders})
                ON CONFLICT ({pg_ident(_KEY_COLUMN)}) DO UPDATE
                SET {assignments}
                """,
                params,
            )
        return True
    except Exception as exc:  # noqa: BLE001 - fail-soft by contract
        error(f"[auth-mirror] {table}#{key_value} 镜像失败: {type(exc).__name__}: {exc}")
        return False
