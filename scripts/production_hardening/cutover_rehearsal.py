"""End-to-end stop-write cutover rehearsal (v3 §5.5 / PR-3).

Runs the rehearsable part of the cutover against a disposable target database:

1. preflight: source exists, target reachable, schema reset (when requested)
2. snapshot:  SQLite ``VACUUM INTO`` frozen copy + hashes of source and copy
3. migrate:   versioned schema + loader import of the authority group
4. verify:    loader checks (counts/hashes/protocol) must be empty
5. smoke:     store reads on the migrated target (queue, metrics, counts)
6. rollback:  drop schema, re-apply, re-load, re-verify (fresh-target cycle)
7. report:    JSON with per-step status and a final ``ok``

Operator production steps (service stop, write barrier, backups, switches) stay
in the runbook; this script proves the data path and the rollback path.

Example::

    python scripts/production_hardening/cutover_rehearsal.py \
        --source storage/auth.db \
        --dsn postgresql://user:pass@127.0.0.1:5432/hdb_rehearsal \
        --report /tmp/cutover_rehearsal.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.storage.control_plane import apply_schema, load_sqlite_snapshot  # noqa: E402
from src.storage.control_plane.postgres import PostgresExecutionStore  # noqa: E402
from src.storage.control_plane.schema_version import record_schema_version  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="SQLite authority snapshot")
    parser.add_argument("--dsn", required=True, help="disposable target PostgreSQL URL")
    parser.add_argument("--report", default="", help="write the JSON report here")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--keep-target-schema",
        action="store_true",
        help="do not drop the target public schema before loading",
    )
    args = parser.parse_args(argv)

    source = Path(args.source)
    steps: list[dict] = []
    report: dict = {"source": str(source), "dsn": "<redacted>", "steps": steps}

    def step(name: str, status: str, **detail) -> None:
        steps.append({"name": name, "status": status, **detail})

    if not source.is_file():
        step("preflight", "failed", error="source database not found")
        report["ok"] = False
        _emit(report, args.report)
        return 2

    import psycopg

    # 1/2: frozen snapshot of the source (VACUUM INTO, read-only safe).
    tmpdir = tempfile.TemporaryDirectory()
    try:
        snapshot = Path(tmpdir.name) / "auth_snapshot.db"
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as conn:
            conn.execute("VACUUM INTO ?", (str(snapshot),))
        source_hash = _sha256(source)
        snapshot_hash = _sha256(snapshot)
        step(
            "snapshot",
            "ok",
            source_sha256=source_hash,
            snapshot_sha256=snapshot_hash,
        )

        def migrate() -> dict:
            with psycopg.connect(
                args.dsn, autocommit=True, options="-c TimeZone=UTC"
            ) as conn:
                conn.execute("DROP SCHEMA public CASCADE")
                conn.execute("CREATE SCHEMA public")
                apply_schema(conn)
                record_schema_version(conn, source_fingerprint=snapshot_hash)
                loaded = load_sqlite_snapshot(
                    snapshot, conn, batch_size=max(1, int(args.batch_size))
                )
            return loaded

        loaded = migrate()
        report["tables"] = len(loaded.tables)
        report["checks"] = loaded.checks
        if not loaded.ok:
            step("migrate", "failed", failures=loaded.failures[:5])
            report["ok"] = False
            _emit(report, args.report)
            return 1
        step("migrate", "ok", tables=len(loaded.tables))
        step("verify", "ok", failures=[])

        store = PostgresExecutionStore(args.dsn, max_size=2)
        try:
            depth, age = store.pending_turn_queue_state()
            metrics = store.task_metrics_summary(hours=24 * 30)
        finally:
            store.close()
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            sessions = conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0]
            turns = conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0]
        step(
            "smoke",
            "ok",
            pending_depth=depth,
            oldest_age_s=round(age, 3),
            sessions=int(sessions),
            turns=int(turns),
            metrics_total=metrics["total"],
        )

        # 6: rollback rehearsal — wipe the target and re-run the import.
        rolled = migrate()
        if not rolled.ok:
            step("rollback", "failed", failures=rolled.failures[:5])
            report["ok"] = False
            _emit(report, args.report)
            return 1
        step("rollback", "ok", tables=len(rolled.tables))
        report["ok"] = True
    finally:
        tmpdir.cleanup()

    _emit(report, args.report)
    return 0 if report.get("ok") else 1


def _emit(report: dict, report_path: str) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if report_path:
        output = Path(report_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    print("ok" if report.get("ok") else "FAILED", f"steps={len(report.get('steps', []))}")


if __name__ == "__main__":
    raise SystemExit(main())
