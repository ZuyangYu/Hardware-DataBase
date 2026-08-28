"""LangGraph Store runtime and the Catalog-aware manager write gate."""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    ListNamespacesOp,
    PutOp,
    SearchItem,
    SearchOp,
)
from langgraph.store.sqlite import SqliteStore

from src.memory.catalog import MemoryCatalogRepository, MemoryProjection
from src.memory.schemas import MEMORY_SCHEMA_VERSION, content_hash
from src.observability.metrics import counter, set_memory_index_health

import logging
import time

_logger = logging.getLogger("RAG")


@dataclass(frozen=True)
class CapturedPut:
    namespace: tuple[str, ...]
    key: str
    value: dict[str, Any]
    projection_id: str | None
    expected_fence: int | None


@dataclass(frozen=True)
class CapturedDelete:
    namespace: tuple[str, ...]
    key: str
    projection_id: str | None
    expected_fence: int | None


class MemoryStoreRuntime:
    """Owns one official ``SqliteStore`` and its connection lifecycle."""

    def __init__(
        self,
        *,
        path: str,
        index: dict[str, Any] | None = None,
        busy_timeout_ms: int = 30_000,
    ):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(
            self.path,
            timeout=max(1, int(busy_timeout_ms)) / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(f"PRAGMA busy_timeout={max(1, int(busy_timeout_ms))}")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.semantic_index_ready = False
        self.index_error = ""
        try:
            # Version-drift guard for langgraph-checkpoint-sqlite==3.1.1
            # (LangMem V2 §21): the public ``IndexConfig`` documents
            # ``fields``, but SqliteStore only reads ``text_fields``.
            # Normalizing here prevents a silent whole-object index fallback;
            # tests/test_memory_store_index_contract.py blocks regressions.
            index_config = dict(index) if index else None
            if index_config is not None:
                fields = index_config.get("fields")
                text_fields = index_config.setdefault("text_fields", fields)
                if text_fields:
                    index_config["text_fields"] = [str(field) for field in text_fields]
                    index_config.pop("fields", None)
            self.store = SqliteStore(self.conn, index=index_config)
            self.store.setup()
            self.semantic_index_ready = bool(index)
            set_memory_index_health(
                backend="sqlite",
                healthy=True,
                semantic_index=self.semantic_index_ready,
            )
        except Exception as exc:
            # A bad embedding/index configuration must not brick chat or the
            # control plane.  Re-open the official Store without semantic
            # search; MemoryService will skip Candidate semantic retrieval.
            self.index_error = str(exc)[:500]
            try:
                self.conn.rollback()
            except Exception:
                pass
            self.store = SqliteStore(self.conn)
            self.store.setup()
            set_memory_index_health(backend="sqlite", healthy=False, semantic_index=False)

    def close(self) -> None:
        with self.lock:
            close = getattr(self.store, "close", None)
            if callable(close):
                close()
            self.conn.close()

    def health(self) -> dict[str, Any]:
        with self.lock:
            try:
                self.conn.execute("SELECT 1").fetchone()
                set_memory_index_health(
                    backend="sqlite",
                    healthy=True,
                    semantic_index=self.semantic_index_ready,
                )
                return {"ok": True, "semantic_index": self.semantic_index_ready, "error": self.index_error}
            except Exception as exc:
                set_memory_index_health(backend="sqlite", healthy=False, semantic_index=False)
                return {"ok": False, "semantic_index": False, "error": str(exc)[:500]}

    def _run_with_busy_retry(self, operation: str, fn, /, *args, **kwargs):
        """Retry transient SQLite busy/locked failures a bounded number of times.

        WAL + busy_timeout already serialize normal contention; this covers the
        rare case where an external process holds the database longer than the
        configured timeout.  Each retry is counted for observability.
        """
        attempts = 3
        delay = 0.05
        for attempt in range(1, attempts + 1):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if ("locked" not in message and "busy" not in message) or attempt == attempts:
                    raise
                try:
                    counter("hdb.memory.sqlite_busy_retries", attributes={"operation": operation})
                    _logger.warning(
                        "hdb.memory.sqlite_busy_retry operation=%s attempt=%s error=%s",
                        operation,
                        attempt,
                        str(exc)[:120],
                    )
                except Exception:
                    pass
                time.sleep(delay * attempt)

    def put(self, namespace: tuple[str, ...], key: str, value: dict[str, Any], *, index: Any = None) -> None:
        with self.lock:
            self._run_with_busy_retry("put", self.store.put, namespace, str(key), value, index=index)

    def delete(self, namespace: tuple[str, ...], key: str) -> None:
        with self.lock:
            self._run_with_busy_retry("delete", self.store.delete, namespace, str(key))

    def search(self, namespace: tuple[str, ...], *, query: str | None = None, limit: int = 10, offset: int = 0, filter: dict[str, Any] | None = None) -> list[SearchItem]:
        with self.lock:
            return self._run_with_busy_retry(
                "search",
                self.store.search,
                namespace,
                query=query,
                limit=limit,
                offset=offset,
                filter=filter,
            )

    def list_namespaces(self, *, prefix: tuple[str, ...] | None = None, limit: int = 100, offset: int = 0) -> list[tuple[str, ...]]:
        with self.lock:
            return [
                tuple(str(part) for part in namespace)
                for namespace in self.store.list_namespaces(
                    prefix=prefix,
                    max_depth=10,
                    limit=max(1, int(limit)),
                    offset=max(0, int(offset)),
                )
            ]


def _resolve_settings(settings=None):
    if settings is not None:
        return settings
    import config.settings as settings_module

    return settings_module


def _absolute_storage_path(raw_path: str | None, storage_dir: str) -> str:
    path = str(raw_path or "memory.db").strip()
    if not os.path.isabs(path):
        path = os.path.join(storage_dir, path)
    return os.path.abspath(path)


def _create_embedding(settings):
    provider = str(getattr(settings, "MEMORY_EMBEDDING_PROVIDER", "") or "").strip().lower()
    model = str(getattr(settings, "MEMORY_EMBEDDING_MODEL", "") or "").strip()
    if not provider or not model:
        return None
    if provider in {"openai", "custom"}:
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=model,
            api_key=str(getattr(settings, "MEMORY_EMBEDDING_API_KEY", "") or "") or None,
            base_url=str(getattr(settings, "MEMORY_EMBEDDING_BASE_URL", "") or "") or None,
            # OpenAI-compatible gateways (e.g. Ark) reject token-array input;
            # send raw strings like the RAGAS adapter does.
            check_embedding_ctx_length=False,
        )
    if provider == "ollama":
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(
            model=model,
            base_url=str(getattr(settings, "MEMORY_EMBEDDING_BASE_URL", "") or "") or None,
        )
    raise ValueError(f"unsupported memory embedding provider: {provider}")


def create_memory_store(settings=None) -> MemoryStoreRuntime:
    """Create the configured projection backend.

    Postgres is deliberately explicit: selecting it before the controlled
    migration is an error, never an implicit empty database.
    """
    settings = _resolve_settings(settings)
    backend = str(getattr(settings, "MEMORY_STORE_BACKEND", "sqlite") or "sqlite").strip().lower()
    if backend == "postgres":
        raise RuntimeError("Postgres memory projection requires the Phase 2 migration runbook")
    if backend != "sqlite":
        raise ValueError(f"unsupported memory store backend: {backend}")
    storage_dir = str(getattr(settings, "STORAGE_DIR", os.getcwd()))
    path = _absolute_storage_path(getattr(settings, "MEMORY_SQLITE_PATH", "memory.db"), storage_dir)
    dims_raw = getattr(settings, "MEMORY_EMBEDDING_DIMS", "")
    try:
        dims = int(dims_raw) if str(dims_raw or "").strip() else 0
    except (TypeError, ValueError):
        dims = 0
    index: dict[str, Any] | None = None
    if dims > 0:
        embedding = _create_embedding(settings)
        if embedding is not None:
            raw_fields = str(getattr(settings, "MEMORY_INDEX_FIELDS", "content.content,content.title,content.subject") or "")
            fields = [field.strip() for field in raw_fields.split(",") if field.strip()]
            index = {"dims": dims, "embed": embedding, "fields": fields or ["content.content"]}
    return MemoryStoreRuntime(
        path=path,
        index=index,
        busy_timeout_ms=int(getattr(settings, "MEMORY_SQLITE_BUSY_TIMEOUT_MS", 30_000)),
    )


class CatalogAwareStore(BaseStore):
    """Restrict LangMem to one complete Candidate namespace.

    ``put`` and ``delete`` are captured rather than sent directly to the
    physical Store.  The Worker persists an operation plan and routes the
    actual write through Projection Outbox, where revision/fence CAS can be
    checked.  This is the safe Phase 1 fallback for LangMem's current
    in-place-update API.
    """

    def __init__(
        self,
        runtime: MemoryStoreRuntime,
        catalog: MemoryCatalogRepository,
        namespace: tuple[str, ...],
        *,
        max_scan: int = 100,
        oversample_factor: int = 4,
    ):
        super().__init__()
        if not namespace or any(part in (None, "") for part in namespace):
            raise ValueError("CatalogAwareStore requires a complete namespace")
        if namespace[-1] != "candidate":
            raise ValueError("LangMem may only access the candidate namespace")
        self.runtime = runtime
        self.catalog = catalog
        self.namespace = tuple(str(part) for part in namespace)
        self.max_scan = max(1, int(max_scan))
        self.oversample_factor = max(1, int(oversample_factor))
        self.captured_puts: list[CapturedPut] = []
        self.captured_deletes: list[CapturedDelete] = []
        self._capture_lock = threading.RLock()

    def clear_capture(self) -> None:
        with self._capture_lock:
            self.captured_puts.clear()
            self.captured_deletes.clear()

    def _validate_namespace(self, namespace: tuple[str, ...]) -> None:
        if tuple(namespace) != self.namespace:
            raise PermissionError("LangMem namespace does not match the server-owned Candidate scope")

    def _allowed_projection(self, key: str) -> MemoryProjection | None:
        projection = self.catalog.get_projection_by_key("sqlite", self.namespace, str(key))
        if projection is None or not projection.active and not projection.manager_writable:
            # New manager keys are allowed only as captured staging writes;
            # existing retired/orphan keys must never be handed to LangMem.
            return None
        if projection.projection_kind != "candidate" or not projection.manager_writable or projection.retired_at is not None:
            return None
        record = self.catalog.get_record(projection.memory_id)
        if record is None or record.status != "candidate":
            return None
        return projection

    def search(
        self,
        namespace_prefix: tuple[str, ...],
        /,
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
        refresh_ttl: bool | None = None,
    ) -> list[SearchItem]:
        self._validate_namespace(namespace_prefix)
        wanted = max(1, min(int(limit), self.max_scan))
        cursor = max(0, int(offset))
        accepted: list[SearchItem] = []
        scanned = 0
        page_size = max(wanted * self.oversample_factor, wanted)
        while scanned < self.max_scan and len(accepted) < wanted:
            with self.runtime.lock:
                raw = self.runtime.store.search(
                    self.namespace,
                    query=query,
                    filter=filter,
                    limit=min(page_size, self.max_scan - scanned),
                    offset=cursor,
                    refresh_ttl=refresh_ttl,
                )
            if not raw:
                break
            cursor += len(raw)
            scanned += len(raw)
            for item in raw:
                if tuple(item.namespace) != self.namespace:
                    continue
                projection = self._allowed_projection(item.key)
                if projection is None:
                    continue
                value = item.value if isinstance(item.value, dict) else {}
                semantic = value.get("content") if isinstance(value.get("content"), dict) else None
                if semantic is None:
                    continue
                expected_hash = content_hash(semantic, schema_version=str(value.get("schema_version") or MEMORY_SCHEMA_VERSION))
                if expected_hash != projection.current_content_hash:
                    continue
                record = self.catalog.get_record(projection.memory_id)
                if record is None or record.content_hash != expected_hash:
                    continue
                accepted.append(item)
                if len(accepted) >= wanted:
                    break
            if len(raw) < page_size:
                break
        return accepted

    def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        index: Any = None,
        *,
        ttl: float | None = None,
    ) -> None:
        self._validate_namespace(namespace)
        if not isinstance(value, dict) or not isinstance(value.get("content"), dict):
            raise ValueError("LangMem projection value must contain a semantic content object")
        projection = self._allowed_projection(str(key))
        if projection is not None:
            expected_fence = projection.fence_version
            projection_id = projection.projection_id
        else:
            # LangMem is allowed to propose a new key; Catalog assigns the
            # logical memory_id and projection key after output persistence.
            expected_fence = None
            projection_id = None
        with self._capture_lock:
            self.captured_puts.append(
                CapturedPut(tuple(namespace), str(key), dict(value), projection_id, expected_fence)
            )

    def delete(self, namespace: tuple[str, ...], key: str) -> None:
        self._validate_namespace(namespace)
        projection = self._allowed_projection(str(key))
        with self._capture_lock:
            self.captured_deletes.append(
                CapturedDelete(tuple(namespace), str(key), projection.projection_id if projection else None, projection.fence_version if projection else None)
            )

    def get(self, namespace: tuple[str, ...], key: str, *, refresh_ttl: bool | None = None):
        self._validate_namespace(namespace)
        if self._allowed_projection(str(key)) is None:
            return None
        with self.runtime.lock:
            return self.runtime.store.get(namespace, str(key), refresh_ttl=refresh_ttl)

    def batch(self, ops):
        """Implement BaseStore's abstract method for LangGraph callers."""
        results = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self.get(op.namespace, op.key, refresh_ttl=op.refresh_ttl))
            elif isinstance(op, SearchOp):
                results.append(self.search(op.namespace_prefix, query=op.query, filter=op.filter, limit=op.limit, offset=op.offset, refresh_ttl=op.refresh_ttl))
            elif isinstance(op, PutOp):
                if op.value is None:
                    self.delete(op.namespace, op.key)
                else:
                    self.put(op.namespace, op.key, op.value, index=op.index, ttl=op.ttl)
                results.append(None)
            elif isinstance(op, ListNamespacesOp):
                results.append([])
            else:
                raise TypeError(f"unsupported operation for CatalogAwareStore: {type(op).__name__}")
        return results

    async def abatch(self, ops):
        """Async BaseStore contract backed by the synchronous Phase 1 store.

        LangMem's Phase 1 worker is synchronous.  Keeping this method here
        makes the adapter a complete official ``BaseStore`` implementation for
        callers that use the async Runnable surface without mixing in an
        ``AsyncSqliteStore`` connection.
        """
        return self.batch(ops)


__all__ = [
    "CapturedDelete",
    "CapturedPut",
    "CatalogAwareStore",
    "MemoryStoreRuntime",
    "create_memory_store",
]
