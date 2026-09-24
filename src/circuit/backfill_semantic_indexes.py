"""Staged backfill of semantic indexes over existing circuit states.

Safety properties (plan task 7):
- Requires an explicit department scope; only designs whose publication
  metadata matches that department are touched.
- Each design is republished through the existing write-lock + atomic snapshot
  mechanism, so a failure keeps the previous complete generation and marks the
  failure reason in the report.
- Designs without governed role sources still get a structure coverage
  summary; role assertions and datasheet links simply stay empty.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def backfill_semantic_indexes(
    kb_name: str,
    *,
    department_id: str | None = None,
    design_ids: list[str] | None = None,
    service: Any,
    catalog_entries: list[Any] | None = None,
) -> dict[str, Any]:
    """Rebuild identity/coverage/link projections for authorized designs.

    Returns a bounded report: processed ids, per-design link counts and
    failure reasons. Raw EDF payloads and existing graph artifacts are only
    ever touched through :meth:`CircuitIndexService.reindex_stored_design`.
    """
    if not str(department_id or "").strip():
        raise ValueError("backfill requires an explicit department_id scope.")
    if service is None:
        raise ValueError("backfill requires a CircuitIndexService instance.")

    processed: list[str] = []
    failures: dict[str, str] = {}
    link_counts: dict[str, int] = {}

    candidates = list(design_ids or [])
    if not candidates:
        for design in service.store.list_designs(kb_name):
            metadata = service._read_metadata(kb_name, design.design_id)
            if str(metadata.get("department_id") or "") == str(department_id):
                candidates.append(design.design_id)

    # Each design republishes through ``reindex_stored_design``, which takes
    # the root write lock itself (the file lock is not reentrant) and performs
    # the atomic snapshot/rollback around every publication.
    for design_id in sorted(candidates):
        metadata = service._read_metadata(kb_name, design_id)
        if str(metadata.get("department_id") or "") != str(department_id):
            # Authorization scope changed since enumeration: skip silently
            # but record it so operators see the narrowing.
            failures[design_id] = "department_scope_mismatch"
            continue
        try:
            service.reindex_stored_design(kb_name, design_id)
        except Exception as exc:
            logger.warning(
                "Backfill failed for %s/%s: %s; previous generation kept.",
                kb_name, design_id, exc,
            )
            failures[design_id] = str(exc)
            continue
        processed.append(design_id)
        try:
            links = service.datasheet_link_index.load_links(kb_name, design_id)
            link_counts[design_id] = len(links)
        except Exception:
            link_counts[design_id] = 0

    return {
        "kb_name": kb_name,
        "department_id": str(department_id),
        "processed": processed,
        "failures": failures,
        "link_counts": link_counts,
    }
