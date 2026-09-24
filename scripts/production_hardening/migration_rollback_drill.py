"""Migration rollback/idempotency drill for the SQLite->PostgreSQL tool.

Validates the rollback story that the runbook promises without cutting over:

1. the source database hash is identical before and after migration (the tool
   must be read-only on the source);
2. a second copy pass is idempotent: zero rows copied, counts and content
   hashes still match;
3. derived tables stay skipped on every pass.

With ``--kill-after <seconds>`` the first attempt is SIGKILLed mid-flight to
leave a partially migrated target; the following full pass must converge with
matching counts and hashes (failure recovery to a clear point).

Requires a reachable PostgreSQL (``--dsn``) and a source group database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE = REPO_ROOT / "storage"
MIGRATE = REPO_ROOT / "scripts" / "production_hardening" / "migrate_sqlite_to_postgres.py"
GROUP_DATABASES = {
    "authority": "auth.db",
    "projection": "memory.db",
    "attachments": "chat_attachments.db",
    "pipeline": "pipeline_documents.db",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_sqlite(source: Path, target: Path) -> None:
    """Consistent copy of a live SQLite database via the backup API."""
    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(str(target))
    try:
        origin.backup(destination)
    finally:
        destination.close()
        origin.close()


def _run_migration(group: str, dsn: str, report: Path) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            str(MIGRATE),
            "--group",
            group,
            "--dsn",
            dsn,
            "--report",
            str(report),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"migration failed rc={completed.returncode}\n{completed.stdout}\n{completed.stderr}"
        )
    return json.loads(report.read_text(encoding="utf-8"))


def _run_migration_chaos(
    group: str,
    dsn: str,
    report: Path,
    kill_after: float,
) -> tuple[bool, int | None]:
    """Start a migration and SIGKILL it mid-flight (partial target state).

    Small groups can finish inside the initial window, so the window shrinks
    until a run is actually interrupted. Returns
    ``(killed, returncode, window_seconds)``; the target database is left
    partially migrated so the follow-up full pass proves convergence.
    """

    window = kill_after
    last_returncode: int | None = None
    while window > 0.01:
        process = subprocess.Popen(
            [
                sys.executable,
                str(MIGRATE),
                "--group",
                group,
                "--dsn",
                dsn,
                "--report",
                str(report),
            ],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            process.communicate(timeout=window)
            last_returncode = process.returncode
            if process.returncode != 0:
                raise RuntimeError(f"chaos migration failed rc={process.returncode}")
            window = round(window / 3, 4)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.communicate()
            return True, process.returncode, window
    return False, last_returncode, None


def _drill_group(
    group: str,
    *,
    source_override: str,
    dsn: str,
    direct: bool,
    kill_after: float = 0.0,
) -> dict:
    live_source = Path(source_override) if source_override else STORAGE / GROUP_DATABASES[group]
    if not live_source.is_file():
        return {"group": group, "ok": False, "error": f"source database not found: {live_source}"}

    temp_dir = tempfile.TemporaryDirectory()
    try:
        if direct:
            source = live_source
        else:
            # Runbook §7.5: rehearse on a consistent backup copy; live services
            # keep writing their own database and must not be touched.
            source = Path(temp_dir.name) / live_source.name
            _snapshot_sqlite(live_source, source)

        report: dict[str, object] = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "group": group,
            "live_source": str(live_source),
            "drilled_source": str(source),
            "mode": "direct" if direct else "snapshot-copy",
            "checks": {},
        }
        started = time.monotonic()
        before_hash = _sha256(source)
        first_path = Path(temp_dir.name) / "first.json"
        second_path = Path(temp_dir.name) / "second.json"
        if kill_after > 0:
            chaos_path = Path(temp_dir.name) / "chaos.json"
            killed, returncode, window = _run_migration_chaos(group, dsn, chaos_path, kill_after)
            report["chaos"] = {
                "initial_window_seconds": kill_after,
                "killed_window_seconds": window,
                "killed": killed,
                "returncode": returncode,
            }
            report["checks"]["chaos_failure_injected"] = killed
        first = _run_migration(group, dsn, first_path)
        second = _run_migration(group, dsn, second_path)
        after_hash = _sha256(source)
        report["checks"]["source_unchanged"] = before_hash == after_hash
        report["checks"]["first_ok"] = bool(first.get("ok"))
        report["checks"]["second_ok"] = bool(second.get("ok"))
        report["checks"]["second_copy_is_idempotent"] = all(
            int(item.get("copied") or 0) == 0
            for item in second.get("tables", [])
            if item.get("status") == "ok"
        )
        report["checks"]["hashes_still_match"] = not second.get("hash_mismatches")
        report["checks"]["derived_skipped"] = all(
            item.get("status") == "derived_skipped"
            for item in second.get("tables", [])
            if "_fts" in str(item.get("table")) or "_trigram" in str(item.get("table"))
        )
        report["source_sha256"] = before_hash
        report["tables"] = len(first.get("tables", []))
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        report["ok"] = all(bool(value) for value in report["checks"].values())
        return report
    finally:
        temp_dir.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=sorted(GROUP_DATABASES), default="authority")
    parser.add_argument(
        "--all-groups",
        action="store_true",
        help="drill every source database that exists (skips missing ones)",
    )
    parser.add_argument("--source", default="")
    parser.add_argument(
        "--kill-after",
        type=float,
        default=0.0,
        help="SIGKILL the first migration attempt after N seconds (0 disables)",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="drill the source in place (requires all writers stopped)",
    )
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    if args.all_groups:
        reports = [
            _drill_group(
                group,
                source_override=args.source if args.source and len(GROUP_DATABASES) == 1 else "",
                dsn=args.dsn,
                direct=args.direct,
                kill_after=args.kill_after,
            )
            for group in sorted(GROUP_DATABASES)
        ]
        combined = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "mode": "direct" if args.direct else "snapshot-copy",
            "dsn": args.dsn,
            "groups": {item["group"]: item for item in reports},
            "ok": all(bool(item.get("ok")) or "source database not found" in str(item.get("error", "")) for item in reports),
            "missing": [item["group"] for item in reports if "source database not found" in str(item.get("error", ""))],
        }
        text = json.dumps(combined, ensure_ascii=False, indent=2)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
            print(f"report={output}")
        else:
            print(text)
        print(f"ok={combined['ok']}")
        return 0 if combined["ok"] else 1

    report = _drill_group(
        args.group,
        source_override=args.source,
        dsn=args.dsn,
        direct=args.direct,
        kill_after=args.kill_after,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    print(f"ok={report['ok']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
