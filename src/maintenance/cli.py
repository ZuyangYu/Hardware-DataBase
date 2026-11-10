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




def _verify_pipeline_schema(args: argparse.Namespace) -> int:
    from src.services.pipeline_reconciliation import pipeline_schema_report

    report = pipeline_schema_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["ok"] else 1


def _reconcile_pipeline(args: argparse.Namespace) -> int:
    from src.services.pipeline_reconciliation import reconcile_pipeline

    report = reconcile_pipeline(
        kb_name=args.kb,
        department_id=str(args.department_id),
        deep=not args.fast,
        limit=args.limit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hardware-database-maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)

    reconcile = subparsers.add_parser(
        "reconcile-pipeline",
        help="report missing archives, orphan directories and orphan archive files",
    )
    reconcile.add_argument("--kb", required=True)
    reconcile.add_argument("--department-id", required=True)
    reconcile.add_argument("--limit", type=int, default=200)
    reconcile.add_argument(
        "--fast", action="store_true", help="skip walking the archive tree for orphan files"
    )
    reconcile.set_defaults(handler=_reconcile_pipeline)

    verify = subparsers.add_parser(
        "verify-pipeline-schema",
        help="verify directory-aware document schema, root backfill and duplicate paths",
    )
    verify.set_defaults(handler=_verify_pipeline_schema)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
