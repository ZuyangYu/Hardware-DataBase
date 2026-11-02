"""Controlled artifact transport for sandbox inputs and outputs.

The broker is the only HDB-side component that touches sandbox payload bytes.
It exposes opaque artifact IDs, stores files below a private root, performs
hash/size/archive checks, and applies task/owner/department ACLs.  It never
returns a host path or a long-lived object-store credential.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import src.settings as settings

from .artifacts import (
    ArtifactManifest,
    ArtifactRejected,
    infer_media_type,
    inspect_archive,
    new_artifact_id,
    validate_content,
    validate_digest,
    validate_artifact_id,
    validate_filename,
    validate_storage_ref,
)


class ArtifactBrokerError(RuntimeError):
    """Base broker error with a bounded reason label."""

    def __init__(self, message: str, *, code: str = "artifact_error", status_code: int = 400) -> None:
        self.code = str(code)[:80]
        self.status_code = int(status_code)
        super().__init__(str(message)[:300])


class ArtifactNotFound(ArtifactBrokerError):
    def __init__(self, artifact_id: str) -> None:
        super().__init__("artifact not found", code="not_found", status_code=404)
        self.artifact_id = str(artifact_id)


class ArtifactAccessDenied(ArtifactBrokerError):
    def __init__(self) -> None:
        super().__init__("artifact access denied", code="forbidden", status_code=403)


class ArtifactStateConflict(ArtifactBrokerError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="state_conflict", status_code=409)


_BLOCKED_METADATA_KEYS = frozenset(
    {
        "secret",
        "secrets",
        "token",
        "password",
        "authorization",
        "headers",
        "command",
        "shell",
        "script",
        "env",
        "environment",
        "host_path",
    }
)


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    def clean(item: Any, path: str = "metadata", depth: int = 0) -> Any:
        if depth > 4:
            raise ArtifactBrokerError("artifact metadata is too deeply nested", code="metadata")
        if isinstance(item, Mapping):
            output: dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key).strip()
                normalized = key.lower().replace("-", "_")
                if not key or len(key) > 80 or normalized in _BLOCKED_METADATA_KEYS:
                    raise ArtifactBrokerError(f"{path} contains a forbidden metadata field", code="metadata")
                output[key] = clean(child, f"{path}.{key}", depth + 1)
            return output
        if isinstance(item, (list, tuple)):
            if len(item) > 100:
                raise ArtifactBrokerError("artifact metadata contains too many values", code="metadata")
            return [clean(child, f"{path}[{index}]", depth + 1) for index, child in enumerate(item)]
        if item is None or isinstance(item, (str, int, float, bool)):
            if isinstance(item, str) and len(item) > 500:
                raise ArtifactBrokerError("artifact metadata value is too large", code="metadata")
            return item
        raise ArtifactBrokerError("artifact metadata must be JSON-compatible", code="metadata")

    result = clean(dict(value or {}))
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")) > 16 * 1024:
        raise ArtifactBrokerError("artifact metadata exceeds the configured limit", code="metadata")
    return result


_DDL = """
CREATE TABLE IF NOT EXISTS sandbox_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    execution_id TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL,
    department_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'staged',
    role TEXT NOT NULL DEFAULT 'input',
    scan_status TEXT NOT NULL DEFAULT 'pending',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    consumed_at TEXT NOT NULL DEFAULT '',
    UNIQUE(task_id, role, sha256, filename)
);
CREATE INDEX IF NOT EXISTS idx_sandbox_artifacts_task
    ON sandbox_artifacts(task_id, state, created_at);
CREATE INDEX IF NOT EXISTS idx_sandbox_artifacts_scope
    ON sandbox_artifacts(owner_id, department_id, state);
CREATE INDEX IF NOT EXISTS idx_sandbox_artifacts_expiry
    ON sandbox_artifacts(state, expires_at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_expiry(value: Any, *, retention_seconds: int) -> str:
    now = _now()
    if not str(value or "").strip():
        return _iso(now + timedelta(seconds=max(60, retention_seconds)))
    parsed = _parse_time(value)
    if parsed is None or parsed <= now:
        raise ArtifactBrokerError("artifact expiry must be a future ISO timestamp", code="expiry")
    if parsed > now + timedelta(seconds=max(60, retention_seconds)):
        raise ArtifactBrokerError("artifact expiry exceeds the retention window", code="expiry")
    return parsed.astimezone(timezone.utc).isoformat()


class ArtifactBroker:
    """Filesystem + SQLite implementation of the HDB artifact broker."""

    def __init__(
        self,
        storage_dir: str | None = None,
        db_path: str | None = None,
        *,
        scanner: Callable[[bytes, ArtifactManifest], Any] | None = None,
        retention_seconds: int | None = None,
    ) -> None:
        self.storage_dir = Path(
            storage_dir or getattr(settings, "SANDBOX_ARTIFACT_STORAGE_DIR", "sandbox_artifacts")
        ).absolute()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.storage_dir, 0o700)
        except OSError:
            pass
        self.db_path = os.path.abspath(str(db_path or getattr(settings, "SANDBOX_TASK_DB_PATH", "sandbox_tasks.db")))
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.scanner = scanner
        self.retention_seconds = int(
            retention_seconds
            if retention_seconds is not None
            else max(60, int(getattr(settings, "SANDBOX_TASK_TIMEOUT_SECONDS", 300)))
        )
        self._init_lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._init_lock, closing(self._connect()) as conn:
            conn.executescript(_DDL)

    @staticmethod
    def _row_to_manifest(row: sqlite3.Row) -> ArtifactManifest:
        metadata: dict[str, Any]
        try:
            metadata = dict(json.loads(str(row["metadata_json"] or "{}")) or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        return ArtifactManifest(
            artifact_id=str(row["artifact_id"]),
            task_id=str(row["task_id"]),
            execution_id=str(row["execution_id"] or ""),
            owner_id=str(row["owner_id"]),
            department_id=str(row["department_id"]),
            filename=str(row["filename"]),
            media_type=str(row["media_type"]),
            size=int(row["size"] or 0),
            sha256=str(row["sha256"]),
            storage_ref=str(row["storage_ref"]),
            state=str(row["state"]),
            role=str(row["role"]),
            scan_status=str(row["scan_status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            expires_at=str(row["expires_at"] or ""),
            consumed_at=str(row["consumed_at"] or ""),
            metadata=metadata,
        )

    def _row(self, artifact_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM sandbox_artifacts WHERE artifact_id = ?", (str(artifact_id),)
            ).fetchone()

    def get(self, artifact_id: str) -> ArtifactManifest | None:
        row = self._row(artifact_id)
        return self._row_to_manifest(row) if row else None

    def require(self, artifact_id: str) -> ArtifactManifest:
        manifest = self.get(artifact_id)
        if manifest is None:
            raise ArtifactNotFound(artifact_id)
        return manifest

    def list_for_task(self, task_id: str, *, states: set[str] | None = None) -> list[ArtifactManifest]:
        params: list[Any] = [str(task_id)]
        where = "task_id = ?"
        if states:
            wanted = sorted({str(item) for item in states})
            where += " AND state IN (" + ",".join("?" for _ in wanted) + ")"
            params.extend(wanted)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM sandbox_artifacts WHERE {where} ORDER BY created_at ASC", params
            ).fetchall()
        return [self._row_to_manifest(row) for row in rows]

    def _storage_path(self, manifest: ArtifactManifest) -> str:
        try:
            return validate_storage_ref(str(self.storage_dir), manifest.storage_ref)
        except ArtifactRejected as exc:
            raise ArtifactBrokerError(str(exc), code=exc.reason, status_code=400) from exc

    def _write_atomic(self, storage_ref: str, content: bytes) -> None:
        target = validate_storage_ref(str(self.storage_dir), storage_ref)
        parent = os.path.dirname(target)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        if os.path.lexists(target) and os.path.islink(target):
            raise ArtifactBrokerError("artifact target is a symlink", code="symlink")
        temp = os.path.join(parent, f".upload-{secrets.token_hex(12)}")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temp, flags, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            # ``replace`` would let a racing retry overwrite a committed
            # payload.  A hard-link create is atomic and fails if the opaque
            # artifact path already exists.
            os.link(temp, target)
            os.unlink(temp)
        except OSError as exc:
            raise ArtifactBrokerError("artifact could not be stored", code="store", status_code=507) from exc
        finally:
            try:
                if os.path.exists(temp):
                    os.unlink(temp)
            except OSError:
                pass

    def stage(
        self,
        *,
        task_id: str,
        owner_id: str | int,
        department_id: str | int,
        filename: str,
        content: bytes,
        sha256: str,
        execution_id: str = "",
        media_type: str = "",
        role: str = "input",
        expires_at: str = "",
        metadata: Mapping[str, Any] | None = None,
        artifact_id: str = "",
    ) -> ArtifactManifest:
        """Atomically stage bytes; callers must explicitly scan/commit them."""

        try:
            safe_filename = validate_filename(filename)
            data = validate_content(content, output=role == "output")
            digest = validate_digest(sha256, data)
            if safe_filename.lower().endswith(".zip"):
                inspect_archive(data)
        except ArtifactRejected as exc:
            raise ArtifactBrokerError(str(exc), code=exc.reason, status_code=400) from exc
        if role not in {"input", "output", "log"}:
            raise ArtifactBrokerError("artifact role is invalid", code="role")
        task = str(task_id or "").strip()
        owner = str(owner_id or "").strip()
        department = str(department_id or "").strip()
        if not task or not owner or not department:
            raise ArtifactBrokerError("task, owner and department are required", code="scope")
        artifact_id = str(artifact_id or new_artifact_id())
        # IDs generated by the broker are opaque.  For retries, accepting the
        # same valid ID is useful, but never permit path-like references.
        try:
            artifact_id = validate_artifact_id(artifact_id)
        except ArtifactRejected as exc:
            raise ArtifactBrokerError(str(exc), code=exc.reason) from exc

        def verified_retry(existing: ArtifactManifest) -> ArtifactManifest:
            if not (
                existing.sha256 == digest
                and existing.task_id == task
                and existing.owner_id == owner
                and existing.department_id == department
                and existing.role == role
                and existing.filename == safe_filename
                and existing.size == len(data)
                and existing.execution_id == str(execution_id or "")[:256]
                and existing.state not in {"rejected", "expired"}
            ):
                raise ArtifactStateConflict("artifact_id is already bound to another artifact")
            try:
                with open(self._storage_path(existing), "rb") as handle:
                    persisted = handle.read(len(data) + 1)
                validate_digest(digest, persisted)
            except (OSError, ArtifactRejected) as exc:
                raise ArtifactBrokerError(
                    "persisted artifact bytes are missing or invalid", code="hash", status_code=422
                ) from exc
            return existing

        # Check an explicitly supplied ID before writing bytes.  Otherwise a
        # retry with a conflicting ID could replace an existing payload before
        # the database conflict is noticed.
        existing_before_write = self.get(artifact_id)
        if existing_before_write is not None:
            return verified_retry(existing_before_write)
        with closing(self._connect()) as conn:
            same_content = conn.execute(
                "SELECT * FROM sandbox_artifacts WHERE task_id = ? AND role = ? AND sha256 = ? AND filename = ?",
                (task, role, digest, safe_filename),
            ).fetchone()
        if same_content is not None:
            return verified_retry(self._row_to_manifest(same_content))
        safe_metadata = _safe_metadata(metadata)
        now = _iso()
        expires_at = _normalize_expiry(expires_at, retention_seconds=self.retention_seconds)
        storage_ref = f"{artifact_id}/payload"
        try:
            self._write_atomic(storage_ref, data)
        except ArtifactBrokerError as exc:
            if exc.code != "store" or not os.path.lexists(
                validate_storage_ref(str(self.storage_dir), storage_ref)
            ):
                raise
            # Another collector may have linked the same immutable payload and
            # not yet inserted its row. Never overwrite the target or accept an
            # orphan file as evidence: wait briefly for the durable binding.
            for _ in range(100):
                existing = self.get(artifact_id)
                if existing is not None:
                    return verified_retry(existing)
                time.sleep(0.01)
            raise ArtifactBrokerError("artifact path has no durable record", code="store", status_code=507) from exc
        manifest = ArtifactManifest(
            artifact_id=artifact_id,
            task_id=task,
            execution_id=str(execution_id or "")[:256],
            owner_id=owner,
            department_id=department,
            filename=safe_filename,
            media_type=infer_media_type(safe_filename, media_type),
            size=len(data),
            sha256=digest,
            storage_ref=storage_ref,
            state="staged",
            role=role,
            scan_status="pending",
            created_at=now,
            updated_at=now,
            expires_at=str(expires_at)[:80],
            metadata=safe_metadata,
        )
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM sandbox_artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                if existing is not None:
                    current = self._row_to_manifest(existing)
                    verified_retry(current)
                    conn.execute("COMMIT")
                    return current
                conn.execute(
                    """
                    INSERT INTO sandbox_artifacts(
                        artifact_id, task_id, execution_id, owner_id, department_id,
                        filename, media_type, size, sha256, storage_ref, state, role,
                        scan_status, metadata_json, created_at, updated_at, expires_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, 'pending', ?, ?, ?, ?, '')
                    """,
                    (
                        artifact_id,
                        task,
                        manifest.execution_id,
                        owner,
                        department,
                        safe_filename,
                        manifest.media_type,
                        len(data),
                        digest,
                        storage_ref,
                        role,
                        _json(safe_metadata),
                        now,
                        now,
                        str(expires_at)[:80],
                    ),
                )
                conn.execute("COMMIT")
                return manifest
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                self._remove_uncommitted_payload(manifest)
                raise

    def stage_input(self, **kwargs: Any) -> ArtifactManifest:
        kwargs["role"] = "input"
        return self.stage(**kwargs)

    def stage_output(self, **kwargs: Any) -> ArtifactManifest:
        kwargs["role"] = "output"
        return self.stage(**kwargs)

    def _authorize(
        self,
        manifest: ArtifactManifest,
        *,
        principal_id: str | int | None = None,
        department_id: str | int | None = None,
        task_id: str | None = None,
        allow_department: bool = False,
    ) -> None:
        principal = str(principal_id or "")
        department = str(department_id or "")
        task = str(task_id or "")
        if principal and principal == manifest.owner_id:
            if not department or department == manifest.department_id:
                return
        # A service may access an artifact only when it presents the exact task
        # binding; a department alone is never sufficient for a runner read.
        if task and task == manifest.task_id and department and department == manifest.department_id and allow_department:
            return
        raise ArtifactAccessDenied()

    def authorize_input(
        self,
        artifact_id: str,
        *,
        principal_id: str | int,
        department_id: str | int,
    ) -> ArtifactManifest:
        """Validate an input reference before it is bound to a new task."""

        manifest = self.require(artifact_id)
        self._authorize(
            manifest,
            principal_id=principal_id,
            department_id=department_id,
            allow_department=False,
        )
        if manifest.role != "input":
            raise ArtifactStateConflict("only committed input artifacts may be task inputs")
        if manifest.state not in {"committed", "consumed"}:
            raise ArtifactStateConflict("input artifact is not committed")
        expires = str(manifest.expires_at or "").strip()
        if expires:
            try:
                deadline = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ArtifactStateConflict("input artifact expiry is invalid") from exc
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline <= _now():
                raise ArtifactStateConflict("input artifact has expired")
        return manifest

    def promote_output(
        self,
        artifact_id: str,
        *,
        principal_id: str | int,
        department_id: str | int,
        expires_at: str = "",
    ) -> ArtifactManifest:
        """Copy a committed output into a new input with explicit lineage.

        Outputs are never re-labelled in place: a new opaque input Artifact
        keeps the original digest, task and artifact IDs in its metadata.
        """

        manifest = self.require(artifact_id)
        self._authorize(
            manifest,
            principal_id=principal_id,
            department_id=department_id,
            allow_department=False,
        )
        if manifest.role != "output":
            raise ArtifactStateConflict("only output artifacts can be promoted")
        if manifest.state not in {"committed", "consumed"}:
            raise ArtifactStateConflict("output artifact is not committed")
        content = self.read(
            artifact_id,
            principal_id=principal_id,
            department_id=department_id,
        )
        staged = self.stage_input(
            task_id=manifest.task_id,
            owner_id=manifest.owner_id,
            department_id=manifest.department_id,
            filename=manifest.filename,
            content=content,
            sha256=manifest.sha256,
            execution_id=manifest.execution_id,
            media_type=manifest.media_type,
            expires_at=expires_at,
            metadata={
                "source_artifact_id": manifest.artifact_id,
                "source_task_id": manifest.task_id,
                "source_sha256": manifest.sha256,
                "promoted": True,
            },
        )
        self.scan(
            staged.artifact_id,
            principal_id=principal_id,
            department_id=department_id,
            task_id=manifest.task_id,
        )
        return self.commit(
            staged.artifact_id,
            principal_id=principal_id,
            department_id=department_id,
            task_id=manifest.task_id,
        )

    def _remove_uncommitted_payload(self, manifest: ArtifactManifest) -> None:
        """Remove a payload only when no durable row owns the same artifact ID."""

        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT state FROM sandbox_artifacts WHERE artifact_id = ?",
                    (manifest.artifact_id,),
                ).fetchone()
            if row is not None:
                return
            os.unlink(self._storage_path(manifest))
        except (OSError, ArtifactBrokerError):
            pass

    def scan(
        self,
        artifact_id: str,
        *,
        principal_id: str | int | None = None,
        department_id: str | int | None = None,
        task_id: str | None = None,
        scanner: Callable[[bytes, ArtifactManifest], Any] | None = None,
    ) -> ArtifactManifest:
        manifest = self.require(artifact_id)
        self._authorize(
            manifest,
            principal_id=principal_id,
            department_id=department_id,
            task_id=task_id,
            allow_department=True,
        )
        if manifest.state not in {"staged", "scanning"}:
            if manifest.state == "committed" or manifest.state == "consumed":
                return manifest
            raise ArtifactStateConflict("artifact is not scannable in its current state")
        path = self._storage_path(manifest)
        try:
            with open(path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            raise ArtifactBrokerError("artifact payload is unavailable", code="missing", status_code=410) from exc
        try:
            valid_digest = validate_digest(manifest.sha256, content)
        except ArtifactRejected as exc:
            self._set_state(manifest, "rejected", "failed")
            raise ArtifactBrokerError("artifact payload failed integrity check", code=exc.reason, status_code=422) from exc
        if len(content) != manifest.size or valid_digest != manifest.sha256:
            self._set_state(manifest, "rejected", "failed")
            raise ArtifactBrokerError("artifact payload changed", code="hash", status_code=422)
        if manifest.filename.lower().endswith(".zip"):
            try:
                inspect_archive(content)
            except ArtifactRejected as exc:
                self._set_state(manifest, "rejected", "failed")
                raise ArtifactBrokerError(str(exc), code=exc.reason, status_code=422) from exc
        checker = scanner or self.scanner
        if checker is None:
            # A deployment that registered a scanner reference must also
            # provide the callable.  Silently treating "no scanner" as clean
            # would let unscanned output reach a committed artifact.
            configured_ref = str(getattr(settings, "SANDBOX_ARTIFACT_SCANNER_REF", "") or "").strip()
            if configured_ref:
                raise ArtifactBrokerError(
                    "artifact scanner is registered but not available",
                    code="scanner_unavailable",
                    status_code=503,
                )
        self._set_state(manifest, "scanning", "running")
        accepted = True
        if checker is not None:
            try:
                result = checker(content, manifest)
                accepted = bool(result[0] if isinstance(result, tuple) else result)
                reason = str(result[1] or "")[:200] if isinstance(result, tuple) and len(result) > 1 else ""
            except Exception as exc:  # noqa: BLE001 - scanner failures fail closed
                accepted = False
                reason = type(exc).__name__
            else:
                reason = ""
        else:
            reason = ""
        if not accepted:
            self._set_state(manifest, "rejected", "failed", reason=reason or "scan_rejected")
            raise ArtifactBrokerError("artifact scanner rejected the payload", code="scan_rejected", status_code=422)
        # Scanning and commit are separate durable operations.  This prevents a
        # scanner result from being confused with authorization to expose the
        # object to a Runner or user.
        return self._set_state(manifest, "scanning", "clean")

    def scan_and_commit(
        self,
        artifact_id: str,
        *,
        principal_id: str | int | None = None,
        department_id: str | int | None = None,
        task_id: str | None = None,
    ) -> ArtifactManifest:
        """Convenience operation for callers that explicitly want both steps."""

        self.scan(
            artifact_id,
            principal_id=principal_id,
            department_id=department_id,
            task_id=task_id,
        )
        return self.commit(
            artifact_id,
            principal_id=principal_id,
            department_id=department_id,
            task_id=task_id,
        )

    def commit(
        self,
        artifact_id: str,
        *,
        principal_id: str | int | None = None,
        department_id: str | int | None = None,
        task_id: str | None = None,
    ) -> ArtifactManifest:
        manifest = self.require(artifact_id)
        self._authorize(
            manifest,
            principal_id=principal_id,
            department_id=department_id,
            task_id=task_id,
            allow_department=True,
        )
        if manifest.state in {"committed", "consumed"}:
            return manifest
        if manifest.state != "scanning" or manifest.scan_status != "clean":
            raise ArtifactStateConflict("artifact must pass scanning before commit")
        return self._set_state(manifest, "committed", "clean")

    def _set_state(
        self,
        manifest: ArtifactManifest,
        state: str,
        scan_status: str,
        *,
        reason: str = "",
    ) -> ArtifactManifest:
        now = _iso()
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE sandbox_artifacts SET state = ?, scan_status = ?, updated_at = ?, "
                "metadata_json = CASE WHEN ? <> '' THEN json_set(metadata_json, '$.scan_reason', ?) ELSE metadata_json END "
                "WHERE artifact_id = ? AND state IN ('staged','scanning')",
                (state, scan_status, now, str(reason or ""), str(reason or "")[:200], manifest.artifact_id),
            )
            if int(cursor.rowcount or 0) == 0:
                current = self.get(manifest.artifact_id)
                if current is not None and current.state == state:
                    return current
                raise ArtifactStateConflict("artifact state changed concurrently")
        return self.require(manifest.artifact_id)

    def read(
        self,
        artifact_id: str,
        *,
        principal_id: str | int | None = None,
        department_id: str | int | None = None,
        task_id: str | None = None,
        mark_consumed: bool = False,
    ) -> bytes:
        manifest = self.require(artifact_id)
        self._authorize(
            manifest,
            principal_id=principal_id,
            department_id=department_id,
            task_id=task_id,
            allow_department=True,
        )
        if manifest.state not in {"committed", "consumed"}:
            raise ArtifactStateConflict("artifact is not available for reading")
        path = self._storage_path(manifest)
        try:
            with open(path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            raise ArtifactBrokerError("artifact payload is unavailable", code="missing", status_code=410) from exc
        try:
            valid_digest = validate_digest(manifest.sha256, content)
        except ArtifactRejected as exc:
            raise ArtifactBrokerError("artifact payload failed integrity check", code=exc.reason, status_code=422) from exc
        if len(content) != manifest.size or valid_digest != manifest.sha256:
            raise ArtifactBrokerError("artifact payload failed integrity check", code="hash", status_code=422)
        if mark_consumed and manifest.state == "committed":
            now = _iso()
            with closing(self._connect()) as conn:
                conn.execute(
                    "UPDATE sandbox_artifacts SET state = 'consumed', consumed_at = ?, updated_at = ? "
                    "WHERE artifact_id = ? AND state = 'committed'",
                    (now, now, manifest.artifact_id),
                )
        return content

    read_bytes = read

    def expire(self, *, now: datetime | None = None, artifact_ids: list[str] | None = None, limit: int = 1000) -> list[str]:
        current_time = now or _now()
        params: list[Any] = [current_time.isoformat()]
        where = "state IN ('staged','scanning','committed','consumed') AND expires_at <> '' AND expires_at <= ?"
        if artifact_ids:
            wanted = [str(item) for item in artifact_ids]
            where += " AND artifact_id IN (" + ",".join("?" for _ in wanted) + ")"
            params.extend(wanted)
        params.append(max(1, min(int(limit), 5000)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM sandbox_artifacts WHERE {where} ORDER BY expires_at LIMIT ?", params
            ).fetchall()
            expired: list[str] = []
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    artifact_id = str(row["artifact_id"])
                    manifest = self._row_to_manifest(row)
                    try:
                        os.unlink(self._storage_path(manifest))
                    except (OSError, ArtifactBrokerError):
                        pass
                    conn.execute(
                        "UPDATE sandbox_artifacts SET state = 'deleted', scan_status = 'expired', updated_at = ? "
                        "WHERE artifact_id = ? AND state <> 'deleted'",
                        (_iso(), artifact_id),
                    )
                    expired.append(artifact_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return expired

    def delete(self, artifact_id: str, *, principal_id: str | int | None = None, department_id: str | int | None = None, task_id: str | None = None) -> ArtifactManifest:
        manifest = self.require(artifact_id)
        self._authorize(manifest, principal_id=principal_id, department_id=department_id, task_id=task_id, allow_department=True)
        try:
            os.unlink(self._storage_path(manifest))
        except (OSError, ArtifactBrokerError):
            pass
        return self._set_deleted(manifest)

    def _set_deleted(self, manifest: ArtifactManifest) -> ArtifactManifest:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE sandbox_artifacts SET state = 'deleted', scan_status = 'deleted', updated_at = ? WHERE artifact_id = ?",
                (_iso(), manifest.artifact_id),
            )
        return self.require(manifest.artifact_id)


__all__ = [
    "ArtifactAccessDenied",
    "ArtifactBroker",
    "ArtifactBrokerError",
    "ArtifactNotFound",
    "ArtifactStateConflict",
]
