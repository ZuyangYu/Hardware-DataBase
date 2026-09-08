"""Compatibility telemetry, legacy markers and the document-authoring closure.

The migration path is additive.  Historical rows remain readable and are
classified as legacy; new document writes can be required to carry accepted
``OutputSpec``/``DocumentPlan`` references once the deployment closure flag is
enabled.  This module deliberately stores identifiers, versions and hashes,
never source content or package bytes.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

import src.settings
from src.document_authoring.harness.idempotency import canonical_json
from src.observability.metrics import record_document_compatibility


LEGACY_DIRECT_EXECUTION = "legacy_direct_execution"
PLAN_BACKED_EXECUTION = "plan_backed_execution"
AUTO_CONFIRMATION_ATTEMPT = "auto_confirmation_attempt"
NEW_WRITE = "new_write"
LEGACY_WRITE = "legacy_write"

_EVENT_TYPES = frozenset({
    LEGACY_DIRECT_EXECUTION,
    PLAN_BACKED_EXECUTION,
    AUTO_CONFIRMATION_ATTEMPT,
    NEW_WRITE,
    LEGACY_WRITE,
})
_FORBIDDEN_KEYS = frozenset({
    "api_key", "credential", "credentials", "content", "evidence",
    "evidence_content", "file", "password", "path", "prompt",
    "raw_content", "secret", "storage_ref", "template_bytes", "token",
})


class CompatibilityClosureError(ValueError):
    """Raised when a new document write does not satisfy closure policy."""


class CompatibilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_payload(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text.casefold() in _FORBIDDEN_KEYS:
                raise ValueError(f"{path} contains forbidden key: {key_text}")
            _safe_payload(child, path=f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _safe_payload(child, path=f"{path}[{index}]")


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _required_text(source: Any, key: str, *, label: str | None = None) -> str:
    value = str(_value(source, key, "") or "").strip()
    if not value:
        raise CompatibilityClosureError(f"{label or key} is required for a plan-backed write")
    return value


class AcceptedPlanReferences(CompatibilityModel):
    """The complete immutable reference set required on a new v2 write."""

    output_spec_id: str = Field(min_length=1, max_length=256)
    output_spec_version: int = Field(ge=1)
    output_spec_hash: str = Field(min_length=1, max_length=256)
    document_plan_id: str = Field(min_length=1, max_length=256)
    document_plan_version: int = Field(ge=1)
    document_plan_hash: str = Field(min_length=1, max_length=256)


class CompatibilityAuditEvent(CompatibilityModel):
    """Safe, append-only audit projection for one compatibility event."""

    event_id: str = Field(default_factory=lambda: f"compat-event-{uuid.uuid4().hex}", min_length=1, max_length=256)
    event_type: str = Field(min_length=1, max_length=128)
    route: Literal["legacy", "plan_backed", "conversation", "system"] = "system"
    operation: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(default="default", min_length=1, max_length=256)
    entity_type: str = Field(default="document", min_length=1, max_length=128)
    entity_id: str = Field(default="unknown", min_length=1, max_length=256)
    plan_id: str | None = Field(default=None, max_length=256)
    plan_version: int | None = Field(default=None, ge=1)
    plan_hash: str | None = Field(default=None, max_length=256)
    payload: dict[str, Any] = Field(default_factory=dict, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=512)
    occurred_at: datetime = Field(default_factory=_now)
    event_hash: str | None = None

    @model_validator(mode="after")
    def validate_event(self) -> "CompatibilityAuditEvent":
        self.event_type = self.event_type.strip().lower()
        self.operation = self.operation.strip()
        self.tenant_id = self.tenant_id.strip()
        self.entity_type = self.entity_type.strip()
        self.entity_id = self.entity_id.strip()
        self.idempotency_key = self.idempotency_key.strip()
        if self.event_type not in _EVENT_TYPES and not self.event_type.startswith("compatibility_"):
            raise ValueError(f"unsupported compatibility event type: {self.event_type}")
        if self.plan_version is not None and not self.plan_id:
            raise ValueError("plan_version requires plan_id")
        if self.plan_hash is not None and not self.plan_id:
            raise ValueError("plan_hash requires plan_id")
        _safe_payload(self.payload)
        expected = _content_hash(self.model_dump(mode="json", exclude={"event_id", "occurred_at", "event_hash"}))
        if self.event_hash is not None and self.event_hash != expected:
            raise ValueError("event_hash does not match compatibility event contents")
        object.__setattr__(self, "event_hash", expected)
        return self


class CompatibilityCounters(CompatibilityModel):
    legacy_direct_execution: int = Field(default=0, ge=0)
    plan_backed_execution: int = Field(default=0, ge=0)
    auto_confirmation_attempt: int = Field(default=0, ge=0)
    new_write: int = Field(default=0, ge=0)
    legacy_write: int = Field(default=0, ge=0)

    @property
    def new_write_count(self) -> int:
        return self.new_write

    @property
    def legacy_write_count(self) -> int:
        return self.legacy_write


class LegacyBackfillMarker(CompatibilityModel):
    """Read-only classification for one historical entity.

    ``plan_id``/``plan_hash`` are intentionally nullable.  Supplying either
    one is an error rather than an opportunity to invent a historical plan.
    """

    marker_id: str = Field(default_factory=lambda: f"legacy-marker-{uuid.uuid4().hex}", min_length=1, max_length=256)
    entity_type: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    legacy_status: Literal["legacy"] = "legacy"
    reason: str = Field(min_length=1, max_length=1_000)
    source_fingerprint: str | None = Field(default=None, max_length=256)
    plan_id: str | None = Field(default=None, max_length=256)
    plan_version: int | None = Field(default=None, ge=1)
    plan_hash: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)
    created_at: datetime = Field(default_factory=_now)
    marker_hash: str | None = None

    @model_validator(mode="after")
    def validate_marker(self) -> "LegacyBackfillMarker":
        if self.plan_id or self.plan_version is not None or self.plan_hash:
            raise ValueError("historical backfill markers must not fabricate a plan reference")
        _safe_payload(self.metadata, path="metadata")
        expected = _content_hash(self.model_dump(mode="json", exclude={"marker_id", "created_at", "marker_hash"}))
        if self.marker_hash is not None and self.marker_hash != expected:
            raise ValueError("marker_hash does not match legacy marker contents")
        object.__setattr__(self, "marker_hash", expected)
        return self


class ReleaseObservation(CompatibilityModel):
    """Evidence captured for one deployment release observation window."""

    release_id: str = Field(default_factory=lambda: f"compat-release-{uuid.uuid4().hex}", min_length=1, max_length=256)
    release_label: str = Field(min_length=1, max_length=256)
    backup_verified: bool = False
    restore_verified: bool = False
    old_fields_read_only: bool = False
    legacy_write_count: int = Field(default=0, ge=0)
    new_write_count: int = Field(default=0, ge=0)
    observed_at: datetime = Field(default_factory=_now)
    observation_hash: str | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> "ReleaseObservation":
        expected = _content_hash(self.model_dump(mode="json", exclude={"release_id", "observed_at", "observation_hash"}))
        if self.observation_hash is not None and self.observation_hash != expected:
            raise ValueError("observation_hash does not match release observation contents")
        object.__setattr__(self, "observation_hash", expected)
        return self


class CompatibilityReadinessReport(CompatibilityModel):
    ready: bool = False
    release_count: int = Field(default=0, ge=0)
    backup_verified: bool = False
    restore_verified: bool = False
    old_fields_read_only: bool = False
    no_new_legacy_writes: bool = False
    reasons: list[str] = Field(default_factory=list, max_length=64)


class SqliteBackupReport(CompatibilityModel):
    database_path: str
    backup_path: str
    safety_backup_path: str | None = None
    verified: bool = False
    integrity_check: str = ""
    file_hash: str = ""
    table_row_counts: dict[str, int] = Field(default_factory=dict)


class DocumentAuthoringCompatibilityStore:
    """SQLite repository for compatibility events, markers and observations."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_authoring_compatibility_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    route TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    plan_id TEXT,
                    plan_version INTEGER,
                    plan_hash TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    event_hash TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_document_authoring_compat_events_type
                    ON document_authoring_compatibility_events(event_type, occurred_at, event_id);
                CREATE TABLE IF NOT EXISTS document_authoring_legacy_backfill_markers (
                    marker_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    legacy_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_fingerprint TEXT,
                    plan_id TEXT,
                    plan_version INTEGER,
                    plan_hash TEXT,
                    marker_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(entity_type, entity_id)
                );
                CREATE TABLE IF NOT EXISTS document_authoring_compatibility_releases (
                    release_id TEXT PRIMARY KEY,
                    release_label TEXT NOT NULL UNIQUE,
                    backup_verified INTEGER NOT NULL,
                    restore_verified INTEGER NOT NULL,
                    old_fields_read_only INTEGER NOT NULL,
                    legacy_write_count INTEGER NOT NULL,
                    new_write_count INTEGER NOT NULL,
                    observation_hash TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def record_event(self, event: CompatibilityAuditEvent) -> CompatibilityAuditEvent:
        value = event if isinstance(event, CompatibilityAuditEvent) else CompatibilityAuditEvent.model_validate(event)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_row = connection.execute(
                    "SELECT payload_json FROM document_authoring_compatibility_events WHERE idempotency_key = ?",
                    (value.idempotency_key,),
                ).fetchone()
                if existing_row is not None:
                    existing = CompatibilityAuditEvent.model_validate(_load_json(existing_row["payload_json"]))
                    if existing.event_hash != value.event_hash:
                        raise ValueError("compatibility event idempotency key conflicts with an existing event")
                    connection.execute("COMMIT")
                    return existing
                connection.execute(
                    """INSERT INTO document_authoring_compatibility_events
                       (event_id, event_type, route, operation, tenant_id, entity_type, entity_id,
                        plan_id, plan_version, plan_hash, idempotency_key, event_hash, occurred_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        value.event_id, value.event_type, value.route, value.operation,
                        value.tenant_id, value.entity_type, value.entity_id, value.plan_id,
                        value.plan_version, value.plan_hash, value.idempotency_key,
                        value.event_hash, value.occurred_at.isoformat(), _json(value),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return value

    def list_events(self, *, event_type: str | None = None) -> list[CompatibilityAuditEvent]:
        sql = "SELECT payload_json FROM document_authoring_compatibility_events"
        params: list[Any] = []
        if event_type:
            sql += " WHERE event_type = ?"
            params.append(str(event_type).strip().lower())
        sql += " ORDER BY occurred_at, event_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [CompatibilityAuditEvent.model_validate(_load_json(row["payload_json"])) for row in rows]

    def count_events(self, event_type: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM document_authoring_compatibility_events WHERE event_type = ?",
                (str(event_type).strip().lower(),),
            ).fetchone()
        return int(row["count"] or 0)

    def record_marker(self, marker: LegacyBackfillMarker) -> LegacyBackfillMarker:
        value = marker if isinstance(marker, LegacyBackfillMarker) else LegacyBackfillMarker.model_validate(marker)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload_json FROM document_authoring_legacy_backfill_markers WHERE entity_type = ? AND entity_id = ?",
                    (value.entity_type, value.entity_id),
                ).fetchone()
                if row is not None:
                    existing = LegacyBackfillMarker.model_validate(_load_json(row["payload_json"]))
                    if existing.marker_hash != value.marker_hash:
                        raise ValueError("legacy marker conflicts with an existing entity classification")
                    connection.execute("COMMIT")
                    return existing
                connection.execute(
                    """INSERT INTO document_authoring_legacy_backfill_markers
                       (marker_id, entity_type, entity_id, legacy_status, reason, source_fingerprint,
                        plan_id, plan_version, plan_hash, marker_hash, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        value.marker_id, value.entity_type, value.entity_id, value.legacy_status,
                        value.reason, value.source_fingerprint, value.plan_id, value.plan_version,
                        value.plan_hash, value.marker_hash, value.created_at.isoformat(), _json(value),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return value

    def get_marker(self, entity_type: str, entity_id: str) -> LegacyBackfillMarker | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM document_authoring_legacy_backfill_markers WHERE entity_type = ? AND entity_id = ?",
                (str(entity_type).strip(), str(entity_id).strip()),
            ).fetchone()
        return LegacyBackfillMarker.model_validate(_load_json(row["payload_json"])) if row else None

    def list_markers(self, *, entity_type: str | None = None) -> list[LegacyBackfillMarker]:
        sql = "SELECT payload_json FROM document_authoring_legacy_backfill_markers"
        params: list[Any] = []
        if entity_type:
            sql += " WHERE entity_type = ?"
            params.append(str(entity_type).strip())
        sql += " ORDER BY entity_type, entity_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [LegacyBackfillMarker.model_validate(_load_json(row["payload_json"])) for row in rows]

    def record_release_observation(self, observation: ReleaseObservation) -> ReleaseObservation:
        value = observation if isinstance(observation, ReleaseObservation) else ReleaseObservation.model_validate(observation)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload_json FROM document_authoring_compatibility_releases WHERE release_label = ?",
                    (value.release_label,),
                ).fetchone()
                if row is not None:
                    existing = ReleaseObservation.model_validate(_load_json(row["payload_json"]))
                    if existing.observation_hash != value.observation_hash:
                        raise ValueError("release observation label conflicts with an existing observation")
                    connection.execute("COMMIT")
                    return existing
                connection.execute(
                    """INSERT INTO document_authoring_compatibility_releases
                       (release_id, release_label, backup_verified, restore_verified, old_fields_read_only,
                        legacy_write_count, new_write_count, observation_hash, observed_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        value.release_id, value.release_label, int(value.backup_verified),
                        int(value.restore_verified), int(value.old_fields_read_only),
                        value.legacy_write_count, value.new_write_count, value.observation_hash,
                        value.observed_at.isoformat(), _json(value),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return value

    def list_release_observations(self) -> list[ReleaseObservation]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM document_authoring_compatibility_releases ORDER BY observed_at, release_id"
            ).fetchall()
        return [ReleaseObservation.model_validate(_load_json(row["payload_json"])) for row in rows]


class DocumentAuthoringCompatibilityService:
    """Facade used by authoring boundaries and deployment readiness checks."""

    def __init__(
        self,
        *,
        store: DocumentAuthoringCompatibilityStore | None = None,
        db_path: str | os.PathLike[str] | None = None,
    ):
        self.store = store or DocumentAuthoringCompatibilityStore(
            db_path or getattr(src.settings, "DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH", "compatibility.db")
        )

    @property
    def closure_enabled(self) -> bool:
        return bool(getattr(src.settings, "DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED", False))

    def record_event(
        self,
        *,
        event_type: str,
        operation: str,
        tenant_id: str = "default",
        entity_type: str = "document",
        entity_id: str = "unknown",
        route: str = "system",
        plan_id: str | None = None,
        plan_version: int | None = None,
        plan_hash: str | None = None,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CompatibilityAuditEvent:
        event = CompatibilityAuditEvent(
            event_type=event_type,
            route=route,
            operation=operation,
            tenant_id=str(tenant_id or "default").strip() or "default",
            entity_type=str(entity_type or "document").strip() or "document",
            entity_id=str(entity_id or "unknown").strip() or "unknown",
            plan_id=str(plan_id).strip() if plan_id is not None else None,
            plan_version=plan_version,
            plan_hash=str(plan_hash).strip() if plan_hash is not None else None,
            payload=dict(payload or {}),
            idempotency_key=idempotency_key or f"compat:{uuid.uuid4().hex}",
        )
        persisted = self.store.record_event(event)
        try:
            record_document_compatibility(event_type=persisted.event_type, route=persisted.route)
        except Exception:
            pass
        return persisted

    def record_legacy_direct_execution(self, **kwargs: Any) -> CompatibilityAuditEvent:
        kwargs.setdefault("route", "legacy")
        return self.record_event(event_type=LEGACY_DIRECT_EXECUTION, **kwargs)

    def record_plan_backed_execution(self, **kwargs: Any) -> CompatibilityAuditEvent:
        kwargs.setdefault("route", "plan_backed")
        return self.record_event(event_type=PLAN_BACKED_EXECUTION, **kwargs)

    def record_auto_confirmation_attempt(self, **kwargs: Any) -> CompatibilityAuditEvent:
        kwargs.setdefault("route", "conversation")
        outcome = kwargs.pop("outcome", None)
        if outcome is not None:
            payload = dict(kwargs.pop("payload", {}) or {})
            payload["outcome"] = str(outcome).strip()
            kwargs["payload"] = payload
        return self.record_event(event_type=AUTO_CONFIRMATION_ATTEMPT, **kwargs)

    def record_new_write(self, **kwargs: Any) -> CompatibilityAuditEvent:
        kwargs.setdefault("route", "plan_backed")
        return self.record_event(event_type=NEW_WRITE, **kwargs)

    def record_legacy_write(self, **kwargs: Any) -> CompatibilityAuditEvent:
        kwargs.setdefault("route", "legacy")
        return self.record_event(event_type=LEGACY_WRITE, **kwargs)

    def counter_snapshot(self) -> dict[str, int]:
        return CompatibilityCounters(
            legacy_direct_execution=self.store.count_events(LEGACY_DIRECT_EXECUTION),
            plan_backed_execution=self.store.count_events(PLAN_BACKED_EXECUTION),
            auto_confirmation_attempt=self.store.count_events(AUTO_CONFIRMATION_ATTEMPT),
            new_write=self.store.count_events(NEW_WRITE),
            legacy_write=self.store.count_events(LEGACY_WRITE),
        ).model_dump()

    def mark_legacy(
        self,
        *,
        entity_type: str,
        entity_id: str,
        reason: str,
        source_fingerprint: str | None = None,
        plan_id: str | None = None,
        plan_version: int | None = None,
        plan_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> LegacyBackfillMarker:
        marker = LegacyBackfillMarker(
            entity_type=str(entity_type).strip(), entity_id=str(entity_id).strip(),
            reason=str(reason).strip(), source_fingerprint=(str(source_fingerprint).strip() if source_fingerprint else None),
            plan_id=plan_id, plan_version=plan_version, plan_hash=plan_hash,
            metadata=dict(metadata or {}),
        )
        return self.store.record_marker(marker)

    def backfill_legacy_records(
        self,
        entity_type: str,
        records: Sequence[Any],
        *,
        reason: str = "historical_legacy_record",
    ) -> list[LegacyBackfillMarker]:
        normalized_type = str(entity_type or "").strip()
        if not normalized_type:
            raise ValueError("entity_type is required for legacy backfill")
        markers: list[LegacyBackfillMarker] = []
        keys = (
            f"{normalized_type}_id", "session_id", "work_order_id", "artifact_id", "run_id",
            "harness_run_id", "entity_id",
        )
        for record in records:
            identifier = next((str(_value(record, key, "") or "").strip() for key in keys if _value(record, key, "")), "")
            if not identifier:
                raise ValueError(f"legacy {normalized_type} record is missing an identity")
            fingerprint = next(
                (
                    str(_value(record, key, "") or "").strip()
                    for key in ("input_fingerprint", "content_hash", "source_snapshot_hash")
                    if str(_value(record, key, "") or "").strip()
                ),
                None,
            )
            markers.append(self.mark_legacy(
                entity_type=normalized_type,
                entity_id=identifier,
                reason=reason,
                source_fingerprint=fingerprint,
            ))
        return markers

    def accepted_plan_references(
        self,
        *,
        output_spec: Any | None = None,
        document_plan: Any | None = None,
        references: Mapping[str, Any] | None = None,
    ) -> AcceptedPlanReferences:
        if output_spec is None and document_plan is None and references is not None:
            output_spec = references
            document_plan = references
        if output_spec is None or document_plan is None:
            raise CompatibilityClosureError(
                "new document-authoring writes require accepted OutputSpec and DocumentPlan references"
            )
        if str(_value(output_spec, "status", "")).strip() != "accepted":
            raise CompatibilityClosureError("OutputSpec must be accepted for a plan-backed write")
        if str(_value(document_plan, "status", "")).strip() != "accepted":
            raise CompatibilityClosureError("DocumentPlan must be accepted for a plan-backed write")
        spec = AcceptedPlanReferences(
            output_spec_id=_required_text(output_spec, "output_spec_id"),
            output_spec_version=int(_value(output_spec, "version", _value(output_spec, "output_spec_version", 0)) or 0),
            output_spec_hash=_required_text(output_spec, "content_hash", label="output_spec_hash"),
            document_plan_id=_required_text(document_plan, "document_plan_id"),
            document_plan_version=int(_value(document_plan, "version", _value(document_plan, "document_plan_version", 0)) or 0),
            document_plan_hash=_required_text(document_plan, "plan_hash"),
        )
        plan_spec_id = str(_value(document_plan, "output_spec_id", "") or "").strip()
        plan_spec_version = int(_value(document_plan, "output_spec_version", 0) or 0)
        plan_spec_hash = str(_value(document_plan, "output_spec_hash", "") or "").strip()
        if (
            plan_spec_id != spec.output_spec_id
            or plan_spec_version != spec.output_spec_version
            or plan_spec_hash != spec.output_spec_hash
        ):
            raise CompatibilityClosureError(
                "DocumentPlan is not bound to the supplied accepted OutputSpec"
            )
        return spec

    def validate_new_document_write(
        self,
        *,
        operation: str,
        output_spec: Any | None = None,
        document_plan: Any | None = None,
        references: Mapping[str, Any] | None = None,
    ) -> AcceptedPlanReferences | None:
        supplied = output_spec is not None or document_plan is not None or references is not None
        if supplied:
            try:
                return self.accepted_plan_references(
                    output_spec=output_spec, document_plan=document_plan, references=references,
                )
            except (TypeError, ValueError) as exc:
                if isinstance(exc, CompatibilityClosureError):
                    raise
                raise CompatibilityClosureError(str(exc)) from exc
        if self.closure_enabled:
            raise CompatibilityClosureError(
                f"direct {str(operation).strip() or 'document'} writes are closed; accepted plan references are required"
            )
        return None

    def record_release_observation(
        self,
        *,
        release_label: str,
        backup_verified: bool = False,
        restore_verified: bool = False,
        old_fields_read_only: bool = False,
        legacy_write_count: int | None = None,
        new_write_count: int | None = None,
        release_id: str | None = None,
    ) -> ReleaseObservation:
        counters = self.counter_snapshot()
        observation = ReleaseObservation(
            release_id=release_id or f"compat-release-{uuid.uuid4().hex}",
            release_label=release_label,
            backup_verified=backup_verified,
            restore_verified=restore_verified,
            old_fields_read_only=old_fields_read_only,
            legacy_write_count=(counters["legacy_write"] if legacy_write_count is None else legacy_write_count),
            new_write_count=(counters["new_write"] if new_write_count is None else new_write_count),
        )
        return self.store.record_release_observation(observation)

    def evaluate_closure_readiness(self) -> CompatibilityReadinessReport:
        observations = self.store.list_release_observations()
        reasons: list[str] = []
        if len(observations) < 2:
            reasons.append("two_release_observation_required")
        backup_verified = bool(observations) and all(item.backup_verified for item in observations)
        restore_verified = bool(observations) and all(item.restore_verified for item in observations)
        no_new_legacy_writes = bool(observations) and all(item.legacy_write_count == 0 for item in observations)
        old_fields_read_only = bool(observations) and observations[-1].old_fields_read_only
        if not backup_verified:
            reasons.append("backup_not_verified")
        if not restore_verified:
            reasons.append("restore_not_verified")
        if not no_new_legacy_writes:
            reasons.append("legacy_writes_observed")
        if not old_fields_read_only:
            reasons.append("old_fields_not_read_only")
        return CompatibilityReadinessReport(
            ready=not reasons,
            release_count=len(observations),
            backup_verified=backup_verified,
            restore_verified=restore_verified,
            old_fields_read_only=old_fields_read_only,
            no_new_legacy_writes=no_new_legacy_writes,
            reasons=list(dict.fromkeys(reasons)),
        )

    def assert_closure_ready(self) -> CompatibilityReadinessReport:
        report = self.evaluate_closure_readiness()
        if not report.ready:
            raise CompatibilityClosureError(
                "compatibility closure readiness failed: " + ", ".join(report.reasons)
            )
        return report


def create_sqlite_backup(
    database_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
) -> SqliteBackupReport:
    """Create and verify a consistent SQLite backup without changing the source."""

    database = Path(database_path).resolve()
    backup = Path(backup_path).resolve()
    if database == backup:
        raise ValueError("database and backup paths must be different")
    if not database.is_file():
        raise FileNotFoundError(str(database))
    backup.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{backup.name}.", suffix=".tmp", dir=backup.parent, delete=False) as stream:
            temporary = Path(stream.name)
        with sqlite3.connect(str(database)) as source, sqlite3.connect(str(temporary)) as target:
            source.backup(target)
        os.replace(temporary, backup)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return _verify_sqlite_file(database, backup)


def restore_sqlite_backup(
    database_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
    *,
    safety_backup_path: str | os.PathLike[str] | None = None,
) -> SqliteBackupReport:
    """Restore a verified backup, retaining a safety copy of the current DB."""

    database = Path(database_path).resolve()
    backup = Path(backup_path).resolve()
    if database == backup:
        raise ValueError("database and backup paths must be different")
    source_report = _verify_sqlite_file(backup, backup)
    if not source_report.verified:
        raise ValueError("SQLite backup failed integrity verification")
    safety = Path(safety_backup_path).resolve() if safety_backup_path else database.with_name(f"{database.name}.pre-restore.bak")
    if safety in {database, backup}:
        raise ValueError("safety backup path must be distinct from database and backup")
    if database.is_file():
        create_sqlite_backup(database, safety)
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{database.name}.restore.", suffix=".tmp", dir=database.parent, delete=False) as stream:
            temporary = Path(stream.name)
        with sqlite3.connect(str(backup)) as source, sqlite3.connect(str(temporary)) as target:
            source.backup(target)
        checked = _verify_sqlite_file(temporary, backup)
        if not checked.verified:
            raise ValueError("restored SQLite file failed integrity verification")
        os.replace(temporary, database)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    result = _verify_sqlite_file(database, backup)
    result.safety_backup_path = str(safety) if safety.exists() else None
    return result


def _verify_sqlite_file(database_path: Path, report_path: Path) -> SqliteBackupReport:
    integrity = ""
    counts: dict[str, int] = {}
    try:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall():
                name = str(row[0])
                counts[name] = int(connection.execute(f' SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"').fetchone()[0])
    except sqlite3.DatabaseError as exc:
        integrity = str(exc)
    file_hash = ""
    if report_path.is_file():
        file_hash = f"sha256:{hashlib.sha256(report_path.read_bytes()).hexdigest()}"
    return SqliteBackupReport(
        database_path=str(database_path), backup_path=str(report_path), verified=integrity == "ok",
        integrity_check=integrity, file_hash=file_hash, table_row_counts=counts,
    )


def _json(value: Any) -> str:
    return canonical_json(value.model_dump(mode="json") if isinstance(value, BaseModel) else value)


def _load_json(raw: str) -> dict[str, Any]:
    import json

    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("compatibility persistence payload must be an object")
    return value


def _content_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


# Short aliases make the boundary easy to discover for integrations that use
# ``CompatibilityStore``/``CompatibilityService`` terminology.
CompatibilityStore = DocumentAuthoringCompatibilityStore
CompatibilityService = DocumentAuthoringCompatibilityService
BackfillMarker = LegacyBackfillMarker


__all__ = [
    "AUTO_CONFIRMATION_ATTEMPT",
    "AcceptedPlanReferences",
    "BackfillMarker",
    "CompatibilityAuditEvent",
    "CompatibilityClosureError",
    "CompatibilityCounters",
    "CompatibilityReadinessReport",
    "CompatibilityService",
    "CompatibilityStore",
    "DocumentAuthoringCompatibilityService",
    "DocumentAuthoringCompatibilityStore",
    "LEGACY_DIRECT_EXECUTION",
    "LEGACY_WRITE",
    "LegacyBackfillMarker",
    "NEW_WRITE",
    "PLAN_BACKED_EXECUTION",
    "ReleaseObservation",
    "SqliteBackupReport",
    "create_sqlite_backup",
    "restore_sqlite_backup",
]
