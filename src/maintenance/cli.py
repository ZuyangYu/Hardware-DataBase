"""Maintenance CLI: derived-data backfills that do not touch source files.

Run through ``uv run hardware-database-maintenance <command>``.  The parser
backfill only re-enqueues parse jobs; source attachment bytes are never
modified, and re-running is idempotent while a job is live.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.attachments.service import AttachmentService


def _reparse_attachments(args: argparse.Namespace) -> int:
    service = AttachmentService()
    extensions = [item.strip().lower() for item in (args.extension or []) if item.strip()]
    if args.dry_run:
        assets = service.list_stale_assets(extensions=extensions or None, limit=args.limit)
        payload = {
            "dry_run": True,
            "scanned": len(assets),
            "assets": [
                {
                    "asset_id": asset.asset_id,
                    "session_id": asset.session_id,
                    "extension": asset.extension,
                    "parser_version": asset.parser_version,
                }
                for asset in assets
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    result = service.reparse_stale_assets(
        extensions=extensions or None,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["scanned"] == 0 or result["queued"] > 0 else 1


def _compatibility_readiness(args: argparse.Namespace) -> int:
    from src.document_authoring.compatibility import (
        AUTO_CONFIRMATION_ATTEMPT,
        LEGACY_DIRECT_EXECUTION,
        LEGACY_WRITE,
        NEW_WRITE,
        PLAN_BACKED_EXECUTION,
        DocumentAuthoringCompatibilityService,
    )

    service = DocumentAuthoringCompatibilityService()
    report = service.evaluate_closure_readiness()
    payload = {
        "ready": report.ready,
        "reasons": report.reasons,
        "release_count": report.release_count,
        "backup_verified": report.backup_verified,
        "restore_verified": report.restore_verified,
        "old_fields_read_only": report.old_fields_read_only,
        "no_new_legacy_writes": report.no_new_legacy_writes,
        "closure_flag_enabled": service.closure_enabled,
        "legacy_markers": len(service.store.list_markers()),
        "event_counts": {
            LEGACY_WRITE: service.store.count_events(LEGACY_WRITE),
            NEW_WRITE: service.store.count_events(NEW_WRITE),
            LEGACY_DIRECT_EXECUTION: service.store.count_events(LEGACY_DIRECT_EXECUTION),
            PLAN_BACKED_EXECUTION: service.store.count_events(PLAN_BACKED_EXECUTION),
            AUTO_CONFIRMATION_ATTEMPT: service.store.count_events(AUTO_CONFIRMATION_ATTEMPT),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.ready else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hardware-database-maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)

    reparse = subparsers.add_parser(
        "reparse-attachments",
        help="re-enqueue parse jobs for assets created by an older parser",
    )
    reparse.add_argument(
        "--stale",
        action="store_true",
        help="only process assets older than the current parser generation (default)",
    )
    reparse.add_argument("--limit", type=int, default=200)
    reparse.add_argument(
        "--extension",
        action="append",
        default=[],
        help="limit to an extension (repeatable), e.g. --extension .docx",
    )
    reparse.add_argument(
        "--dry-run",
        action="store_true",
        help="list the stale assets without enqueueing jobs",
    )
    reparse.set_defaults(handler=_reparse_attachments)

    readiness = subparsers.add_parser(
        "compatibility-readiness",
        help="report the document-authoring legacy closure gates before flipping the flag",
    )
    readiness.set_defaults(handler=_compatibility_readiness)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
