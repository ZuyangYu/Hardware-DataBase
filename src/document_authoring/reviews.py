"""Persistent, task-bound human review records.

The review adapter deliberately owns only review state and command
idempotency.  It does not know how a task, work order, artifact, or review UI
is implemented; callers provide those identifiers and the immutable hashes
that authorize a decision against the exact subject being reviewed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Mapping

from pydantic import BaseModel, Field, model_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    """Return the stable JSON representation used for stored payloads/hashes."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("review payload must be JSON serializable") from exc


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def resolve_review_decision_status(decision: Any, status: str | None = None) -> str:
    """Normalize a review command to the persisted lifecycle status."""
    if status is not None and str(status).strip():
        return str(status).strip()
    candidate = decision if isinstance(decision, str) else None
    if isinstance(decision, Mapping):
        for key in ("status", "outcome", "decision"):
            value = decision.get(key)
            if isinstance(value, str):
                candidate = value
                break
    normalized = str(candidate or "").strip().casefold()
    aliases = {
        "approve": "approved",
        "accept": "approved",
        "accepted": "approved",
        "approved": "approved",
        "reject": "rejected",
        "rejected": "rejected",
        "request_changes": "changes_requested",
        "changes_requested": "changes_requested",
        "cancel": "cancelled",
        "cancelled": "cancelled",
    }
    return aliases.get(normalized, "decided")


class DocumentReview(BaseModel):
    """A version-bound review item associated with one DocumentTask.

    ``client_request_id`` identifies the create request when present.
    Decision commands use a separate idempotency namespace because one review
    may be created by an internal projection and later decided by a client.
    """

    review_id: str = Field(default_factory=lambda: f"review-{uuid.uuid4().hex}")
    task_id: str
    work_order_id: str | None = None
    artifact_id: str | None = None
    review_kind: str
    status: str = "pending"
    subject_hash: str
    source_snapshot_hash: str
    schema_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    decision: Any | None = None
    decision_metadata: dict[str, Any] = Field(default_factory=dict)
    decision_client_request_id: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    decided_at: datetime | None = None
    client_request_id: str | None = None

    @model_validator(mode="after")
    def validate_review_identity(self) -> DocumentReview:
        for field_name in (
            "review_id",
            "task_id",
            "review_kind",
            "status",
            "subject_hash",
            "source_snapshot_hash",
            "schema_hash",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"review {field_name} is required")
            setattr(self, field_name, value)

        for field_name in ("work_order_id", "artifact_id", "client_request_id", "decision_client_request_id"):
            value = getattr(self, field_name)
            setattr(self, field_name, str(value).strip() if value is not None and str(value).strip() else None)

        self.created_at = _normalize_datetime(self.created_at)
        self.updated_at = _normalize_datetime(self.updated_at)
        if self.decided_at is not None:
            self.decided_at = _normalize_datetime(self.decided_at)
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


Review = DocumentReview


class DocumentReviewStore:
    """SQLite repository for reviews and their idempotent decision commands."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = os.fspath(db_path)
        if not self.db_path:
            raise ValueError("review database path is required")
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_reviews (
                    review_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    work_order_id TEXT,
                    artifact_id TEXT,
                    review_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    subject_hash TEXT NOT NULL,
                    source_snapshot_hash TEXT NOT NULL,
                    schema_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    decision_json TEXT,
                    decision_metadata_json TEXT NOT NULL,
                    decision_client_request_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    decided_at TEXT,
                    client_request_id TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_document_reviews_create_request
                    ON document_reviews(task_id, client_request_id)
                    WHERE client_request_id IS NOT NULL AND client_request_id != '';
                CREATE INDEX IF NOT EXISTS idx_document_reviews_task
                    ON document_reviews(task_id, created_at, review_id);
                CREATE TABLE IF NOT EXISTS document_review_decision_commands (
                    command_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    client_request_id TEXT NOT NULL,
                    command_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(review_id, client_request_id),
                    FOREIGN KEY(review_id) REFERENCES document_reviews(review_id)
                );
                CREATE INDEX IF NOT EXISTS idx_document_review_commands_review
                    ON document_review_decision_commands(review_id, created_at, command_id);
                """
            )

    @staticmethod
    def _review_from_row(row: sqlite3.Row | None) -> DocumentReview | None:
        if row is None:
            return None
        return DocumentReview.model_validate(json.loads(row["payload_json"]))

    @staticmethod
    def _creation_identity(review: DocumentReview) -> dict[str, Any]:
        payload = review.to_dict()
        return {
            field_name: payload[field_name]
            for field_name in (
                "task_id",
                "work_order_id",
                "artifact_id",
                "review_kind",
                "subject_hash",
                "source_snapshot_hash",
                "schema_hash",
                "metadata",
                "client_request_id",
            )
        }

    @staticmethod
    def _insert_review(connection: sqlite3.Connection, review: DocumentReview) -> None:
        payload = review.to_dict()
        connection.execute(
            """INSERT INTO document_reviews (
                   review_id, task_id, work_order_id, artifact_id, review_kind,
                   status, subject_hash, source_snapshot_hash, schema_hash,
                   metadata_json, decision_json, decision_metadata_json,
                   decision_client_request_id, created_at, updated_at,
                   decided_at, client_request_id, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                review.review_id,
                review.task_id,
                review.work_order_id,
                review.artifact_id,
                review.review_kind,
                review.status,
                review.subject_hash,
                review.source_snapshot_hash,
                review.schema_hash,
                _canonical_json(review.metadata),
                _canonical_json(review.decision) if review.decision is not None else None,
                _canonical_json(review.decision_metadata),
                review.decision_client_request_id,
                review.created_at.isoformat(),
                review.updated_at.isoformat(),
                review.decided_at.isoformat() if review.decided_at else None,
                review.client_request_id,
                _canonical_json(payload),
            ),
        )

    def create(
        self,
        review: DocumentReview | Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> DocumentReview:
        """Persist a review, replaying an identical create request safely."""

        if review is not None and fields:
            raise TypeError("provide either a review object or review fields, not both")
        if review is None:
            current = DocumentReview(**fields)
        elif isinstance(review, DocumentReview):
            current = review
        else:
            current = DocumentReview.model_validate(review)

        requested_identity = self._creation_identity(current)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_row = None
                if current.client_request_id:
                    existing_row = connection.execute(
                        """SELECT * FROM document_reviews
                           WHERE task_id = ? AND client_request_id = ?""",
                        (current.task_id, current.client_request_id),
                    ).fetchone()
                if existing_row is not None:
                    existing = self._review_from_row(existing_row)
                    assert existing is not None
                    if self._creation_identity(existing) != requested_identity:
                        raise ValueError("review idempotency key conflicts with existing payload")
                    connection.execute("COMMIT")
                    return existing

                id_collision = connection.execute(
                    "SELECT * FROM document_reviews WHERE review_id = ?",
                    (current.review_id,),
                ).fetchone()
                if id_collision is not None:
                    raise ValueError("review id already exists")

                self._insert_review(connection, current)
                connection.execute("COMMIT")
                return current
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def get(self, review_id: str) -> DocumentReview | None:
        review_id = str(review_id or "").strip()
        if not review_id:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM document_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        return self._review_from_row(row)

    def list_for_task(
        self,
        task_id: str,
        *,
        review_kind: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[DocumentReview]:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise ValueError("task id is required")
        if limit is not None and limit < 1:
            raise ValueError("review list limit must be positive")

        clauses = ["task_id = ?"]
        parameters: list[Any] = [task_id]
        if review_kind is not None:
            clauses.append("review_kind = ?")
            parameters.append(str(review_kind).strip())
        if status is not None:
            clauses.append("status = ?")
            parameters.append(str(status).strip())
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM document_reviews WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, review_id"
                + limit_clause,
                parameters,
            ).fetchall()
        return [review for row in rows if (review := self._review_from_row(row)) is not None]

    @staticmethod
    def _decision_status(decision: Any, status: str | None) -> str:
        return resolve_review_decision_status(decision, status)

    def submit_decision(
        self,
        review_id: str,
        *,
        subject_hash: str,
        decision: Any,
        client_request_id: str,
        status: str | None = None,
        decision_metadata: Mapping[str, Any] | None = None,
    ) -> DocumentReview:
        """Apply one hash-bound decision, replaying an identical command.

        A subject hash mismatch is rejected even for a retry.  A different
        command cannot overwrite an already decided review; a caller must
        create a new review for a new subject or decision workflow.
        """

        review_id = str(review_id or "").strip()
        requested_subject_hash = str(subject_hash or "").strip()
        request_id = str(client_request_id or "").strip()
        if not review_id:
            raise ValueError("review id is required")
        if not requested_subject_hash:
            raise ValueError("subject hash is required")
        if not request_id:
            raise ValueError("client request id is required")
        if decision_metadata is not None and not isinstance(decision_metadata, Mapping):
            raise ValueError("decision metadata must be an object")

        resolved_status = self._decision_status(decision, status)
        metadata = dict(decision_metadata or {})
        command_payload = {
            "review_id": review_id,
            "subject_hash": requested_subject_hash,
            "decision": decision,
            "status": resolved_status,
            "decision_metadata": metadata,
        }
        command_hash = _hash_payload(command_payload)
        now = _utc_now()

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM document_reviews WHERE review_id = ?",
                    (review_id,),
                ).fetchone()
                review = self._review_from_row(row)
                if review is None:
                    raise KeyError("review not found")
                if review.subject_hash != requested_subject_hash:
                    raise ValueError("review subject hash does not match current subject")

                command_row = connection.execute(
                    """SELECT * FROM document_review_decision_commands
                       WHERE review_id = ? AND client_request_id = ?""",
                    (review_id, request_id),
                ).fetchone()
                if command_row is not None:
                    if command_row["command_hash"] != command_hash:
                        raise ValueError("review decision idempotency key conflicts with existing payload")
                    connection.execute("COMMIT")
                    return review

                if review.decision is not None or review.decided_at is not None:
                    raise ValueError("review decision has already been submitted")

                decided = review.model_copy(
                    update={
                        "status": resolved_status,
                        "decision": decision,
                        "decision_metadata": metadata,
                        "decision_client_request_id": request_id,
                        "updated_at": now,
                        "decided_at": now,
                    },
                )
                connection.execute(
                    """INSERT INTO document_review_decision_commands (
                           command_id, review_id, client_request_id, command_hash,
                           created_at, payload_json
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"review-decision-{uuid.uuid4().hex}",
                        review_id,
                        request_id,
                        command_hash,
                        now.isoformat(),
                        _canonical_json(command_payload),
                    ),
                )
                connection.execute(
                    """UPDATE document_reviews SET
                           status = ?, decision_json = ?, decision_metadata_json = ?,
                           decision_client_request_id = ?, updated_at = ?,
                           decided_at = ?, payload_json = ?
                       WHERE review_id = ?""",
                    (
                        decided.status,
                        _canonical_json(decided.decision),
                        _canonical_json(decided.decision_metadata),
                        decided.decision_client_request_id,
                        decided.updated_at.isoformat(),
                        decided.decided_at.isoformat() if decided.decided_at else None,
                        _canonical_json(decided.to_dict()),
                        review_id,
                    ),
                )
                connection.execute("COMMIT")
                return decided
            except Exception:
                connection.execute("ROLLBACK")
                raise


ReviewStore = DocumentReviewStore


__all__ = [
    "DocumentReview",
    "DocumentReviewStore",
    "Review",
    "ReviewStore",
    "resolve_review_decision_status",
]
