"""Stability drill for the control plane.

Checks the things that made the platform look "unstable" during development:

- isolated (uncertain) resource permits that block admission
- non-terminal chat turns (stuck streaming/pending work)
- multiple chat worker deployments sharing one queue

Usage:
    uv run python scripts/check_stability.py

Exit code 0 means all checks are clean; 1 means action is required.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import src.settings


def _uncertain_permits() -> list[dict]:
    from src.core.concurrency import get_governor

    governor = get_governor()
    if governor.store is None:
        return []
    return [
        {"resource": item["resource"], "permit_id": str(item["permit_id"])}
        for item in governor.store.uncertain_permits()
    ]


def _non_terminal_turns() -> int:
    if str(getattr(src.settings, "CONTROL_PLANE_BACKEND", "sqlite")).lower() == "postgres":
        import psycopg

        dsn = str(getattr(src.settings, "CONTROL_PLANE_DATABASE_URL", "") or "")
        if not dsn:
            return -1
        with psycopg.connect(dsn) as conn:
            return int(
                conn.execute(
                    "SELECT count(*) FROM chat_turns "
                    "WHERE status NOT IN ('completed','failed','cancelled')"
                ).fetchone()[0]
            )
    path = str(getattr(src.settings, "AUTH_DB_PATH", ""))
    if not path or not os.path.exists(path):
        return -1
    with closing(sqlite3.connect(path)) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM chat_turns "
                "WHERE status NOT IN ('completed','failed','cancelled')"
            ).fetchone()[0]
        )


def _chat_workers() -> list[str]:
    try:
        result = subprocess.run(
            ["pgrep", "-af", "[h]ardware-database-worker --role chat"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _runtime_workers(workers: list[str]) -> list[str]:
    """Keep actual Python runtime processes (drop uv/docker-init wrappers)."""

    return [line for line in workers if ".venv/bin/python" in line]


def _deployments(workers: list[str]) -> list[str]:
    kinds = set()
    for line in workers:
        if "/app/.venv" in line:
            kinds.add("container(/app)")
        elif "/home/" in line:
            kinds.add("host")
    return sorted(kinds)


def main() -> int:
    uncertain = _uncertain_permits()
    non_terminal = _non_terminal_turns()
    workers = _runtime_workers(_chat_workers())
    deployments = _deployments(workers)
    report = {
        "uncertain_permits": uncertain,
        "non_terminal_turns": non_terminal,
        "chat_worker_processes": len(workers),
        "worker_deployments": deployments,
        "clean": not uncertain and non_terminal == 0 and len(deployments) <= 1,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if uncertain:
        print(
            "ACTION: release uncertain permits with evidence (see "
            "scripts/production_hardening/governor_recovery.py).",
            file=sys.stderr,
        )
    if non_terminal != 0:
        print("ACTION: inspect stuck turns / worker heartbeats.", file=sys.stderr)
    if len(deployments) > 1:
        print(
            "ACTION: stop the extra worker deployment; one queue must have one "
            "code version.",
            file=sys.stderr,
        )
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
