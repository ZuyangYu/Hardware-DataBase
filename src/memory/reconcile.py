"""Bounded reconciliation of the rebuildable LangGraph projection."""

from __future__ import annotations

from src.memory.catalog import ACTIVE_MEMORY_STATUSES, MemoryCatalogRepository, memory_content_hash
from src.memory.store import MemoryStoreRuntime


def reconcile_store(
    runtime: MemoryStoreRuntime,
    catalog: MemoryCatalogRepository,
    *,
    max_scan: int = 100,
) -> int:
    """Delete physical orphan/retired/hash-invalid objects.

    The Catalog remains authoritative.  This pass is deliberately bounded and
    only considers the two HDB projection leaves, so unrelated Store data is
    never touched.
    """

    budget = max(1, min(int(max_scan), 5_000))
    deleted = 0
    namespaces = runtime.list_namespaces(prefix=("hdb",), limit=budget)
    for namespace in namespaces:
        if len(namespace) < 2 or namespace[-1] not in {"candidate", "verified"}:
            continue
        scanned = 0
        while scanned < budget:
            page_size = min(100, budget - scanned)
            items = runtime.search(
                namespace,
                query=None,
                limit=page_size,
                offset=scanned,
            )
            if not items:
                break
            scanned += len(items)
            for item in items:
                projection = catalog.get_projection_by_key("sqlite", namespace, str(item.key))
                valid = projection is not None and projection.active and projection.retired_at is None
                if valid:
                    record = catalog.get_record(projection.memory_id)
                    value = item.value if isinstance(item.value, dict) else {}
                    semantic = value.get("content") if isinstance(value.get("content"), dict) else None
                    valid = (
                        record is not None
                        and record.status in ACTIVE_MEMORY_STATUSES
                        and semantic is not None
                        and memory_content_hash(
                            semantic,
                            schema_version=str(value.get("schema_version") or record.schema_version),
                        )
                        == projection.current_content_hash
                        and record.content_hash == projection.current_content_hash
                    )
                if not valid:
                    runtime.delete(namespace, str(item.key))
                    deleted += 1
            if len(items) < page_size:
                break
    return deleted


__all__ = ["reconcile_store"]
