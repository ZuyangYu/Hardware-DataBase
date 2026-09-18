"""Load a frozen SQLite snapshot into the versioned control-plane PostgreSQL schema.

Stop-write maintenance-window tool (v3 §5.4/§5.5).  It creates the target
schema (execution core DDL + generated DDL for the remaining tables), copies
the rows with explicit type mapping, rebuilds indexes and foreign keys, then
verifies counts and content hashes plus the execution-protocol checks.

Example::

    python scripts/production_hardening/load_sqlite_into_postgres.py \
        --source storage/auth.db \
        --dsn postgresql://user:pass@127.0.0.1:5432/hdb_control \
        --report /tmp/control_plane_load.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.storage.control_plane import load_sqlite_snapshot  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="SQLite snapshot path")
    parser.add_argument("--dsn", required=True, help="target PostgreSQL connection URL")
    parser.add_argument(
        "--tables",
        default="",
        help="comma-separated table subset (default: every non-derived table)",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report source tables and row counts without writing",
    )
    parser.add_argument(
        "--no-backfill-execution-state",
        action="store_true",
        help="do not insert missing conversation_execution_state rows",
    )
    parser.add_argument(
        "--no-foreign-keys",
        action="store_true",
        help="skip foreign-key creation after the load",
    )
    parser.add_argument("--report", default="", help="write the JSON report to this path")
    args = parser.parse_args(argv)

    source_path = Path(args.source)
    if not source_path.is_file():
        print(f"source database not found: {source_path}", file=sys.stderr)
        return 2

    import psycopg

    tables = [item.strip() for item in args.tables.split(",") if item.strip()]
    with psycopg.connect(
        args.dsn, autocommit=True, options="-c TimeZone=UTC -c lock_timeout=5000"
    ) as conn:
        report = load_sqlite_snapshot(
            source_path,
            conn,
            tables=tables or None,
            batch_size=max(1, int(args.batch_size)),
            backfill_execution_state=not args.no_backfill_execution_state,
            dry_run=bool(args.dry_run),
            add_foreign_keys=not args.no_foreign_keys,
        )

    payload = report.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    print(
        "ok" if report.ok else "FAILED",
        f"tables={len(report.tables)}",
        f"failures={len(report.failures)}",
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
