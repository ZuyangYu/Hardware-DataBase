"""Low-priority, single-writer Memory Worker.

The worker consumes durable reflection jobs and projection/deletion outboxes.
Every catalog mutation is fenced by the claimed job generation and lease
token; Store writes are rebuildable side effects and never become the source
of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import closing
from typing import Any, Iterable

import config.settings as settings

from src.memory.catalog import (
    MemoryCatalogRepository,
    ensure_memory_schema,
    get_job,
    json_loads,
    namespace_for_project,
    namespace_for_user,
    scope_fingerprint,
    utc_now,
)
from src.memory.jobs import MemoryJobRepository, _dead_letter_alert
from src.memory.manager import (
    ExtractedMemory,
    LangMemAdapter,
    MemoryExtractionError,
    MemoryExtractionOutput,
)
from src.memory.retention import expire_memory_records
from src.memory.reconcile import reconcile_store
from src.memory.schemas import ProjectMemory, UserMemory, content_hash
from src.memory.service import message_content_hash
from src.memory.store import MemoryStoreRuntime, create_memory_store
from src.observability import observe
from src.observability.metrics import counter, histogram, record_worker, set_memory_projection_state


class StaleMemoryJob(RuntimeError):
    """The lease, consent generation, source window, or scope changed."""


class InvalidMemorySource(PermissionError):
    """The durable source no longer satisfies the authorization contract."""


def _ready_clause(now: str) -> tuple[str, tuple[str, str]]:
    return (
        "status IN ('pending', 'retrying') AND (next_retry_at IS NULL OR next_retry_at <= ?) AND available_at <= ?",
        (now, now),
    )


class MemoryWorker:
    def __init__(
        self,
        *,
        db_path: str | None = None,
        worker_id: str | None = None,
        jobs: MemoryJobRepository | None = None,
        catalog: MemoryCatalogRepository | None = None,
        runtime: MemoryStoreRuntime | None = None,
        adapter: LangMemAdapter | None = None,
        settings_module=None,
    ):
        self.settings = settings_module or settings
        self.db_path = db_path or self.settings.AUTH_DB_PATH
        self.worker_id = worker_id or f"memory-worker-{uuid.uuid4().hex}"
        self.jobs = jobs or MemoryJobRepository(self.db_path)
        self.catalog = catalog or MemoryCatalogRepository(self.db_path)
        self.runtime = runtime
        self.adapter = adapter
        self.running = True
        self._last_retention_at = 0.0
        self._last_reconcile_at = 0.0
        self._last_reflection_started_at = 0.0
        self._reflection_failures = 0
        self._reflection_circuit_open_until = 0.0
        self._writer_lock_fd: int | None = None
        self._writer_lock_path = ""
        self._acquire_single_writer_lock()
        # A timed-out provider call cannot be force-killed safely in Python.
        # Keep the single reflection slot occupied until its provider thread
        # actually finishes, so a late call cannot share the adapter capture
        # state with the next job.
        self._reflection_in_flight = threading.Event()
        try:
            self._requeue_stuck_outbox()
        except Exception:
            self._release_single_writer_lock()
            raise

    def stop(self, *_args) -> None:
        self.running = False

    def close(self) -> None:
        """Close process-owned Store/Catalog resources during shutdown."""
        try:
            if self.runtime is not None:
                self.runtime.close()
                self.runtime = None
            close_catalog = getattr(self.catalog, "close", None)
            if callable(close_catalog):
                close_catalog()
        finally:
            self._release_single_writer_lock()

    def _acquire_single_writer_lock(self) -> None:
        """Enforce Phase 1's one-process Store writer contract.

        The normal settings module explicitly opts into this guard.  Small
        injected test settings may omit the flag so they can construct
        multiple isolated workers without sharing a production lock file.
        Deployments that intentionally move to HA must set the flag false only
        after the distributed lease/fence migration is complete.
        """
        enabled = getattr(self.settings, "MEMORY_SINGLE_WRITER", None)
        if enabled is not True:
            return
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - Linux is the Phase 1 target
            raise RuntimeError("MEMORY_SINGLE_WRITER requires a platform file-lock implementation") from exc
        lock_dir = os.path.dirname(os.path.abspath(self.db_path)) or os.getcwd()
        os.makedirs(lock_dir, exist_ok=True)
        self._writer_lock_path = os.path.join(lock_dir, "memory-worker.lock")
        fd = os.open(self._writer_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("another MemoryWorker owns the Phase 1 single-writer lock") from exc
        self._writer_lock_fd = fd

    def _release_single_writer_lock(self) -> None:
        fd = self._writer_lock_fd
        if fd is None:
            return
        self._writer_lock_fd = None
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _ensure_runtime(self) -> MemoryStoreRuntime:
        if self.runtime is None:
            self.runtime = create_memory_store(self.settings)
        return self.runtime

    def _requeue_stuck_outbox(self) -> None:
        # Projection/deletion outboxes have no external process lease; a new
        # single writer can safely retry operations left in running after a
        # crash. Idempotency keys and fence checks handle duplicate effects.
        with closing(self.jobs._connect()) as conn:
            conn.execute("UPDATE memory_projection_outbox SET status = 'retrying', next_retry_at = ? WHERE status = 'running'", (utc_now(),))
            conn.execute("UPDATE memory_deletion_outbox SET status = 'retrying', next_retry_at = ? WHERE status = 'running'", (utc_now(),))

    def _record_outbox_state(self) -> None:
        """Export bounded queue depth/lag without exposing operation IDs."""
        try:
            with closing(self.jobs._connect()) as conn:
                row = conn.execute(
                    """SELECT COUNT(*) AS pending, MIN(created_at) AS oldest
                       FROM (
                           SELECT created_at FROM memory_projection_outbox
                           WHERE status IN ('pending', 'retrying', 'running')
                           UNION ALL
                           SELECT created_at FROM memory_deletion_outbox
                           WHERE status IN ('pending', 'retrying', 'running')
                       )"""
                ).fetchone()
            oldest_age = 0.0
            if row is not None and row["oldest"]:
                from datetime import datetime, timezone

                created = datetime.fromisoformat(str(row["oldest"]))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                oldest_age = max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
            set_memory_projection_state(
                pending=int(row["pending"] or 0) if row is not None else 0,
                oldest_age_s=oldest_age,
            )
        except Exception:
            # Queue gauges are advisory and must never block the worker.
            pass

    def _claim_outbox(self, table: str) -> dict[str, Any] | None:
        if table not in {"memory_projection_outbox", "memory_deletion_outbox"}:
            raise ValueError("invalid memory outbox table")
        now = utc_now()
        with closing(self.jobs._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE status IN ('pending', 'retrying') AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY created_at, operation_id LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    conn.commit()
                    self._record_outbox_state()
                    return None
                conn.execute(
                    f"UPDATE {table} SET status = 'running', last_error = '' WHERE operation_id = ? AND status IN ('pending', 'retrying')",
                    (row["operation_id"],),
                )
                claimed = dict(conn.execute(f"SELECT * FROM {table} WHERE operation_id = ?", (row["operation_id"],)).fetchone())
                conn.commit()
                self._record_outbox_state()
                return claimed
            except Exception:
                conn.rollback()
                raise

    def _outbox_retry(self, table: str, operation_id: str, error: str) -> None:
        max_retries = max(1, int(getattr(self.settings, "MEMORY_PROJECTION_MAX_RETRIES", 5)))
        with closing(self.jobs._connect()) as conn:
            row = conn.execute(f"SELECT retry_count FROM {table} WHERE operation_id = ?", (operation_id,)).fetchone()
            if row is None:
                return
            retry_count = int(row["retry_count"] or 0) + 1
            terminal = retry_count >= max_retries
            conn.execute(
                f"UPDATE {table} SET status = ?, retry_count = ?, next_retry_at = ?, last_error = ? WHERE operation_id = ? AND status = 'running'",
                ("dead_letter" if terminal else "retrying", retry_count, None if terminal else self._retry_time(retry_count), str(error)[:2000], operation_id),
            )
            queue = "deletion" if table == "memory_deletion_outbox" else "projection"
            counter("hdb.memory.outbox_failed", attributes={"queue": queue})
            if terminal:
                counter("hdb.memory.outbox_dead_letter", attributes={"queue": queue})
                _dead_letter_alert(
                    {
                        "queue": f"outbox:{queue}",
                        "operation_id": str(operation_id),
                        "attempts": retry_count,
                        "reason": str(error)[:200],
                    }
                )

    def _deletion_target_matches(self, item: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
        """Validate that a delete still targets the retired projection it staged."""
        namespace = tuple(str(part) for part in payload.get("namespace") or ())
        key = str(payload.get("store_key") or "")
        if not namespace or not key:
            return False, "missing_delete_target"
        projection_id = str(payload.get("projection_id") or "")
        with closing(self.jobs._connect()) as conn:
            if projection_id:
                rows = conn.execute(
                    "SELECT * FROM memory_projections WHERE projection_id = ?",
                    (projection_id,),
                ).fetchall()
            else:
                # Legacy outbox payloads predate projection_id.  Resolve the
                # exact retired row by its server-owned identity tuple and
                # fail closed if it is ambiguous or gone.
                rows = conn.execute(
                    "SELECT * FROM memory_projections WHERE memory_id = ?",
                    (item["memory_id"],),
                ).fetchall()
        matches = [
            row
            for row in rows
            if row["memory_id"] == item["memory_id"]
            and row["store_backend"] == "sqlite"
            and tuple(str(part) for part in json_loads(row["namespace_json"], [])) == namespace
            and str(row["store_key"]) == key
        ]
        if len(matches) != 1:
            return False, "projection_identity_changed"
        projection = matches[0]
        if projection["retired_at"] is None:
            return False, "projection_not_retired"
        if int(projection["current_revision"]) != int(item["expected_revision"]):
            return False, "projection_revision_changed"
        if int(projection["fence_version"]) != int(item["expected_fence"]):
            return False, "projection_fence_changed"
        return True, ""

    def _complete_deletion_outbox(self, item: dict[str, Any], *, reason: str) -> None:
        with closing(self.jobs._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                completed = self.catalog.mark_projection_deleted(item["operation_id"], conn=conn)
                if completed:
                    self.catalog.audit(
                        "projection_delete_completed",
                        memory_id=item["memory_id"],
                        operation_id=item["operation_id"],
                        actor_id=self.worker_id,
                        metadata={"reason": reason},
                        conn=conn,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _retry_time(retry_count: int) -> str:
        delay = min(3600, max(5, 2 ** min(max(0, int(retry_count)), 8)))
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

    def _process_deletion_outbox(self) -> bool:
        item = self._claim_outbox("memory_deletion_outbox")
        if item is None:
            return False
        started = time.monotonic()
        observation = observe.chain("hdb.memory.outbox", operation="delete_projection")
        observation.start()
        status = "success"
        try:
            payload = json_loads(item["payload_json"], {})
            namespace = tuple(str(part) for part in payload.get("namespace") or ())
            key = str(payload.get("store_key") or "")
            valid_target, skip_reason = self._deletion_target_matches(item, payload)
            if valid_target:
                runtime = self._ensure_runtime()
                with observe.chain("hdb.memory.delete", operation="delete_projection") as delete_observation:
                    delete_observation.set("hdb.memory.delete.key_present", bool(key))
                    runtime.delete(namespace, key)
                    delete_observation.outcome("success")
                self._complete_deletion_outbox(item, reason=str(payload.get("reason") or ""))
            else:
                # A stale delete is a successful no-op.  Never call
                # Store.delete after the Catalog identity/fence has changed.
                self._complete_deletion_outbox(item, reason=f"skipped:{skip_reason}")
                counter("hdb.memory.deletion_outbox_stale", attributes={"reason": skip_reason})
            counter("hdb.memory.deletion_outbox_replayed")
            record_worker(status="success", duration_s=time.monotonic() - started)
            return True
        except Exception as exc:
            status = "failed"
            observation.error(exc)
            self._outbox_retry("memory_deletion_outbox", item["operation_id"], str(exc))
            record_worker(status="failed", duration_s=time.monotonic() - started)
            return True
        finally:
            observation.outcome(status)
            observation.end()

    def _process_projection_outbox(self) -> bool:
        item = self._claim_outbox("memory_projection_outbox")
        if item is None:
            return False
        started = time.monotonic()
        observation = observe.chain("hdb.memory.projection", operation="put")
        observation.start()
        status = "success"
        try:
            runtime = self._ensure_runtime()
            payload = json_loads(item["payload_json"], {})
            projection = self.catalog.get_projection(item["projection_id"])
            if projection is None:
                self._complete_projection_outbox(item["operation_id"], "orphan_projection")
                return True
            record = self.catalog.get_record(item["memory_id"])
            if record is None:
                self._complete_projection_outbox(item["operation_id"], "orphan_record")
                return True
            if projection.current_revision != int(item["expected_revision"]) or projection.fence_version != int(item["expected_fence"]):
                self._complete_projection_outbox(item["operation_id"], "stale_fence")
                return True
            if projection.retired_at is not None:
                self._complete_projection_outbox(item["operation_id"], "retired_projection")
                return True
            semantic = payload.get("content")
            payload_hash = str(payload.get("content_hash") or "")
            if not isinstance(semantic, dict) or content_hash(semantic, schema_version=str(payload.get("schema_version") or record.schema_version)) != payload_hash:
                self._complete_projection_outbox(item["operation_id"], "invalid_payload")
                return True
            if content_hash(semantic, schema_version=str(payload.get("schema_version") or record.schema_version)) != projection.current_content_hash:
                self._complete_projection_outbox(item["operation_id"], "stale_content_hash")
                return True
            physical_value = {
                "kind": str(payload.get("kind") or record.memory_type),
                "content": semantic,
                "schema_version": str(payload.get("schema_version") or record.schema_version),
            }
            runtime.put(projection.namespace, projection.store_key, physical_value)
            with closing(self.jobs._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                active = self.catalog.mark_projection_active(
                    item["operation_id"],
                    projection_id=projection.projection_id,
                    expected_revision=int(item["expected_revision"]),
                    expected_fence=int(item["expected_fence"]),
                    content_hash_value=projection.current_content_hash,
                    conn=conn,
                )
                if not active:
                    # A governance/rebuild fence changed while the physical
                    # write was in flight. The Catalog remains fail-closed;
                    # the next deletion outbox removes this stale object.
                    conn.rollback()
                    self._complete_projection_outbox(item["operation_id"], "catalog_fence_changed")
                    return True
                conn.commit()
            counter("hdb.memory.projection_outbox_replayed")
            record_worker(status="success", duration_s=time.monotonic() - started)
            return True
        except Exception as exc:
            status = "failed"
            observation.error(exc)
            self._outbox_retry("memory_projection_outbox", item["operation_id"], str(exc))
            record_worker(status="failed", duration_s=time.monotonic() - started)
            return True
        finally:
            observation.outcome(status)
            observation.end()

    def _complete_projection_outbox(self, operation_id: str, reason: str) -> None:
        with closing(self.jobs._connect()) as conn:
            conn.execute(
                "UPDATE memory_projection_outbox SET status = 'completed', completed_at = ?, last_error = ? WHERE operation_id = ? AND status IN ('pending', 'running', 'retrying')",
                (utc_now(), reason[:500], operation_id),
            )

    # ------------------------------------------------------------------
    # Reflection source snapshots

    def _load_project_messages(self, job) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with closing(self.jobs._connect()) as conn:
            session = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (job.session_id,)).fetchone()
            if session is None:
                raise StaleMemoryJob("conversation session no longer exists")
            if session["department_id"] in (None, "") or session["kb_id"] in (None, ""):
                raise StaleMemoryJob("project memory scope is incomplete")
            expected_scope = scope_fingerprint(scope="project", department_id=session["department_id"], kb_id=session["kb_id"])
            if expected_scope != job.scope_fingerprint:
                raise StaleMemoryJob("project scope changed")
            rows = conn.execute(
                """SELECT DISTINCT m.id, m.session_id, m.role, m.content, m.created_at,
                    t.id AS turn_id
                    FROM chat_messages m
                    JOIN chat_turns t ON t.session_id = m.session_id
                      AND (t.user_message_id = m.id OR t.assistant_message_id = m.id)
                      AND t.status = 'completed'
                    WHERE m.session_id = ? AND m.id <= ?
                      AND (? IS NULL OR m.id >= ?)
                    ORDER BY m.id""",
                (job.session_id, job.target_message_id, job.source_start_message_id, job.source_start_message_id),
            ).fetchall()
        messages = self._rows_to_messages(rows)
        if not messages or int(messages[-1]["id"]) != int(job.target_message_id):
            raise StaleMemoryJob("no stable completed messages at project target")
        return dict(session), messages

    def _load_user_messages(self, job) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
        consent = self.jobs.validate_consent_for_job(job)
        if (
            consent.authorized_start_message_id != job.authorized_start_message_id
            or consent.authorized_end_message_id != job.authorized_end_message_id
            or job.target_message_id != consent.authorized_end_message_id
        ):
            raise InvalidMemorySource("user job target is outside the immutable consent boundary")
        with closing(self.jobs._connect()) as conn:
            session = conn.execute("SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?", (job.session_id, consent.user_id)).fetchone()
            if session is None:
                raise InvalidMemorySource("consent session is unavailable")
            messages: list[dict[str, Any]] = []
            for manifest_item in consent.manifest:
                row = conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content, m.created_at, t.id AS turn_id
                       FROM chat_messages m
                       JOIN chat_turns t ON t.session_id = m.session_id
                         AND (t.user_message_id = m.id OR t.assistant_message_id = m.id)
                         AND t.status = 'completed'
                       WHERE m.session_id = ? AND m.id = ?""",
                    (job.session_id, int(manifest_item["message_id"])),
                ).fetchone()
                if row is None:
                    raise InvalidMemorySource("consent source message is missing")
                current = message_content_hash(row["role"], row["content"])
                if str(row["turn_id"]) != str(manifest_item["turn_id"]) or row["role"] != manifest_item["role"] or current != manifest_item["content_hash"]:
                    raise InvalidMemorySource("consent source manifest hash or boundary changed")
                message = dict(row)
                message["content_hash"] = current
                messages.append(message)
        if not messages or messages[-1]["id"] != job.authorized_end_message_id:
            raise InvalidMemorySource("consent end boundary is invalid")
        return dict(session), messages, consent

    @staticmethod
    def _rows_to_messages(rows: Iterable[Any]) -> list[dict[str, Any]]:
        messages = []
        for row in rows:
            value = dict(row)
            value["content_hash"] = message_content_hash(value["role"], value["content"])
            messages.append(value)
        return messages

    @staticmethod
    def _snapshot_hash(messages: list[dict[str, Any]]) -> str:
        payload = [
            {
                "turn_id": str(item.get("turn_id") or ""),
                "message_id": int(item["id"]),
                "role": str(item["role"]),
                "content_hash": str(item.get("content_hash") or ""),
            }
            for item in messages
        ]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _run_key(self, job, source_hash: str) -> str:
        payload = {
            "job_kind": job.job_kind,
            "consent_event_id": job.consent_event_id,
            "scope_fingerprint": job.scope_fingerprint,
            "target_turn_id": job.target_turn_id,
            "target_message_id": job.target_message_id,
            "generation": job.generation,
            "authorized_source_hash": job.authorized_source_hash,
            "authorized_start_message_id": job.authorized_start_message_id,
            "authorized_end_message_id": job.authorized_end_message_id,
            "source_snapshot_hash": source_hash,
            "schema_version": "1",
            "extractor_version": "langmem-0.0.30-hdb-v1",
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _scope_from_session(session: dict[str, Any], job, *, user: bool) -> tuple[str, ...]:
        if user:
            return namespace_for_user(session["user_id"], "candidate")
        return namespace_for_project(session["department_id"], session["kb_id"], "candidate")

    def _normalize_output(self, raw: Any, *, user: bool) -> list[dict[str, Any]]:
        if isinstance(raw, MemoryExtractionOutput):
            values = list(raw.items)
        elif isinstance(raw, list):
            values = []
            for item in raw:
                if isinstance(item, ExtractedMemory):
                    values.append(item)
                elif isinstance(item, dict):
                    semantic = item.get("semantic") or item.get("content") or item
                    if not isinstance(semantic, dict):
                        continue
                    values.append(ExtractedMemory(semantic=semantic, output_key=item.get("output_key") or item.get("key"), output_item_hash=""))
        else:
            values = []
        normalized: list[dict[str, Any]] = []
        schema = UserMemory if user else ProjectMemory
        for item in values:
            try:
                semantic = schema.model_validate(item.semantic).model_dump(mode="json")
            except Exception:
                continue
            normalized.append(
                {
                    "semantic": semantic,
                    "output_key": str(item.output_key) if item.output_key not in (None, "", "default") else None,
                    "output_item_hash": content_hash(semantic),
                }
            )
        return normalized

    def _output_for_run(self, job, run: Any, messages: list[dict[str, Any]], *, scope: tuple[str, ...], user: bool) -> list[dict[str, Any]]:
        if run["output_payload_json"]:
            payload = json_loads(run["output_payload_json"], [])
            return payload if isinstance(payload, list) else []
        if self.adapter is None:
            runtime = self._ensure_runtime()
            self.adapter = LangMemAdapter(runtime, self.catalog, settings=self.settings)
        output = self._extract_with_lease(job, messages, scope=scope, user=user)
        normalized = self._normalize_output(output, user=user)
        if not self.jobs.persist_output_for_job(
            job=job,
            run_id=run["run_id"],
            output=normalized,
        ):
            raise StaleMemoryJob("memory job consent or generation changed before output persistence")
        return normalized

    def _extract_with_lease(
        self,
        job,
        messages: list[dict[str, Any]],
        *,
        scope: tuple[str, ...],
        user: bool,
    ) -> MemoryExtractionOutput:
        """Run one reflection with a hard wall-clock budget and heartbeats."""

        lease_seconds = max(10, int(getattr(self.settings, "MEMORY_JOB_LEASE_SECONDS", 180)))
        timeout_seconds = max(1, int(getattr(self.settings, "MEMORY_REFLECTION_TIMEOUT_SECONDS", 120)))
        heartbeat_interval = max(1.0, min(30.0, lease_seconds / 3.0))
        stopped = threading.Event()
        observation = observe.chain("hdb.memory.extract", operation="extract", job_kind=job.job_kind)
        observation.start()
        extract_error: BaseException | None = None

        def heartbeat_loop() -> None:
            while not stopped.wait(heartbeat_interval):
                if not self.jobs.heartbeat(
                    job.job_id,
                    job.lease_token or "",
                    job.generation,
                    lease_seconds=lease_seconds,
                ):
                    return

        heartbeat = threading.Thread(
            target=heartbeat_loop,
            name=f"{self.worker_id}-lease",
            daemon=True,
        )
        heartbeat.start()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{self.worker_id}-reflection")
        timed_out = False
        extract_started = time.monotonic()
        try:
            self._last_reflection_started_at = extract_started
            self._reflection_in_flight.set()
            histogram(
                "hdb.memory.extract_tokens",
                len(json.dumps(messages, ensure_ascii=False)) / 4.0,
                attributes={"job_kind": job.job_kind},
                unit="{tokens}",
            )
            future = executor.submit(self.adapter.extract, messages, scope=scope, user=user)
            future.add_done_callback(lambda _future: self._reflection_in_flight.clear())
            try:
                return future.result(timeout=timeout_seconds)
            except FutureTimeoutError as exc:
                timed_out = True
                counter("hdb.memory.reflection_timeout")
                extract_error = exc
                observation.error(exc)
                future.cancel()
                raise MemoryExtractionError(
                    f"memory reflection exceeded {timeout_seconds}s timeout"
                ) from exc
            except Exception as exc:
                extract_error = exc
                observation.error(exc)
                raise
        finally:
            stopped.set()
            heartbeat.join(timeout=1.0)
            # A provider call cannot be forcefully killed in Python.  Do not
            # block the worker on a timed-out call; the job lease/generation
            # CAS still prevents its late result from committing.
            executor.shutdown(wait=not timed_out, cancel_futures=True)
            if not timed_out:
                self._reflection_in_flight.clear()
            histogram(
                "hdb.memory.extract_latency_ms",
                (time.monotonic() - extract_started) * 1000,
                attributes={"job_kind": job.job_kind},
                unit="ms",
            )
            observation.outcome("failed" if extract_error is not None else "success")
            observation.end()

    def _apply_job(self, job, session: dict[str, Any], messages: list[dict[str, Any]], *, source_hash: str, run_key: str, user: bool) -> bool:
        scope = self._scope_from_session(session, job, user=user)
        with closing(self.jobs._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = get_job(conn, job.job_id)
                if current is None or current.status != "running" or current.lease_token != job.lease_token or current.generation != job.generation:
                    raise StaleMemoryJob("memory job lease or generation changed")
                if user:
                    # Re-open consent in the apply transaction, so a revoke
                    # racing model output invalidates the whole Catalog write.
                    consent = self.jobs.validate_consent_for_job(current, conn=conn)
                    settings_row = self.jobs.get_user_settings(consent.user_id, conn=conn)
                    if not settings_row.opt_in or settings_row.revoke_generation != current.consent_revoke_generation:
                        raise StaleMemoryJob("user memory consent changed before apply")
                run = conn.execute("SELECT * FROM memory_reflection_runs WHERE run_key = ?", (run_key,)).fetchone()
                if run is None or not run["output_payload_json"]:
                    raise StaleMemoryJob("reflection output was not durably persisted")
                output = json_loads(run["output_payload_json"], [])
                if not isinstance(output, list):
                    output = []
                calculated_output_hash = hashlib.sha256(
                    json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if run["output_hash"] and calculated_output_hash != run["output_hash"]:
                    raise MemoryExtractionError("durable reflection output hash mismatch")
                output_schema = UserMemory if user else ProjectMemory
                existing_items = {
                    int(row["item_ordinal"]): row
                    for row in conn.execute("SELECT * FROM memory_run_items WHERE run_id = ?", (run["run_id"],)).fetchall()
                }
                source_refs = [
                    {
                        "session_id": int(message["session_id"]),
                        "turn_id": str(message["turn_id"]),
                        "message_id": int(message["id"]),
                        "source_hash": str(message["content_hash"]),
                        "source_role": str(message["role"]),
                        "contribution_kind": "user_consent" if user else "project_reflection",
                        "consent_event_id": current.consent_event_id if user else None,
                    }
                    for message in messages
                ]
                for ordinal, item in enumerate(output):
                    if not isinstance(item, dict) or not isinstance(item.get("semantic"), dict):
                        continue
                    try:
                        semantic = output_schema.model_validate(item["semantic"]).model_dump(mode="json")
                    except Exception as exc:
                        raise MemoryExtractionError("durable reflection output does not match the semantic schema") from exc
                    item_hash = content_hash(semantic)
                    if item.get("output_item_hash") and str(item["output_item_hash"]) != item_hash:
                        raise MemoryExtractionError("durable reflection item hash mismatch")
                    previous = existing_items.get(ordinal)
                    if previous is not None and previous["status"] == "catalog_committed" and previous["memory_id"]:
                        continue
                    memory_id = previous["memory_id"] if previous is not None else None
                    projection_key = item.get("output_key")
                    if not memory_id and projection_key:
                        projection = self.catalog.get_projection_by_key("sqlite", scope, str(projection_key), conn=conn)
                        if projection is not None:
                            projection_record = self.catalog.get_record(projection.memory_id, conn=conn)
                            if (
                                projection_record is not None
                                and projection_record.scope == ("user" if user else "project")
                                and projection_record.status == "needs_rebuild"
                                and projection.projection_kind == "candidate"
                            ):
                                # A rebuild may intentionally reuse the
                                # logical memory while replacing its retired
                                # physical projection. Keep the memory ID but
                                # clear the stale key so Catalog allocates a
                                # revision-specific key below.
                                memory_id = projection_record.memory_id
                                projection_key = None
                            elif projection.manager_writable and projection.retired_at is None:
                                memory_id = projection.memory_id
                            else:
                                # Store keys are unique even after retirement.
                                # A stale/reused LangMem key must never
                                # collide with the old projection; let Catalog
                                # allocate a new server-owned key instead.
                                projection_key = None
                    expected_fence = None
                    if memory_id:
                        if projection_key:
                            current_projection = self.catalog.get_projection_by_key(
                                "sqlite", scope, str(projection_key), conn=conn
                            )
                            expected_fence = (
                                current_projection.fence_version if current_projection is not None else None
                            )
                        else:
                            current_projection_row = conn.execute(
                                "SELECT fence_version FROM memory_projections WHERE memory_id = ? AND projection_kind = 'candidate' AND retired_at IS NULL",
                                (memory_id,),
                            ).fetchone()
                            expected_fence = (
                                int(current_projection_row["fence_version"])
                                if current_projection_row is not None
                                else None
                            )
                    record, projection, _operation_id = self.catalog.prepare_candidate(
                        content=semantic,
                        scope="user" if user else "project",
                        user_id=session["user_id"] if user else None,
                        department_id=None if user else session["department_id"],
                        kb_id=None if user else session["kb_id"],
                        memory_id=memory_id,
                        projection_key=None if memory_id else (str(projection_key) if projection_key else None),
                        source_refs=source_refs,
                        actor_id=self.worker_id,
                        reason="user consent reflection" if user else "project reflection",
                        expected_fence=expected_fence,
                        run_id=run["run_id"],
                        conn=conn,
                    )
                    self.jobs.upsert_run_item(
                        run_id=run["run_id"],
                        item_ordinal=ordinal,
                        output_item_hash=item_hash,
                        planned_action="upsert_candidate",
                        memory_id=record.memory_id,
                        revision_no=record.current_revision,
                        projection_id=projection.projection_id,
                        expected_fence=projection.fence_version,
                        status="catalog_committed",
                        conn=conn,
                    )
                    counter(
                        "hdb.memory.created" if record.current_revision == 1 else "hdb.memory.updated",
                        attributes={"scope": record.scope},
                    )
                conn.execute("UPDATE memory_reflection_runs SET status = 'catalog_committed' WHERE run_id = ? AND status IN ('prepared', 'output_persisted', 'catalog_committed')", (run["run_id"],))
                self.jobs.ensure_checkpoint(job.job_id, conn=conn)
                checkpointed = self.jobs.update_checkpoint_in_connection(
                    conn,
                    job_id=job.job_id,
                    lease_token=job.lease_token or "",
                    generation=job.generation,
                    committed_turn_id=job.target_turn_id,
                    committed_message_id=job.target_message_id,
                    source_fingerprint=source_hash,
                )
                if not checkpointed:
                    raise StaleMemoryJob("memory checkpoint lease or generation CAS failed")
                completed = conn.execute(
                    """UPDATE memory_jobs SET status = 'completed', completed_at = ?, lease_owner = NULL,
                       lease_token = NULL, lease_expires_at = NULL
                       WHERE job_id = ? AND status = 'running' AND lease_token = ? AND generation = ?""",
                    (utc_now(), job.job_id, job.lease_token, int(job.generation)),
                )
                if completed.rowcount != 1:
                    raise StaleMemoryJob("memory job completion CAS failed")
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def _cancel_job(self, job, reason: str) -> None:
        with closing(self.jobs._connect()) as conn:
            conn.execute(
                "UPDATE memory_jobs SET status = 'cancelled', generation = generation + 1, last_error = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL WHERE job_id = ? AND status = 'running' AND lease_token = ? AND generation = ?",
                (str(reason)[:2000], job.job_id, job.lease_token, int(job.generation)),
            )

    def _process_job(self) -> bool:
        if self._reflection_in_flight.is_set():
            return False
        now = time.monotonic()
        min_interval = max(0.0, float(getattr(self.settings, "MEMORY_REFLECTION_MIN_INTERVAL_SECONDS", 0)))
        if now - self._last_reflection_started_at < min_interval:
            return False
        if now < self._reflection_circuit_open_until:
            return False
        if self._reflection_circuit_open_until:
            self._reflection_failures = 0
            self._reflection_circuit_open_until = 0.0
        job = self.jobs.claim_next(
            self.worker_id,
            lease_seconds=int(getattr(self.settings, "MEMORY_JOB_LEASE_SECONDS", 180)),
            max_retries=int(getattr(self.settings, "MEMORY_JOB_MAX_RETRIES", 5)),
        )
        if job is None:
            return False
        started = time.monotonic()
        observation = observe.chain("hdb.memory.job", job_kind=job.job_kind)
        observation.start()
        outcome = "success"
        try:
            user = job.job_kind == "user_reflection"
            if user:
                session, messages, _consent = self._load_user_messages(job)
            else:
                session, messages = self._load_project_messages(job)
            source_hash = self._snapshot_hash(messages)
            run_key = self._run_key(job, source_hash)
            with closing(self.jobs._connect()) as conn:
                ensure_memory_schema(conn)
                run = conn.execute("SELECT * FROM memory_reflection_runs WHERE run_key = ?", (run_key,)).fetchone()
            if run is None:
                run = self.jobs.save_run(
                    job=job,
                    run_key=run_key,
                    source_snapshot_hash=source_hash,
                    source_snapshot_ref=f"memory://reflection/{run_key}",
                    extractor_model=str(getattr(self.settings, "MEMORY_MODEL", "") or getattr(self.settings, "AGENT_OLLAMA_MODEL", "")),
                    extractor_version="langmem-0.0.30-hdb-v1",
                    schema_version="1",
                )
            else:
                counter("hdb.memory.reflection_run_replayed", attributes={"job_kind": job.job_kind})
            reused_output = bool(run["output_payload_json"])
            if not reused_output:
                if user:
                    # A revoke may race the source reload while the model is
                    # being prepared.  Re-check immediately before any new
                    # output is generated and persisted.
                    self.jobs.validate_consent_for_job(job)
                self._output_for_run(job, run, messages, scope=self._scope_from_session(session, job, user=user), user=user)
                with closing(self.jobs._connect()) as conn:
                    run = conn.execute("SELECT * FROM memory_reflection_runs WHERE run_id = ?", (run["run_id"],)).fetchone()
            else:
                counter("hdb.memory.reflection_run_output_reused", attributes={"job_kind": job.job_kind})
            self._apply_job(job, session, messages, source_hash=source_hash, run_key=run_key, user=user)
            self._reflection_failures = 0
            self._reflection_circuit_open_until = 0.0
            record_worker(status="success", duration_s=time.monotonic() - started)
            return True
        except InvalidMemorySource as exc:
            outcome = "failed"
            self._cancel_job(job, str(exc))
            counter("hdb.memory.user_job_consent_denied")
            record_worker(status="failed", duration_s=time.monotonic() - started)
            return True
        except PermissionError as exc:
            # A revoked/invalid consent is terminal for this immutable User
            # Job; retrying it would only create noise and could re-read a
            # source that is no longer authorized.
            outcome = "failed"
            self._cancel_job(job, str(exc))
            counter("hdb.memory.user_job_consent_denied")
            record_worker(status="failed", duration_s=time.monotonic() - started)
            return True
        except StaleMemoryJob:
            # A new generation/revoke won the race; the old worker must not
            # retry or mark that newer job completed.
            record_worker(status="success", duration_s=time.monotonic() - started)
            observation.set("hdb.memory.job.outcome", "stale")
            return True
        except Exception as exc:
            outcome = "failed"
            self._reflection_failures += 1
            circuit_failures = max(1, int(getattr(self.settings, "MEMORY_REFLECTION_CIRCUIT_FAILURES", 5)))
            if self._reflection_failures >= circuit_failures:
                cooldown = max(1.0, float(getattr(self.settings, "MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS", 60)))
                self._reflection_circuit_open_until = time.monotonic() + cooldown
                counter("hdb.memory.reflection_circuit_open")
            self.jobs.mark_retry(
                job.job_id,
                job.lease_token or "",
                job.generation,
                str(exc),
                retry_after_seconds=30,
                max_retries=int(getattr(self.settings, "MEMORY_JOB_MAX_RETRIES", 5)),
            )
            record_worker(status="failed", duration_s=time.monotonic() - started)
            return True
        finally:
            observation.outcome(outcome)
            observation.end()

    def run_once(self) -> bool:
        if not bool(getattr(self.settings, "MEMORY_ENABLED", True)):
            return False
        retention_interval = max(
            30.0,
            float(getattr(self.settings, "MEMORY_RECONCILE_INTERVAL_SECONDS", 3600)),
        )
        if time.monotonic() - self._last_retention_at >= retention_interval:
            self._last_retention_at = time.monotonic()
            expired = expire_memory_records(
                self.db_path,
                retention_days=getattr(self.settings, "MEMORY_RETENTION_DAYS", ""),
            )
            if expired:
                counter("hdb.memory.retention_expired", value=expired)
                return True
        # Deletion/revocation always wins over new model work.
        if self._process_deletion_outbox():
            return True
        if self._process_projection_outbox():
            return True
        if time.monotonic() - self._last_reconcile_at >= retention_interval:
            self._last_reconcile_at = time.monotonic()
            try:
                removed = reconcile_store(
                    self._ensure_runtime(),
                    self.catalog,
                    max_scan=int(getattr(self.settings, "MEMORY_STORE_MAX_SCAN", 100)),
                )
                if removed:
                    counter("hdb.memory.reconcile_removed", value=removed)
                    return True
            except Exception as exc:
                # Reconciliation is a rebuild aid; it must not prevent a
                # durable job from being claimed or make Chat unavailable.
                counter("hdb.memory.reconcile_error", attributes={"error": str(exc)[:100]})
        return self._process_job()

    def run_forever(self) -> None:
        while self.running:
            if not self.run_once():
                time.sleep(max(0.1, float(getattr(self.settings, "WORKER_POLL_INTERVAL_SECONDS", 0.5))))


def main() -> None:
    from src.observability import init_observability, shutdown_observability

    init_observability(
        "hardware-database-memory-worker",
        service_version=settings.OBS_SERVICE_VERSION,
        environment=settings.OBS_ENVIRONMENT,
    )
    worker = MemoryWorker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    try:
        worker.run_forever()
    finally:
        worker.stop()
        worker.close()
        shutdown_observability()


__all__ = ["InvalidMemorySource", "MemoryWorker", "StaleMemoryJob", "main"]
