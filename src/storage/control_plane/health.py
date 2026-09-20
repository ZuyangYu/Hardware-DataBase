"""Control-plane health for readiness probes (v3 §15).

SQLite deployments check the authority file; PostgreSQL deployments must prove
the versioned schema is present and compatible — a missing or stale schema is
``down``, never a silent fallback to a local empty database.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any


def check_control_plane(settings_module: Any | None = None) -> dict[str, Any]:
    if settings_module is None:
        import src.settings as settings_module  # type: ignore[no-redef]

    backend = (
        str(getattr(settings_module, "CONTROL_PLANE_BACKEND", "sqlite") or "sqlite")
        .strip()
        .lower()
    )
    started = time.monotonic()

    if backend == "sqlite":
        path = str(getattr(settings_module, "AUTH_DB_PATH", "") or "")
        try:
            with sqlite3.connect(path, timeout=5) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 - normalized as readiness failure
            return {"status": "down", "backend": "sqlite", "error": type(exc).__name__}
        return {
            "status": "up",
            "backend": "sqlite",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }

    if backend != "postgres":
        return {"status": "down", "backend": backend, "error": "unsupported backend"}

    dsn = str(
        getattr(settings_module, "CONTROL_PLANE_DATABASE_URL", "") or ""
    ).strip()
    if not dsn:
        return {
            "status": "down",
            "backend": "postgres",
            "error": "CONTROL_PLANE_DATABASE_URL is not configured",
        }
    try:
        import psycopg

        from .schema_version import assert_schema_compatible

        with psycopg.connect(
            dsn,
            autocommit=True,
            options="-c TimeZone=UTC -c statement_timeout=5000",
        ) as conn:
            version = assert_schema_compatible(conn)
        return {
            "status": "up",
            "backend": "postgres",
            "schema_version": version,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except Exception as exc:  # noqa: BLE001 - normalized as readiness failure
        return {
            "status": "down",
            "backend": "postgres",
            "error": str(exc)[:200],
        }
