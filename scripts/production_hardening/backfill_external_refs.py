#!/usr/bin/env python
"""Backfill opaque External refs for existing KBs and document records.

The P0 External API creates mappings lazily, but a migration can use this
script to make existing resources discoverable ahead of opening the feature.
The operation is idempotent: existing active refs are returned unchanged and
never reuse an internal integer ID as the external value.

Usage:
    uv run python scripts/production_hardening/backfill_external_refs.py
    uv run python scripts/production_hardening/backfill_external_refs.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.core.auth import AuthService  # noqa: E402
from src.pipelines.document_store_sqlite import PipelineDocumentStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写映射")
    args = parser.parse_args(argv)

    auth = AuthService()
    store = PipelineDocumentStore()
    kb_count = 0
    file_count = 0
    skipped = 0
    failures: list[str] = []
    for kb in auth.list_all_knowledge_base_identities():
        if kb.department_id is None:
            skipped += 1
            continue
        try:
            if not args.dry_run:
                auth.ensure_external_kb_ref(kb.kb_id)
            kb_count += 1
        except Exception as exc:  # noqa: BLE001 - report per-resource failures
            failures.append(f"kb#{kb.kb_id}: {type(exc).__name__}: {exc}")
            continue
        try:
            records = store.list_documents(
                kb.kb_name, department_id=kb.department_id
            )
        except Exception as exc:  # noqa: BLE001 - keep processing other KBs
            failures.append(f"kb#{kb.kb_id}: list failed: {type(exc).__name__}: {exc}")
            continue
        for record in records:
            if record.kb_id not in (0, None, kb.kb_id):
                continue
            try:
                if not args.dry_run:
                    auth.ensure_external_file_ref(kb.kb_id, record.id)
                file_count += 1
            except Exception as exc:  # noqa: BLE001 - report per-resource failures
                failures.append(
                    f"kb#{kb.kb_id}/record#{record.id}: {type(exc).__name__}: {exc}"
                )

    print(
        f"[external-backfill] {'dry-run ' if args.dry_run else ''}"
        f"kbs={kb_count} files={file_count} skipped={skipped} failures={len(failures)}"
    )
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
