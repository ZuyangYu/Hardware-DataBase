"""Check/apply/verify additive scope fields. The default mode never writes."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.storage.control_plane.knowledge_scope_migration import (  # noqa: E402
    apply_postgres, apply_sqlite, inspect_postgres, inspect_sqlite,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--db", default="")
    parser.add_argument("--dsn", default="")
    modes = parser.add_mutually_exclusive_group()
    for mode in ("check", "apply", "verify"):
        modes.add_argument(f"--{mode}", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.backend == "sqlite":
            path = Path(args.db)
            if not args.db or not path.is_file():
                raise ValueError("sqlite_database_required")
            with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode={'rw' if args.apply else 'ro'}", uri=True)) as conn:
                if args.apply:
                    with conn:
                        conn.execute("BEGIN IMMEDIATE")
                        report = apply_sqlite(conn)
                else:
                    report = inspect_sqlite(conn)
        else:
            import psycopg

            dsn = args.dsn or os.getenv("CONTROL_PLANE_DATABASE_URL", "")
            if not dsn:
                raise ValueError("postgres_dsn_required")
            with psycopg.connect(dsn) as conn:
                if args.apply:
                    conn.execute("SELECT pg_advisory_xact_lock(739135002)")
                    report = apply_postgres(conn)
                else:
                    conn.execute("SET TRANSACTION READ ONLY")
                    report = inspect_postgres(conn)
        print(json.dumps({"backend": args.backend, "mode": "apply" if args.apply else "verify" if args.verify else "check", **report}, sort_keys=True))
        return 0 if report["ready"] else 1
    except Exception as exc:
        # Driver exceptions can contain credentials/hostnames; do not echo them.
        print(json.dumps({"ready": False, "error": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
