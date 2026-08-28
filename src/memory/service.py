"""Application boundary for long-term memory.

The service is intentionally the only place where request-scoped identity is
turned into a memory namespace.  LangMem and the physical LangGraph store are
implementation details behind this boundary; callers receive Catalog records
and never need to know a namespace or Store key.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import config.settings as settings
from langgraph.store.base import SearchItem

from src.core.auth import ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN, AuthService, AuthUser
from src.memory.catalog import (
    ACTIVE_MEMORY_STATUSES,
    MemoryCatalogRepository,
    MemoryProjection,
    MemoryRecord,
    ensure_memory_schema,
    json_dumps,
    json_loads,
    memory_content_hash,
    namespace_for_project,
    namespace_for_user,
    row_to_record,
    scope_fingerprint,
    utc_now,
)
from src.memory.jobs import MemoryJobRepository
from src.memory.formatter import format_memory_context
from src.memory.schemas import (
    MEMORY_SCHEMA_VERSION,
    MemoryConsentManifest,
    MemoryConsentSourceItem,
    ProjectMemory,
    UserMemory,
    content_hash,
)
from src.memory.store import MemoryStoreRuntime, create_memory_store
from src.observability import observe
from src.observability.metrics import counter, histogram


class MemoryServiceError(RuntimeError):
    """Base error for memory operations."""


class MemoryNotFound(MemoryServiceError, LookupError):
    pass


class MemoryAuthorizationError(PermissionError, MemoryServiceError):
    pass


@dataclass(frozen=True)
class MemoryScope:
    kind: str
    user_id: str | None = None
    department_id: str | None = None
    kb_id: str | None = None

    def namespace(self, projection_kind: str) -> tuple[str, ...]:
        if self.kind == "user":
            return namespace_for_user(self.user_id, projection_kind)
        return namespace_for_project(self.department_id, self.kb_id, projection_kind)


def message_content_hash(role: str, content: str) -> str:
    """Hash the exact role/content pair used by a consent manifest."""

    return content_hash({"role": str(role), "content": str(content)}, schema_version="message-v1")


def _actor_id(actor: AuthUser | int | str | None) -> str:
    if actor is None:
        return ""
    return str(getattr(actor, "id", actor))


def _actor_user_id(actor: AuthUser | int | str | None) -> str | None:
    if actor is None:
        return None
    value = getattr(actor, "id", actor)
    if value in (None, ""):
        return None
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MemoryService:
    """Catalog-first memory read and governance service.

    ``db_path`` points at the existing Conversation/Auth SQLite control plane.
    The Store is lazy so a missing embedding provider cannot make chat/API
    startup fail.  Tests and migrations may inject a ``MemoryStoreRuntime``.
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        jobs: MemoryJobRepository | None = None,
        catalog: MemoryCatalogRepository | None = None,
        store_runtime: MemoryStoreRuntime | None = None,
        auth: AuthService | None = None,
        settings_module=None,
    ):
        self.db_path = db_path or settings.AUTH_DB_PATH
        self.jobs = jobs or MemoryJobRepository(self.db_path)
        self.catalog = catalog or MemoryCatalogRepository(self.db_path)
        self.auth = auth or AuthService(self.db_path)
        self._store_runtime = store_runtime
        self._settings = settings_module or settings

    @property
    def store_runtime(self) -> MemoryStoreRuntime | None:
        if self._store_runtime is None:
            try:
                self._store_runtime = create_memory_store(self._settings)
            except Exception as exc:
                # Store availability is deliberately not a chat/API fatal
                # condition.  Search returns an empty result and the worker
                # retains the outbox for retry.
                counter("hdb.memory.store_unavailable", attributes={"operation": "open"})
                self._store_runtime = None
                self._store_error = str(exc)[:500]
        return self._store_runtime

    def close(self) -> None:
        if self._store_runtime is not None:
            self._store_runtime.close()
            self._store_runtime = None
        close_catalog = getattr(self.catalog, "close", None)
        if callable(close_catalog):
            close_catalog()

    # ------------------------------------------------------------------
    # Scope and authorization

    def _resolve_user_id(self, request_context=None, actor: AuthUser | int | str | None = None) -> str:
        user_id = _actor_user_id(actor)
        if user_id:
            return user_id
        metadata = getattr(request_context, "metadata", {}) or {}
        trusted_id = metadata.get("actor_user_id")
        if trusted_id not in (None, ""):
            return str(trusted_id)
        username = getattr(request_context, "user_id", None)
        user = self.auth.get_user_by_username(str(username)) if username else None
        if user is not None and user.is_active:
            return str(user.id)
        raise MemoryAuthorizationError("authenticated user identity is required")

    def _project_scope(self, request_context, *, kb_name: str | None = None, require_read: bool = True, actor: AuthUser | int | str | None = None) -> MemoryScope:
        if request_context is not None and request_context.is_system_admin():
            raise MemoryAuthorizationError("system administrators cannot access project memory content")
        if request_context is None:
            if not isinstance(actor, AuthUser):
                raise MemoryAuthorizationError("request context is required for project memory")
            if actor.role == ROLE_SYSTEM_ADMIN or actor.department_id in (None, "") or not kb_name:
                raise MemoryAuthorizationError("a selected authorized KB is required for project memory")
            kb_id = self.auth.get_knowledge_base_id(kb_name, department_id=actor.department_id)
            if kb_id is None:
                raise MemoryAuthorizationError("knowledge base is not available in the authenticated department")
            if require_read:
                permission = self.auth.get_kb_permissions_for_user(actor).get(f"{actor.department_id}:{kb_name}")
                if permission not in {"read", "write", "admin"}:
                    raise MemoryAuthorizationError("knowledge-base read permission is required")
            return MemoryScope("project", department_id=str(actor.department_id), kb_id=str(kb_id))
        metadata = getattr(request_context, "metadata", {}) or {}
        department_id = metadata.get("resource_department_id")
        if department_id in (None, ""):
            department_id = metadata.get("department_id")
        kb_id = metadata.get("kb_id")
        if department_id in (None, "") or kb_id in (None, ""):
            raise MemoryAuthorizationError("project memory requires a complete authenticated KB scope")
        selected_kb = kb_name or metadata.get("kb_name")
        if require_read and selected_kb and not request_context.has_kb_permission(str(selected_kb), "read"):
            raise MemoryAuthorizationError("knowledge-base read permission is required")
        return MemoryScope("project", department_id=str(department_id), kb_id=str(kb_id))

    def _user_scope(self, request_context=None, actor: AuthUser | int | str | None = None) -> MemoryScope:
        return MemoryScope("user", user_id=self._resolve_user_id(request_context, actor))

    def _scope_for_record(self, record: MemoryRecord) -> MemoryScope:
        if record.scope == "user":
            return MemoryScope("user", user_id=record.user_id)
        return MemoryScope(
            "project",
            department_id=record.department_id,
            kb_id=record.kb_id,
        )

    def _can_read_record(
        self,
        record: MemoryRecord,
        *,
        request_context=None,
        actor: AuthUser | int | str | None = None,
        kb_name: str | None = None,
    ) -> bool:
        if record.scope == "user":
            try:
                return record.user_id == self._resolve_user_id(request_context, actor)
            except MemoryAuthorizationError:
                return False
        if request_context is None and isinstance(actor, AuthUser):
            # A governance/list endpoint may have an authenticated actor but
            # no selected KB context. Resolve the persisted KB binding by id
            # while retaining the department/read-grant boundary.
            if actor.role == ROLE_SYSTEM_ADMIN or str(actor.department_id) != str(record.department_id):
                return False
            with closing(self.auth._connect()) as conn:
                if actor.role == ROLE_DEPT_ADMIN:
                    # Department admins have an implicit admin grant over
                    # every KB in their department; that grant is not stored
                    # as a row in kb_permissions.
                    row = conn.execute(
                        "SELECT 1 FROM knowledge_bases WHERE id = ? AND department_id = ?",
                        (_safe_int(record.kb_id), _safe_int(actor.department_id)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """SELECT 1 FROM kb_permissions p
                           JOIN knowledge_bases kb ON kb.id = p.kb_id
                           WHERE p.user_id = ? AND p.kb_id = ?
                             AND p.permission IN ('read', 'write', 'admin')""",
                        (actor.id, _safe_int(record.kb_id)),
                    ).fetchone()
            return row is not None
        try:
            scope = self._project_scope(request_context, kb_name=kb_name, require_read=True, actor=actor)
        except MemoryAuthorizationError:
            return False
        return scope.department_id == record.department_id and scope.kb_id == record.kb_id

    def _can_manage_record(
        self,
        record: MemoryRecord,
        *,
        actor: AuthUser | int | str | None,
        request_context=None,
        kb_name: str | None = None,
    ) -> bool:
        if isinstance(actor, AuthUser) and actor.role == ROLE_SYSTEM_ADMIN:
            # System administrators own platform governance, not the private
            # contents of a department/KB.  They may manage a User Memory
            # only when it belongs to their own account; project memory stays
            # behind the department/KB administrator boundary.
            return record.scope == "user" and record.user_id == str(actor.id)
        if record.scope == "user":
            return record.user_id == self._resolve_user_id(request_context, actor)
        if not isinstance(actor, AuthUser):
            return False
        if actor.role == ROLE_DEPT_ADMIN and str(actor.department_id) == str(record.department_id):
            return True
        if request_context is None:
            if str(actor.department_id) != str(record.department_id):
                return False
            with closing(self.auth._connect()) as conn:
                kb = conn.execute(
                    "SELECT name FROM knowledge_bases WHERE id = ? AND department_id = ?",
                    (_safe_int(record.kb_id), _safe_int(actor.department_id)),
                ).fetchone()
            if kb is None:
                return False
            if actor.role == ROLE_DEPT_ADMIN:
                return True
            permission = self.auth.get_kb_permissions_for_user(actor).get(
                f"{actor.department_id}:{kb['name']}"
            )
            return permission == "admin"
        try:
            scope = self._project_scope(request_context, kb_name=kb_name, require_read=False, actor=actor)
        except MemoryAuthorizationError:
            return False
        return scope.department_id == record.department_id and scope.kb_id == record.kb_id and bool(
            request_context.has_kb_permission(str(kb_name or ""), "admin") if kb_name else False
        )

    def _require_read(self, record: MemoryRecord, **kwargs) -> None:
        if not self._can_read_record(record, **kwargs):
            raise MemoryAuthorizationError("memory is outside the authenticated scope")

    def _require_manage(self, record: MemoryRecord, **kwargs) -> None:
        if not self._can_manage_record(record, **kwargs):
            raise MemoryAuthorizationError("memory governance permission is required")

    # ------------------------------------------------------------------
    # Read path

    def _user_record_is_active(self, record: MemoryRecord) -> bool:
        if record.scope != "user" or record.status not in ACTIVE_MEMORY_STATUSES:
            return False
        user_settings = self.jobs.get_user_settings(record.user_id or "")
        if not user_settings.opt_in:
            return False
        sources = self.catalog.get_sources(record.memory_id, valid_only=True)
        if not sources:
            return False
        with closing(self.jobs._connect()) as conn:
            for source in sources:
                consent_id = source["consent_event_id"]
                if not consent_id:
                    return False
                consent = self.jobs.get_consent(consent_id, conn=conn)
                if consent is None or consent.revoked_at:
                    return False
                if consent.user_id != record.user_id:
                    return False
        return True

    def _projection_allowlist(
        self,
        scope: MemoryScope,
        *,
        kinds: Iterable[str],
    ) -> dict[tuple[tuple[str, ...], str], tuple[MemoryProjection, MemoryRecord]]:
        allowed: dict[tuple[tuple[str, ...], str], tuple[MemoryProjection, MemoryRecord]] = {}
        for kind in kinds:
            namespace = scope.namespace(kind)
            for projection in self.catalog.get_projections(
                scope=scope.kind,
                user_id=scope.user_id,
                department_id=scope.department_id,
                kb_id=scope.kb_id,
                kinds=(kind,),
                active_only=True,
            ):
                if projection.namespace != namespace or projection.projection_kind != kind:
                    continue
                record = self.catalog.get_record(projection.memory_id)
                if record is None or record.status not in ACTIVE_MEMORY_STATUSES:
                    continue
                if record.scope == "user" and not self._user_record_is_active(record):
                    continue
                allowed[(namespace, projection.store_key)] = (projection, record)
        return allowed

    @staticmethod
    def _item_content(item: SearchItem) -> dict[str, Any] | None:
        value = item.value if isinstance(item.value, dict) else {}
        content = value.get("content")
        return content if isinstance(content, dict) else None

    @staticmethod
    def _lexical_score(query: str, record: MemoryRecord) -> float:
        terms = {part.lower() for part in str(query or "").split() if part.strip()}
        if not terms:
            return 0.0
        haystack = json.dumps(record.content, ensure_ascii=False).lower()
        hits = sum(1 for term in terms if term in haystack)
        return hits / max(1, len(terms))

    def _search_projection_kind(
        self,
        *,
        scope: MemoryScope,
        kind: str,
        query: str,
        limit: int,
        allowlist: dict[tuple[tuple[str, ...], str], tuple[MemoryProjection, MemoryRecord]],
        runtime: MemoryStoreRuntime | None,
    ) -> list[dict[str, Any]]:
        namespace = scope.namespace(kind)
        physical: list[SearchItem] = []
        if runtime is not None and runtime.health().get("ok"):
            # Candidate retrieval is fail-closed without a healthy semantic
            # index. Verified memories may use the bounded Catalog lexical
            # fallback, because they are not silently broadened by it.
            if kind == "candidate" and not runtime.semantic_index_ready:
                counter("hdb.memory.search_candidate_skipped", attributes={"reason": "semantic_index_unavailable"})
            else:
                try:
                    max_scan = max(1, int(getattr(self._settings, "MEMORY_STORE_MAX_SCAN", 100)))
                    page_size = max(
                        1,
                        min(
                            max_scan,
                            limit * max(1, int(getattr(self._settings, "MEMORY_STORE_OVERSAMPLE_FACTOR", 4))),
                        ),
                    )
                    offset = 0
                    while len(physical) < max_scan:
                        page_limit = min(page_size, max_scan - len(physical))
                        page = runtime.search(
                            namespace,
                            query=query,
                            limit=page_limit,
                            offset=offset,
                        )
                        if not page:
                            break
                        physical.extend(page)
                        offset += len(page)
                        if len(page) < page_limit:
                            break
                except Exception:
                    counter("hdb.memory.search_store_error", attributes={"kind": kind})
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        min_score = None
        if bool(getattr(self._settings, "MEMORY_MIN_SCORE_ENABLED", False)):
            try:
                min_score = float(getattr(self._settings, "MEMORY_MIN_SCORE", ""))
            except (TypeError, ValueError):
                min_score = None
        for item in physical:
            key = (namespace, str(item.key))
            pair = allowlist.get(key)
            if pair is None or tuple(item.namespace) != namespace:
                existing_projection = self.catalog.get_projection_by_key(
                    "sqlite",
                    namespace,
                    str(item.key),
                )
                if existing_projection is not None and (
                    not existing_projection.active or existing_projection.retired_at is not None
                ):
                    counter("hdb.memory.search_filtered_retired", attributes={"kind": kind})
                else:
                    counter("hdb.memory.search_filtered_orphan", attributes={"kind": kind})
                continue
            projection, record = pair
            semantic = self._item_content(item)
            if semantic is None:
                continue
            schema_version = str((item.value or {}).get("schema_version") or record.schema_version or MEMORY_SCHEMA_VERSION)
            if memory_content_hash(semantic, schema_version=schema_version) != projection.current_content_hash:
                counter("hdb.memory.search_filtered_hash", attributes={"kind": kind})
                continue
            if record.content_hash != projection.current_content_hash or record.memory_id in seen:
                continue
            try:
                score = float(item.score) if item.score is not None else self._lexical_score(query, record)
            except (TypeError, ValueError):
                score = self._lexical_score(query, record)
            if min_score is not None and score < min_score:
                continue
            seen.add(record.memory_id)
            results.append(self._result(record, score=score))
        if not physical and kind == "verified":
            # This fallback only walks the already filtered Catalog allowlist;
            # it cannot see another user/KB and never activates candidates.
            for (item_namespace, _key), (_projection, record) in allowlist.items():
                if item_namespace != namespace or record.memory_id in seen:
                    continue
                score = self._lexical_score(query, record)
                if query.strip() and score <= 0:
                    continue
                if min_score is not None and score < min_score:
                    continue
                seen.add(record.memory_id)
                results.append(self._result(record, score=score))
        return results

    @staticmethod
    def _recency_value(value: Any) -> float:
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0

    @classmethod
    def _ranking_key(cls, item: dict[str, Any]) -> tuple[int, float, float, str]:
        """Rank status first, then calibrated score/confidence/importance/recency."""
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            confidence = max(0.0, min(1.0, float(content.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            explicit_importance = float(content.get("importance")) if content.get("importance") is not None else None
        except (TypeError, ValueError):
            explicit_importance = None
        # Current semantic schemas do not ask the model for governance
        # importance.  When a future schema supplies it, honor it; otherwise
        # provenance density is a deterministic, server-owned proxy.
        importance = explicit_importance if explicit_importance is not None else min(1.0, float(item.get("source_count") or 0) / 3.0)
        effective_score = score * (1.2 if item.get("status") == "verified" else 1.0)
        effective_score += confidence * 0.05 + max(0.0, min(1.0, importance)) * 0.02
        return (
            0 if item.get("status") == "verified" else 1,
            -effective_score,
            -cls._recency_value(item.get("updated_at")),
            str(item.get("memory_id") or item.get("id") or ""),
        )

    def _result(self, record: MemoryRecord, *, score: float | None = None) -> dict[str, Any]:
        result = {
            "id": record.memory_id,
            "memory_id": record.memory_id,
            "revision": record.current_revision,
            "status": record.status,
            "scope": record.scope,
            "kind": record.memory_type,
            "type": record.memory_type,
            # The service keeps the semantic object intact.  Agent-facing
            # callers can derive a short display string without losing tags
            # or temporal fields at the API/governance boundary.
            "content": dict(record.content),
            "title": record.title,
            "subject": record.subject,
            "has_provenance": True,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "replacement_id": record.replacement_id,
        }
        valid_sources = self.catalog.get_sources(record.memory_id, valid_only=True)
        result["score"] = score
        result["source_count"] = len(valid_sources)
        result["has_provenance"] = bool(valid_sources)
        return result

    def search(
        self,
        query: str,
        *,
        request_context=None,
        actor: AuthUser | int | str | None = None,
        scope: str = "all",
        status: str = "all",
        include_candidate: bool = True,
        include_verified: bool = True,
        top_k: int | None = None,
        kb_name: str | None = None,
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        if not bool(getattr(self._settings, "MEMORY_ENABLED", True)):
            counter("hdb.memory.search_disabled")
            return []
        query = str(query or "").strip()
        if not query:
            return []
        if scope not in {"all", "user", "project"}:
            raise ValueError("memory scope must be all, user, or project")
        if status not in {"all", "candidate", "verified"}:
            raise ValueError("memory status must be all, candidate, or verified")
        scopes: list[MemoryScope] = []
        if scope in {"all", "user"}:
            try:
                scopes.append(self._user_scope(request_context, actor))
            except MemoryAuthorizationError:
                if scope == "user":
                    counter("hdb.memory.search_acl_denied", attributes={"scope": "user"})
                    raise
        if scope in {"all", "project"}:
            try:
                scopes.append(self._project_scope(request_context, kb_name=kb_name, actor=actor))
            except MemoryAuthorizationError:
                if scope == "project":
                    counter("hdb.memory.search_acl_denied", attributes={"scope": "project"})
                    raise
        kinds: list[str] = []
        if include_candidate and status in {"all", "candidate"}:
            kinds.append("candidate")
        if include_verified and status in {"all", "verified"}:
            kinds.append("verified")
        requested_cap = max(1, min(int(top_k), 20)) if top_k is not None else None

        def scope_cap(current_scope: MemoryScope) -> int:
            configured = int(
                getattr(
                    self._settings,
                    "MEMORY_USER_TOP_K" if current_scope.kind == "user" else "MEMORY_PROJECT_TOP_K",
                    3 if current_scope.kind == "user" else 5,
                )
            )
            configured = max(1, configured)
            return min(configured, requested_cap) if requested_cap is not None else configured

        combined: list[dict[str, Any]] = []
        for current_scope in scopes:
            before_generation = None
            if current_scope.kind == "user":
                before_generation = self.jobs.get_user_settings(current_scope.user_id or "").revoke_generation
            allowlist = self._projection_allowlist(current_scope, kinds=kinds)
            runtime = self.store_runtime
            current_cap = scope_cap(current_scope)
            for kind in kinds:
                with observe.retriever(
                    "hdb.memory.search",
                    scope=current_scope.kind,
                    status=status,
                    projection_kind=kind,
                ) as observation:
                    found = self._search_projection_kind(
                        scope=current_scope,
                        kind=kind,
                        query=query,
                        limit=current_cap,
                        allowlist=allowlist,
                        runtime=runtime,
                    )
                    observation.set("hdb.memory.search.limit", current_cap)
                    observation.hit_count(len(found))
                    observation.outcome("success")
                    combined.extend(found)
            if current_scope.kind == "user":
                after_generation = self.jobs.get_user_settings(current_scope.user_id or "").revoke_generation
                if before_generation != after_generation:
                    # A revoke raced the Store read.  Returning an old result
                    # would violate the privacy contract, so fail closed.
                    combined = [item for item in combined if item.get("scope") != "user"]
        dedup: dict[str, dict[str, Any]] = {}
        for item in combined:
            memory_id = str(item.get("id") or "")
            previous = dedup.get(memory_id)
            if previous is None or (
                str(item.get("status")) == "verified" and str(previous.get("status")) != "verified"
            ) or float(item.get("score") or 0) > float(previous.get("score") or 0):
                dedup[memory_id] = item
        global_cap = sum(scope_cap(current_scope) for current_scope in scopes)
        result = sorted(dedup.values(), key=self._ranking_key)[:global_cap]
        if len(result) < global_cap:
            counter("hdb.memory.search_underfilled", attributes={"scope": scope})
        counter("hdb.memory.search_hits", value=len(result), attributes={"scope": scope})
        histogram("hdb.memory.search_latency_ms", (time.monotonic() - started) * 1000, unit="ms")
        return result

    def format_context(self, memories: Iterable[dict[str, Any]], *, max_tokens: int | None = None, item_max_tokens: int | None = None) -> str:
        """Render memory as bounded, explicitly untrusted context."""

        rendered = format_memory_context(
            memories,
            max_tokens=max_tokens if max_tokens is not None else int(getattr(self._settings, "MEMORY_CONTEXT_MAX_TOKENS", 1800)),
            item_max_tokens=item_max_tokens if item_max_tokens is not None else int(getattr(self._settings, "MEMORY_ITEM_MAX_TOKENS", 350)),
        )
        if not rendered:
            return ""
        counter("hdb.memory.context_items", value=rendered.count("[M"))
        histogram(
            "hdb.memory.context_tokens",
            len(rendered) / 4.0,
            unit="{tokens}",
        )
        return rendered

    # ------------------------------------------------------------------
    # Catalog listing and governance

    def list_memories(
        self,
        *,
        request_context=None,
        actor: AuthUser | int | str | None = None,
        scope: str = "all",
        status: str | None = "all",
        kb_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
        query: str = "",
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        status = status or "all"
        if scope not in {"all", "user", "project"}:
            raise ValueError("memory scope must be all, user, or project")
        if status not in {"all", "candidate", "verified", "verification_pending", "rejected", "deleted", "superseded", "needs_rebuild", "supersede_pending", "provenance_missing"}:
            raise ValueError("unsupported memory status")
        from src.memory.catalog import MEMORY_STATUSES

        statuses = {status} if status != "all" else set(MEMORY_STATUSES)
        try:
            cursor_offset = max(0, int(cursor)) if cursor else 0
        except (TypeError, ValueError):
            raise ValueError("invalid memory cursor") from None
        requested_limit = max(1, min(int(limit), 200))
        fetch_limit = max(requested_limit, min(500, requested_limit + cursor_offset))
        scopes: list[MemoryScope] = []
        if scope in {"all", "user"}:
            try:
                scopes.append(self._user_scope(request_context, actor))
            except MemoryAuthorizationError:
                if scope == "user":
                    raise
        if scope in {"all", "project"}:
            try:
                scopes.append(self._project_scope(request_context, kb_name=kb_name, actor=actor))
            except MemoryAuthorizationError:
                if scope == "project":
                    raise
        rows: list[MemoryRecord] = []
        for current_scope in scopes:
            records = self.catalog.list_records(
                scope=current_scope.kind,
                user_id=current_scope.user_id,
                department_id=current_scope.department_id,
                kb_id=current_scope.kb_id,
                statuses=statuses,
                limit=fetch_limit,
                offset=offset,
            )
            rows.extend(
                record
                for record in records
                if (record.scope != "user" or self._user_record_is_active(record))
                and (
                    not str(query or "").strip()
                    or str(query).lower() in json.dumps(record.content, ensure_ascii=False).lower()
                )
            )
        result = [self._result(record) for record in rows]
        return result[cursor_offset: cursor_offset + requested_limit]

    def get_memory(
        self,
        memory_id: str,
        *,
        request_context=None,
        actor: AuthUser | int | str | None = None,
        kb_name: str | None = None,
        include_audit: bool = True,
    ) -> dict[str, Any]:
        record = self.catalog.get_record(str(memory_id))
        if record is None:
            raise MemoryNotFound("memory not found")
        self._require_read(record, request_context=request_context, actor=actor, kb_name=kb_name)
        result = self._result(record)
        if include_audit:
            result["audit"] = {"events": self.catalog.audit_events(record.memory_id)}
            result["sources"] = [
                {
                    "source_id": row["source_id"],
                    "source_kind": row["contribution_kind"],
                    "session_id": row["session_id"],
                    "turn_id": row["turn_id"],
                    "message_id": row["message_id"],
                    "content_hash": row["source_hash"],
                    "valid": bool(row["source_valid"]),
                }
                for row in self.catalog.get_sources(record.memory_id)
            ]
            result["projection_status"] = "active" if any(
                projection.memory_id == record.memory_id
                for projection in self.catalog.get_projections(
                    scope=record.scope,
                    user_id=record.user_id,
                    department_id=record.department_id,
                    kb_id=record.kb_id,
                    active_only=True,
                )
            ) else "pending_or_retired"
        return result

    # ------------------------------------------------------------------
    # Reflection contract and operator-triggered extraction

    def reflect_job(self, job: Any) -> dict[str, Any]:
        """Reload the exact, authorized source window for a reflection job.

        This is deliberately a Catalog/Conversation read operation.  The
        Worker uses the same invariants before invoking LangMem, while this
        public service contract gives tests and future adapters one stable
        place to validate job boundaries.
        """

        if isinstance(job, (str, int)):
            job = self.jobs.get(str(job))
        if job is None or not hasattr(job, "job_kind"):
            raise MemoryNotFound("memory reflection job not found")

        if job.job_kind == "project_reflection":
            with closing(self.jobs._connect()) as conn:
                session = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (int(job.session_id),)).fetchone()
                if session is None:
                    raise MemoryNotFound("conversation session no longer exists")
                if session["department_id"] in (None, "") or session["kb_id"] in (None, ""):
                    raise MemoryAuthorizationError("project memory scope is incomplete")
                expected_scope = scope_fingerprint(
                    scope="project",
                    department_id=session["department_id"],
                    kb_id=session["kb_id"],
                )
                if expected_scope != job.scope_fingerprint:
                    raise MemoryAuthorizationError("project memory scope changed")
                rows = conn.execute(
                    """SELECT DISTINCT m.id, m.session_id, m.role, m.content, m.created_at,
                        t.id AS turn_id
                       FROM chat_messages m
                       JOIN chat_turns t ON t.session_id = m.session_id
                         AND (t.user_message_id = m.id OR t.assistant_message_id = m.id)
                         AND t.status = 'completed'
                       WHERE m.session_id = ?
                         AND m.id <= ?
                         AND (? IS NULL OR m.id >= ?)
                       ORDER BY m.id""",
                    (
                        int(job.session_id),
                        int(job.target_message_id),
                        job.source_start_message_id,
                        job.source_start_message_id,
                    ),
                ).fetchall()
            messages = self._rows_to_messages(rows)
            if not messages or int(messages[-1]["id"]) != int(job.target_message_id):
                raise MemoryAuthorizationError("project reflection target is not a completed message")
            return {
                "job": job,
                "session": dict(session),
                "messages": messages,
                "scope": namespace_for_project(session["department_id"], session["kb_id"], "candidate"),
                "consent": None,
            }

        if job.job_kind != "user_reflection":
            raise ValueError("unsupported memory reflection job kind")
        consent = self.jobs.validate_consent_for_job(job)
        if job.target_message_id != consent.authorized_end_message_id:
            raise MemoryAuthorizationError("user reflection target is outside the consent boundary")
        if job.scope_fingerprint != scope_fingerprint(scope="user", user_id=consent.user_id):
            raise MemoryAuthorizationError("user reflection scope changed")
        with closing(self.jobs._connect()) as conn:
            session = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?",
                (int(job.session_id), str(consent.user_id)),
            ).fetchone()
            if session is None:
                raise MemoryAuthorizationError("consent session is unavailable")
            messages: list[dict[str, Any]] = []
            for item in consent.manifest:
                row = conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content, m.created_at,
                        t.id AS turn_id
                       FROM chat_messages m
                       JOIN chat_turns t ON t.session_id = m.session_id
                         AND (t.user_message_id = m.id OR t.assistant_message_id = m.id)
                         AND t.status = 'completed'
                       WHERE m.session_id = ? AND m.id = ?""",
                    (int(job.session_id), int(item["message_id"])),
                ).fetchone()
                if row is None:
                    raise MemoryAuthorizationError("consent source message is unavailable")
                current_hash = message_content_hash(row["role"], row["content"])
                if (
                    str(row["turn_id"]) != str(item["turn_id"])
                    or str(row["role"]) != str(item["role"])
                    or current_hash != str(item["content_hash"])
                ):
                    raise MemoryAuthorizationError("consent source manifest no longer matches the conversation")
                value = dict(row)
                value["content_hash"] = current_hash
                messages.append(value)
        if not messages or int(messages[-1]["id"]) != int(consent.authorized_end_message_id):
            raise MemoryAuthorizationError("consent end boundary is invalid")
        return {
            "job": job,
            "session": dict(session),
            "messages": messages,
            "scope": namespace_for_user(consent.user_id, "candidate"),
            "consent": consent,
        }

    def extract_memory(self, *, actor: AuthUser, session_id: int, reason: str = "", request_id: str = "") -> dict[str, Any]:
        """Explicitly enqueue a project extraction for the current session."""

        if not bool(getattr(self._settings, "MEMORY_ENABLED", True)) or not bool(
            getattr(self._settings, "MEMORY_EXTRACTION_ENABLED", True)
        ):
            raise MemoryServiceError("long-term memory extraction is disabled")
        if not isinstance(actor, AuthUser) or not actor.is_active:
            raise MemoryAuthorizationError("active authenticated user is required")
        with closing(self.jobs._connect()) as conn:
            session = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?",
                (int(session_id), int(actor.id)),
            ).fetchone()
        if session is None:
            raise MemoryAuthorizationError("conversation does not belong to current user")
        if session["department_id"] in (None, "") or session["kb_id"] in (None, "") or not session["kb_name"]:
            raise MemoryAuthorizationError("project memory requires a complete session scope")
        selected_scope = self._project_scope(None, kb_name=session["kb_name"], actor=actor)
        if selected_scope.department_id != str(session["department_id"]) or selected_scope.kb_id != str(session["kb_id"]):
            raise MemoryAuthorizationError("conversation KB is outside the authenticated project scope")

        with closing(self.jobs._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current_session = conn.execute(
                    "SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?",
                    (int(session_id), int(actor.id)),
                ).fetchone()
                target = conn.execute(
                    """SELECT t.id AS turn_id, t.assistant_message_id
                       FROM chat_turns t
                       WHERE t.session_id = ? AND t.status = 'completed'
                       ORDER BY t.assistant_message_id DESC, t.id DESC
                       LIMIT 1""",
                    (int(session_id),),
                ).fetchone()
                if current_session is None or target is None or target["assistant_message_id"] is None:
                    raise ValueError("a completed turn is required before extracting memory")
                job_id = self.jobs.enqueue_project_reflection(
                    session_id=int(session_id),
                    scope_fingerprint=scope_fingerprint(
                        scope="project",
                        department_id=current_session["department_id"],
                        kb_id=current_session["kb_id"],
                    ),
                    target_turn_id=str(target["turn_id"]),
                    target_message_id=int(target["assistant_message_id"]),
                    available_at=utc_now(),
                    force=True,
                    conn=conn,
                )
                job = self.jobs.get(job_id, conn=conn)
                self.catalog.audit(
                    "project_reflection_requested",
                    actor_id=str(actor.id),
                    request_id=request_id,
                    metadata={"session_id": int(session_id), "reason": reason, "job_id": job_id},
                    conn=conn,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "operation_id": job_id,
            "memory_id": "",
            "status": "accepted",
            "revision": None,
            "message": f"memory reflection queued ({getattr(job, 'status', 'pending')})",
        }

    def update_draft(
        self,
        *,
        actor: AuthUser,
        memory_id: str,
        content: dict[str, Any],
        expected_revision: int,
        reason: str,
        request_id: str = "",
        request_context=None,
        kb_name: str | None = None,
    ) -> dict[str, Any]:
        """CAS-update a Candidate semantic object and re-project it safely."""

        record = self.catalog.get_record(memory_id)
        if record is None:
            raise MemoryNotFound("memory not found")
        self._require_manage(record, actor=actor, request_context=request_context, kb_name=kb_name)
        if record.status != "candidate":
            raise MemoryServiceError("only candidate memory can be edited as a draft")
        schema = UserMemory if record.scope == "user" else ProjectMemory
        semantic = schema.model_validate(content).model_dump(mode="json")
        conn = self._begin_control_transaction()
        try:
            row = conn.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryNotFound("memory not found")
            self._require_revision(row, expected_revision)
            if row["status"] != "candidate":
                raise MemoryServiceError("only candidate memory can be edited as a draft")
            projection = conn.execute(
                "SELECT * FROM memory_projections WHERE memory_id = ? AND projection_kind = 'candidate' AND retired_at IS NULL",
                (memory_id,),
            ).fetchone()
            if projection is None or not projection["manager_writable"]:
                raise MemoryServiceError("candidate projection is fenced")
            next_fence = int(projection["fence_version"]) + 1
            fenced = conn.execute(
                "UPDATE memory_projections SET fence_version = ? WHERE projection_id = ? AND retired_at IS NULL AND manager_writable = 1 AND fence_version = ?",
                (next_fence, projection["projection_id"], int(projection["fence_version"])),
            )
            if fenced.rowcount != 1:
                raise MemoryServiceError("candidate projection fence changed")
            updated, _updated_projection, operation_id = self.catalog.prepare_candidate(
                content=semantic,
                scope=row["scope"],
                user_id=row["user_id"],
                department_id=row["department_id"],
                kb_id=row["kb_id"],
                memory_id=memory_id,
                actor_id=_actor_id(actor),
                reason=reason,
                expected_fence=next_fence,
                extractor_model=row["extractor_model"] or "",
                extractor_version=row["extractor_version"] or "",
                schema_version=row["schema_version"] or MEMORY_SCHEMA_VERSION,
                revision_operation="draft_update",
                conn=conn,
            )
            self.catalog.audit(
                "draft_updated",
                memory_id=memory_id,
                operation_id=operation_id,
                actor_id=_actor_id(actor),
                request_id=request_id,
                metadata={"reason": reason, "revision": updated.current_revision},
                conn=conn,
            )
            conn.commit()
            return {
                "operation_id": operation_id,
                "memory_id": memory_id,
                "status": "candidate",
                "revision": updated.current_revision,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _begin_control_transaction(self) -> sqlite3.Connection:
        conn = self.jobs._connect()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def _require_revision(self, row: sqlite3.Row, expected_revision: int) -> None:
        if int(row["current_revision"]) != int(expected_revision):
            raise MemoryServiceError("memory revision changed; refresh before retrying")

    def _insert_revision(self, conn: sqlite3.Connection, *, record: sqlite3.Row, revision: int, operation: str, actor: str, reason: str, projection_id: str | None = None, before: Any = None, after: Any = None) -> None:
        conn.execute(
            """INSERT INTO memory_revisions
            (revision_id, memory_id, revision_no, projection_id, before_content_json, after_content_json, operation, actor_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), record["memory_id"], int(revision), projection_id,
                json_dumps(before) if before is not None else None,
                json_dumps(after) if after is not None else None,
                operation, actor, reason, utc_now(),
            ),
        )

    def _queue_verified_projection(self, conn: sqlite3.Connection, record: sqlite3.Row, *, revision: int) -> str:
        namespace = namespace_for_project(record["department_id"], record["kb_id"], "verified") if record["scope"] == "project" else namespace_for_user(record["user_id"], "verified")
        projection_id = str(uuid.uuid4())
        store_key = f"{record['memory_id']}:verified:{revision}"
        now = utc_now()
        conn.execute(
            """INSERT INTO memory_projections
            (projection_id, memory_id, projection_kind, store_backend, namespace_json, store_key,
             current_revision, current_content_hash, active, manager_writable, fence_version, created_at)
            VALUES (?, ?, 'verified', 'sqlite', ?, ?, ?, ?, 0, 0, 1, ?)""",
            (projection_id, record["memory_id"], json_dumps(list(namespace)), store_key, int(revision), record["content_hash"], now),
        )
        operation_id = str(uuid.uuid4())
        payload = {
            "kind": record["memory_type"],
            "content": json_loads(record["content_json"], {}),
            "memory_id": record["memory_id"],
            "projection_id": projection_id,
            "content_hash": record["content_hash"],
            "schema_version": record["schema_version"] or MEMORY_SCHEMA_VERSION,
        }
        conn.execute(
            """INSERT INTO memory_projection_outbox
            (operation_id, memory_id, projection_id, operation, expected_revision, expected_fence,
             idempotency_key, payload_json, created_at)
            VALUES (?, ?, ?, 'put', ?, 1, ?, ?, ?)""",
            (operation_id, record["memory_id"], projection_id, int(revision), f"put:{projection_id}:{revision}:{record['content_hash']}", json_dumps(payload), now),
        )
        return operation_id

    def verify(self, memory_id: str, *, actor, expected_revision: int, evidence_refs: Iterable[Any], reason: str, request_id: str = "", request_context=None, kb_name: str | None = None) -> dict[str, Any]:
        record = self.catalog.get_record(memory_id)
        if record is None:
            raise MemoryNotFound("memory not found")
        self._require_manage(record, actor=actor, request_context=request_context, kb_name=kb_name)
        evidence = list(evidence_refs or [])
        if not evidence:
            raise ValueError("verify requires evidence_refs")
        observation = observe.chain("hdb.memory.verify", operation="verify")
        observation.start()
        outcome = "failed"
        conn = self._begin_control_transaction()
        try:
            row = conn.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryNotFound("memory not found")
            self._require_manage(
                row_to_record(row),
                actor=actor,
                request_context=request_context,
                kb_name=kb_name,
            )
            self._require_revision(row, expected_revision)
            if row["status"] != "candidate":
                raise MemoryServiceError("only candidate memory can be verified")
            revision = int(row["current_revision"]) + 1
            candidate = conn.execute("SELECT * FROM memory_projections WHERE memory_id = ? AND projection_kind = 'candidate' AND retired_at IS NULL", (memory_id,)).fetchone()
            if candidate is not None:
                fenced = self.catalog.fence_projection(
                    candidate["projection_id"],
                    expected_fence=int(candidate["fence_version"]),
                    conn=conn,
                )
                if fenced is None:
                    raise MemoryServiceError("candidate projection fence changed")
            conn.execute("UPDATE memory_records SET status = 'verification_pending', current_revision = ?, updated_at = ? WHERE memory_id = ? AND current_revision = ?", (revision, utc_now(), memory_id, int(expected_revision)))
            self._insert_revision(conn, record=row, revision=revision, operation="verify", actor=_actor_id(actor), reason=reason, before=json_loads(row["content_json"], {}), after=json_loads(row["content_json"], {}))
            put_op = self._queue_verified_projection(conn, conn.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone(), revision=revision)
            self.catalog.audit("verify_requested", memory_id=memory_id, actor_id=_actor_id(actor), evidence_refs=evidence, request_id=request_id, metadata={"reason": reason, "revision": revision}, conn=conn)
            conn.commit()
            updated = self.catalog.get_record(memory_id, conn=conn)
            counter("hdb.memory.verified", attributes={"scope": row["scope"]})
            outcome = "accepted"
            return {"memory": self._result(updated) if updated else {}, "operation_id": put_op, "delete_operation_ids": [], "status": "verification_pending"}
        except Exception as exc:
            observation.error(exc)
            conn.rollback()
            raise
        finally:
            conn.close()
            observation.outcome(outcome)
            observation.end()

    def reject(self, memory_id: str, *, actor, expected_revision: int, reason: str, request_id: str = "", request_context=None, kb_name: str | None = None) -> dict[str, Any]:
        return self._retire_governed(memory_id, actor=actor, expected_revision=expected_revision, next_status="rejected", operation="reject", reason=reason, request_id=request_id, request_context=request_context, kb_name=kb_name)

    def delete(self, memory_id: str, *, actor, expected_revision: int, reason: str, request_id: str = "", request_context=None, kb_name: str | None = None) -> dict[str, Any]:
        return self._retire_governed(memory_id, actor=actor, expected_revision=expected_revision, next_status="deleted", operation="delete", reason=reason, request_id=request_id, request_context=request_context, kb_name=kb_name, scrub=True)

    def _retire_governed(self, memory_id: str, *, actor, expected_revision: int, next_status: str, operation: str, reason: str, request_id: str, request_context, kb_name: str | None, scrub: bool = False) -> dict[str, Any]:
        record = self.catalog.get_record(memory_id)
        if record is None:
            raise MemoryNotFound("memory not found")
        self._require_manage(record, actor=actor, request_context=request_context, kb_name=kb_name)
        observation = observe.chain(f"hdb.memory.{operation}", operation=operation)
        observation.start()
        outcome = "failed"
        conn = self._begin_control_transaction()
        try:
            row = conn.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryNotFound("memory not found")
            self._require_manage(
                row_to_record(row),
                actor=actor,
                request_context=request_context,
                kb_name=kb_name,
            )
            self._require_revision(row, expected_revision)
            revision = int(row["current_revision"]) + 1
            delete_ops: list[str] = []
            projections = conn.execute("SELECT * FROM memory_projections WHERE memory_id = ? AND retired_at IS NULL", (memory_id,)).fetchall()
            for projection in projections:
                op = self.catalog.retire_projection(projection["projection_id"], reason=reason, actor_id=_actor_id(actor), conn=conn)
                if op:
                    delete_ops.append(op)
            if scrub:
                replacement_hash = memory_content_hash({}, schema_version=row["schema_version"] or MEMORY_SCHEMA_VERSION)
                conn.execute(
                    "UPDATE memory_records SET status = ?, current_revision = ?, content_hash = ?, content_json = '{}', title = '已删除记忆', subject = NULL, memory_type = 'context', deleted_at = ?, updated_at = ? WHERE memory_id = ? AND current_revision = ?",
                    (next_status, revision, replacement_hash, utc_now(), utc_now(), memory_id, int(expected_revision)),
                )
                if row["scope"] == "user":
                    conn.execute(
                        "UPDATE memory_sources SET source_valid = 0, invalidated_at = COALESCE(invalidated_at, ?) WHERE memory_id = ?",
                        (utc_now(), memory_id),
                    )
                conn.execute("UPDATE memory_revisions SET before_content_json = NULL, after_content_json = NULL WHERE memory_id = ?", (memory_id,))
                conn.execute("UPDATE memory_reflection_runs SET output_payload_json = NULL, encrypted_source_snapshot_ref = NULL WHERE job_id IN (SELECT job_id FROM memory_run_items WHERE memory_id = ?)", (memory_id,))
            else:
                conn.execute("UPDATE memory_records SET status = ?, current_revision = ?, deleted_at = CASE WHEN ? = 'deleted' THEN ? ELSE deleted_at END, updated_at = ? WHERE memory_id = ? AND current_revision = ?", (next_status, revision, next_status, utc_now(), utc_now(), memory_id, int(expected_revision)))
            self._insert_revision(conn, record=row, revision=revision, operation=operation, actor=_actor_id(actor), reason=reason, before=None if scrub else json_loads(row["content_json"], {}), after=None if scrub else json_loads(row["content_json"], {}))
            self.catalog.audit(f"{operation}_requested", memory_id=memory_id, actor_id=_actor_id(actor), request_id=request_id, metadata={"reason": reason, "revision": revision}, conn=conn)
            conn.commit()
            updated = self.catalog.get_record(memory_id, conn=conn)
            counter(f"hdb.memory.{operation}", attributes={"scope": row["scope"]})
            outcome = "accepted"
            return {"memory": self._result(updated) if updated else {}, "delete_operation_ids": delete_ops, "status": next_status}
        except Exception as exc:
            observation.error(exc)
            conn.rollback()
            raise
        finally:
            conn.close()
            observation.outcome(outcome)
            observation.end()

    def supersede(self, memory_id: str, replacement_id: str, *, actor, expected_revision: int, reason: str, request_id: str = "", request_context=None, kb_name: str | None = None) -> dict[str, Any]:
        if memory_id == replacement_id:
            raise ValueError("a memory cannot supersede itself")
        old = self.catalog.get_record(memory_id)
        replacement = self.catalog.get_record(replacement_id)
        if old is None or replacement is None:
            raise MemoryNotFound("memory not found")
        self._require_manage(old, actor=actor, request_context=request_context, kb_name=kb_name)
        if old.status not in ACTIVE_MEMORY_STATUSES:
            raise MemoryServiceError("only active memory can be superseded")
        if replacement.status not in ACTIVE_MEMORY_STATUSES:
            raise MemoryServiceError("replacement memory must be active")
        if old.scope != replacement.scope or old.user_id != replacement.user_id or old.department_id != replacement.department_id or old.kb_id != replacement.kb_id:
            raise MemoryAuthorizationError("replacement memory must have the same scope")
        observation = observe.chain("hdb.memory.supersede", operation="supersede")
        observation.start()
        outcome = "failed"
        conn = self._begin_control_transaction()
        try:
            row = conn.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryNotFound("memory not found")
            self._require_manage(
                row_to_record(row),
                actor=actor,
                request_context=request_context,
                kb_name=kb_name,
            )
            self._require_revision(row, expected_revision)
            revision = int(row["current_revision"]) + 1
            delete_ops: list[str] = []
            for projection in conn.execute("SELECT * FROM memory_projections WHERE memory_id = ? AND retired_at IS NULL", (memory_id,)).fetchall():
                op = self.catalog.retire_projection(projection["projection_id"], reason="superseded", actor_id=_actor_id(actor), conn=conn)
                if op:
                    delete_ops.append(op)
            conn.execute("UPDATE memory_records SET status = 'supersede_pending', replacement_id = ?, current_revision = ?, updated_at = ? WHERE memory_id = ? AND current_revision = ?", (replacement_id, revision, utc_now(), memory_id, int(expected_revision)))
            self._insert_revision(conn, record=row, revision=revision, operation="supersede", actor=_actor_id(actor), reason=reason, before=json_loads(row["content_json"], {}), after=json_loads(row["content_json"], {}))
            self.catalog.audit("supersede_requested", memory_id=memory_id, actor_id=_actor_id(actor), request_id=request_id, evidence_refs=[replacement_id], metadata={"reason": reason, "replacement_id": replacement_id, "revision": revision}, conn=conn)
            conn.commit()
            updated = self.catalog.get_record(memory_id, conn=conn)
            counter("hdb.memory.superseded", attributes={"scope": row["scope"]})
            outcome = "accepted"
            return {"memory": self._result(updated) if updated else {}, "replacement_id": replacement_id, "delete_operation_ids": delete_ops, "status": "supersede_pending"}
        except Exception as exc:
            observation.error(exc)
            conn.rollback()
            raise
        finally:
            conn.close()
            observation.outcome(outcome)
            observation.end()

    # ------------------------------------------------------------------
    # User opt-in and immutable consent

    def get_user_settings(self, actor: AuthUser | int | str) -> dict[str, Any]:
        user_id = _actor_user_id(actor)
        if not user_id:
            raise MemoryAuthorizationError("user identity is required")
        value = self.jobs.get_user_settings(user_id)
        return {"user_id": value.user_id, "opt_in": value.opt_in, "policy_version": value.policy_version, "revoke_generation": value.revoke_generation, "updated_at": value.updated_at}

    def set_user_opt_in(self, actor: AuthUser | int | str, enabled: bool) -> dict[str, Any]:
        user_id = _actor_user_id(actor)
        if not user_id:
            raise MemoryAuthorizationError("user identity is required")
        value = self.jobs.set_user_opt_in(user_id, bool(enabled))
        counter("hdb.memory.user_opt_in", attributes={"enabled": str(bool(enabled)).lower()})
        return {"user_id": value.user_id, "opt_in": value.opt_in, "policy_version": value.policy_version, "revoke_generation": value.revoke_generation, "updated_at": value.updated_at}

    def create_user_consent(
        self,
        actor: AuthUser,
        session_id: int,
        message_ids: Iterable[int],
        *,
        reason: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        if not isinstance(actor, AuthUser) or not actor.is_active:
            raise MemoryAuthorizationError("active authenticated user is required")
        requested_ids = sorted({int(value) for value in message_ids})
        if not requested_ids or len(requested_ids) > 100:
            raise ValueError("consent requires 1-100 message ids")
        conn = self.jobs._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ensure_memory_schema(conn)
            session = conn.execute("SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?", (int(session_id), int(actor.id))).fetchone()
            if session is None:
                raise MemoryAuthorizationError("conversation does not belong to current user")
            placeholders = ",".join("?" for _ in requested_ids)
            rows = conn.execute(
                f"""SELECT m.id, m.session_id, m.role, m.content,
                    COALESCE(t.id, '') AS turn_id
                    FROM chat_messages m
                    LEFT JOIN chat_turns t ON t.session_id = m.session_id
                      AND (t.user_message_id = m.id OR t.assistant_message_id = m.id)
                      AND t.status = 'completed'
                    WHERE m.session_id = ? AND m.id IN ({placeholders})
                    ORDER BY m.id""",
                (int(session_id), *requested_ids),
            ).fetchall()
            if len(rows) != len(requested_ids) or any(not row["turn_id"] for row in rows):
                raise ValueError("consent messages must be existing messages belonging to completed turns")
            if any(row["role"] not in {"user", "assistant"} for row in rows):
                raise ValueError("consent source may only contain user/assistant messages")
            actual_ids = [int(row["id"]) for row in rows]
            if actual_ids != requested_ids:
                raise ValueError("consent source messages are invalid")
            user_settings = self.jobs.get_user_settings(actor.id, conn=conn)
            if not user_settings.opt_in:
                raise PermissionError("user memory opt-in is required")
            items = tuple(
                MemoryConsentSourceItem(
                    ordinal=index,
                    turn_id=str(row["turn_id"]),
                    message_id=int(row["id"]),
                    role=str(row["role"]),
                    content_hash=message_content_hash(row["role"], row["content"]),
                )
                for index, row in enumerate(rows)
            )
            manifest = MemoryConsentManifest(items=items)
            target = rows[-1]
            snapshot = self.jobs.create_consent_event(
                user_id=actor.id,
                session_id=int(session_id),
                turn_id=str(target["turn_id"]),
                message_id=int(target["id"]),
                policy_version=user_settings.policy_version,
                consent_revoke_generation=user_settings.revoke_generation,
                manifest=manifest,
                conn=conn,
            )
            job_id = self.jobs.enqueue_user_reflection(
                session_id=int(session_id),
                scope_fingerprint=scope_fingerprint(scope="user", user_id=actor.id),
                target_turn_id=str(target["turn_id"]),
                target_message_id=int(target["id"]),
                consent=snapshot,
                conn=conn,
            )
            self.catalog.audit("consent_granted", actor_id=str(actor.id), request_id=request_id, metadata={"consent_event_id": snapshot.consent_event_id, "session_id": int(session_id), "message_count": len(items), "reason": reason}, conn=conn)
            conn.commit()
            counter("hdb.memory.consent_granted")
            return {"consent_event_id": snapshot.consent_event_id, "job_id": job_id, "user_id": str(actor.id), "session_id": int(session_id), "policy_version": snapshot.policy_version, "revoke_generation": snapshot.consent_revoke_generation, "authorized_start_message_id": snapshot.authorized_start_message_id, "authorized_end_message_id": snapshot.authorized_end_message_id, "authorized_source_hash": snapshot.authorized_source_hash, "manifest": list(snapshot.manifest), "granted_at": snapshot.granted_at}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_memory_consents(self, actor: AuthUser | int | str, *, session_id: int | None = None) -> list[dict[str, Any]]:
        """Return a sanitized consent ledger for the authenticated user."""

        user_id = _actor_user_id(actor)
        if not user_id:
            raise MemoryAuthorizationError("user identity is required")
        with closing(self.jobs._connect()) as conn:
            rows = conn.execute(
                """SELECT c.consent_event_id, c.session_id, c.policy_version,
                          c.consent_revoke_generation, c.granted_at, c.revoked_at,
                          c.authorized_source_hash,
                          COUNT(i.message_id) AS source_count
                   FROM memory_consent_events c
                   LEFT JOIN memory_consent_source_items i
                     ON i.consent_event_id = c.consent_event_id
                   WHERE c.user_id = ? AND (? IS NULL OR c.session_id = ?)
                   GROUP BY c.consent_event_id
                   ORDER BY c.granted_at DESC, c.consent_event_id DESC""",
                (user_id, session_id, session_id),
            ).fetchall()
        return [
            {
                "consent_event_id": row["consent_event_id"],
                "session_id": int(row["session_id"]),
                "source_count": int(row["source_count"] or 0),
                "manifest_hash": row["authorized_source_hash"],
                "policy_version": row["policy_version"],
                "revoke_generation": int(row["consent_revoke_generation"]),
                "status": "revoked" if row["revoked_at"] else "active",
                "granted_at": row["granted_at"],
                "revoked_at": row["revoked_at"],
            }
            for row in rows
        ]

    def revoke_consent(self, actor: AuthUser, consent_event_id: str, *, reason: str = "explicit_revoke", request_id: str = "") -> bool:
        if not isinstance(actor, AuthUser) or not actor.is_active:
            raise MemoryAuthorizationError("active authenticated user is required")
        consent = self.jobs.get_consent(consent_event_id)
        if consent is None or consent.user_id != str(actor.id):
            raise MemoryAuthorizationError("consent is outside the authenticated user scope")
        result = self.jobs.revoke_consent(actor.id, consent_event_id, reason=reason, request_id=request_id)
        if result:
            counter("hdb.memory.consent_revoked")
        return result

    # Stable HTTP-facing names.  Keeping these aliases here means the route
    # layer cannot accidentally grow a second governance implementation.
    def verify_memory(self, *, actor, memory_id: str, expected_revision: int, evidence_refs: Iterable[Any], reason: str, request_id: str = "", **kwargs) -> dict[str, Any]:
        result = self.verify(
            memory_id,
            actor=actor,
            expected_revision=expected_revision,
            evidence_refs=evidence_refs,
            reason=reason,
            request_id=request_id,
            request_context=kwargs.get("request_context"),
            kb_name=kwargs.get("kb_name"),
        )
        return {
            "operation_id": result.get("operation_id", ""),
            "memory_id": memory_id,
            "status": result.get("status", "accepted"),
            "revision": (result.get("memory") or {}).get("revision"),
        }

    def reject_memory(self, *, actor, memory_id: str, expected_revision: int, reason: str, request_id: str = "", **kwargs) -> dict[str, Any]:
        result = self.reject(
            memory_id,
            actor=actor,
            expected_revision=expected_revision,
            reason=reason,
            request_id=request_id,
            request_context=kwargs.get("request_context"),
            kb_name=kwargs.get("kb_name"),
        )
        operation_id = (result.get("delete_operation_ids") or [""])[0]
        return {"operation_id": operation_id, "memory_id": memory_id, "status": result.get("status", "accepted"), "revision": (result.get("memory") or {}).get("revision")}

    def delete_memory(self, *, actor, memory_id: str, expected_revision: int, reason: str, request_id: str = "", **kwargs) -> dict[str, Any]:
        result = self.delete(
            memory_id,
            actor=actor,
            expected_revision=expected_revision,
            reason=reason,
            request_id=request_id,
            request_context=kwargs.get("request_context"),
            kb_name=kwargs.get("kb_name"),
        )
        operation_id = (result.get("delete_operation_ids") or [""])[0]
        return {"operation_id": operation_id, "memory_id": memory_id, "status": result.get("status", "accepted"), "revision": (result.get("memory") or {}).get("revision")}

    def supersede_memory(self, *, actor, memory_id: str, successor_memory_id: str | None, expected_revision: int, reason: str, request_id: str = "", **kwargs) -> dict[str, Any]:
        if not successor_memory_id:
            raise ValueError("successor_memory_id is required")
        result = self.supersede(
            memory_id,
            successor_memory_id,
            actor=actor,
            expected_revision=expected_revision,
            reason=reason,
            request_id=request_id,
            request_context=kwargs.get("request_context"),
            kb_name=kwargs.get("kb_name"),
        )
        operation_id = (result.get("delete_operation_ids") or [""])[0]
        return {"operation_id": operation_id, "memory_id": memory_id, "status": result.get("status", "accepted"), "revision": (result.get("memory") or {}).get("revision")}

    def set_user_memory_settings(self, *, actor, opt_in: bool, reason: str = "", request_id: str = "", **_kwargs) -> dict[str, Any]:
        user_id = _actor_user_id(actor)
        if not user_id:
            raise MemoryAuthorizationError("user identity is required")
        value = self.jobs.set_user_opt_in(
            user_id,
            bool(opt_in),
            reason=reason,
            request_id=request_id,
        )
        counter("hdb.memory.user_opt_in", attributes={"enabled": str(bool(opt_in)).lower()})
        return {
            "user_id": value.user_id,
            "opt_in": value.opt_in,
            "policy_version": value.policy_version,
            "revoke_generation": value.revoke_generation,
            "updated_at": value.updated_at,
        }

    def create_memory_consent(self, actor: AuthUser, session_id: int, message_ids: Iterable[int], *, reason: str = "", request_id: str = "", **_kwargs) -> dict[str, Any]:
        result = self.create_user_consent(actor, session_id, message_ids, reason=reason, request_id=request_id)
        result["status"] = "active"
        result["source_count"] = len(result.get("manifest") or [])
        result["manifest_hash"] = result.get("authorized_source_hash", "")
        return result

    def revoke_memory_consent(self, actor: AuthUser, consent_event_id: str, *, reason: str = "", request_id: str = "", **_kwargs) -> dict[str, Any]:
        if not self.revoke_consent(actor, consent_event_id, reason=reason or "explicit_revoke", request_id=request_id):
            raise MemoryNotFound("consent not found or already revoked")
        return {"operation_id": consent_event_id, "memory_id": "", "status": "revoked", "message": reason}


__all__ = [
    "MemoryAuthorizationError",
    "MemoryNotFound",
    "MemoryScope",
    "MemoryService",
    "MemoryServiceError",
    "message_content_hash",
]
