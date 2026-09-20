"""Operations tool for deployment-scope resource permits (P0-006).

Subcommands:

- ``list``: dump active/uncertain permit counts and escalated resources.
- ``show --permit-id``: one permit plus its audit trail.
- ``reconcile``: reap expired leases and escalate long-isolated permits.
- ``release --permit-id --evidence ...``: evidence-backed CAS release of one
  uncertain permit.  Missing/unknown evidence is refused; every release writes
  an audit row with operator, reason and previous state.

The tool never clears counters wholesale and never auto-releases on client
timeouts: only server-verified completion/cancel/deadline evidence is accepted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import src.settings  # noqa: E402
from src.core.concurrency import SqliteQuotaStore  # noqa: E402

EVIDENCE_CHOICES = sorted(SqliteQuotaStore.VALID_RELEASE_EVIDENCE)


def _detect_deployment_id(path: str, override: str) -> str:
    """Use the identity recorded in the quota file; override only when given."""
    if override:
        return override
    if os.path.isfile(path):
        import sqlite3

        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT value FROM resource_governor_meta WHERE key = 'deployment_id'"
                ).fetchone()
                if row and row[0]:
                    return str(row[0])
        except sqlite3.Error:
            pass
    return str(getattr(src.settings, "RESOURCE_GOVERNOR_DEPLOYMENT_ID", "local"))


def _store(args: argparse.Namespace) -> SqliteQuotaStore:
    return SqliteQuotaStore(
        args.db,
        lease_seconds=int(getattr(src.settings, "RESOURCE_PERMIT_LEASE_SECONDS", 300)),
        deployment_id=_detect_deployment_id(args.db, args.deployment_id),
        config_revision="",
        require_preconfigured=False,
    )


def _print(payload: dict, args: argparse.Namespace) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(REPO_ROOT / "storage" / "resource_quotas.db"),
        help="quota database path",
    )
    parser.add_argument("--deployment-id", default="")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--output", default="", help="JSON report path; defaults to stdout")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="summarize permit state", parents=[common])

    show = sub.add_parser("show", help="show one permit with its audit trail", parents=[common])
    show.add_argument("--permit-id", required=True)

    reconcile = sub.add_parser(
        "reconcile", help="reap expired leases and escalate isolates", parents=[common]
    )
    reconcile.add_argument(
        "--escalate-seconds",
        type=int,
        default=int(getattr(src.settings, "RESOURCE_UNCERTAIN_ESCALATE_SECONDS", 900)),
    )

    release = sub.add_parser(
        "release", help="evidence-backed release of an uncertain permit", parents=[common]
    )
    release.add_argument("--permit-id", required=True)
    release.add_argument("--evidence", required=True, choices=EVIDENCE_CHOICES)
    release.add_argument("--reason", default="")
    release.add_argument("--operator", default="")
    release.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    store = _store(args)

    if args.command == "list":
        payload = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "db": store.path,
            "deployment_id": store.deployment_id,
            "snapshot": store.snapshot(),
            "escalated_resources": sorted(store.escalated_resources()),
            "uncertain": [
                {
                    "permit_id": row["permit_id"],
                    "resource": row["resource"],
                    "owner_id": row["owner_id"],
                    "uncertain_since": row["uncertain_since"],
                    "escalated_at": row["escalated_at"],
                }
                for row in store.uncertain_permits()
            ],
        }
        _print(payload, args)
        return 0

    if args.command == "show":
        rows = store.uncertain_permits()
        permit = next((row for row in rows if row["permit_id"] == args.permit_id), None)
        payload = {
            "permit_id": args.permit_id,
            "permit": permit,
            "audit": store.audit_entries(args.permit_id),
        }
        _print(payload, args)
        return 0 if permit is not None or payload["audit"] else 1

    if args.command == "reconcile":
        result = store.maintain(args.escalate_seconds)
        payload = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "reaped": result["reaped"],
            "escalated": result["escalated"],
            "escalated_resources": sorted(store.escalated_resources()),
            "snapshot": store.snapshot(),
        }
        _print(payload, args)
        return 0

    if args.command == "release":
        if args.dry_run:
            rows = {row["permit_id"]: row for row in store.uncertain_permits()}
            payload = {
                "dry_run": True,
                "permit_id": args.permit_id,
                "current": rows.get(args.permit_id),
                "would_release": rows.get(args.permit_id) is not None,
                "evidence": args.evidence,
            }
            _print(payload, args)
            return 0
        released = store.release_uncertain(
            args.permit_id,
            evidence=args.evidence,
            reason=args.reason,
            operator=args.operator,
        )
        payload = {
            "permit_id": args.permit_id,
            "released": released,
            "evidence": args.evidence,
            "operator": args.operator,
            "audit": store.audit_entries(args.permit_id),
        }
        _print(payload, args)
        return 0 if released else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
