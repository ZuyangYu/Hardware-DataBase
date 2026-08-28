"""Rebuild helpers for the rebuildable LangGraph projection.

Phase 1 deliberately has no implicit Postgres cutover.  A migration/rebuild
operation may rehydrate only Catalog-authorized active projections; it never
discovers Store keys and never changes Catalog lifecycle state by itself.
"""

from __future__ import annotations

import json

from src.memory.catalog import MemoryCatalogRepository, memory_content_hash
from src.memory.store import MemoryStoreRuntime


def rebuild_active_projections(
    runtime: MemoryStoreRuntime,
    catalog: MemoryCatalogRepository,
    *,
    max_items: int = 5_000,
) -> int:
    """Idempotently rehydrate active Catalog projections into the Store."""

    budget = max(1, min(int(max_items), 50_000))
    rows = catalog.conn.execute(
        """SELECT p.*, r.status, r.content_hash, r.memory_type, r.schema_version, r.content_json
           FROM memory_projections p
           JOIN memory_records r ON r.memory_id = p.memory_id
           WHERE p.active = 1 AND p.retired_at IS NULL
             AND r.status IN ('candidate', 'verified')
           ORDER BY p.created_at, p.projection_id
           LIMIT ?""",
        (budget,),
    ).fetchall()
    rebuilt = 0
    for row in rows:
        semantic = json.loads(row["content_json"] or "{}")
        if not isinstance(semantic, dict):
            continue
        schema_version = str(row["schema_version"] or "1")
        if memory_content_hash(semantic, schema_version=schema_version) != row["content_hash"]:
            continue
        runtime.put(
            tuple(str(part) for part in json.loads(row["namespace_json"] or "[]")),
            str(row["store_key"]),
            {
                "kind": str(row["memory_type"] or "context"),
                "content": semantic,
                "schema_version": schema_version,
            },
        )
        rebuilt += 1
    return rebuilt


__all__ = ["rebuild_active_projections"]
