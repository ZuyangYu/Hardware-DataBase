from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
import logging
from pathlib import Path

from .collection_qc import run_collection_qc
from .dataset_loader import load_dataset
from .history import cohort_fingerprint
from .reporters import write_reports
from .schemas import (
    AnswerSnapshot,
    EvaluationRunState,
    EvaluationSample,
    EvaluationSummary,
)
from .service import new_run_id
from .snapshot_manifest import write_snapshot_manifest
from .snapshot_store import SnapshotStore
from src.observability import observe, thread_with_current_context
from src.observability.metrics import record_evaluation


_STATE_LOCK = threading.RLock()
_STATE_REPLACE_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)
_REPORT_ARTIFACT_NAMES = (
    "summary.json",
    "results.jsonl",
    "summary.csv",
    "report.html",
    "report_complete.json",
)
_DELETABLE_RUN_STATUSES = frozenset({"failed", "cancelled", "completed", "collected"})
_EXECUTION_DATASET_NAME = "execution_dataset.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _freeze_execution_dataset(
    dataset_path: str | Path,
    run_dir: str | Path,
    samples: list[EvaluationSample],
) -> tuple[str, str, str]:
    """Write the normalized sample list used by a run and return its hashes."""

    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    execution_path = run_path / _EXECUTION_DATASET_NAME
    execution_path.write_text(
        "".join(sample.model_dump_json() + "\n" for sample in samples),
        encoding="utf-8",
    )
    return (
        str(execution_path),
        _sha256_file(dataset_path),
        _sha256_file(execution_path),
    )


class RunStateStore:
    _lock = _STATE_LOCK

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def create(self, state: EvaluationRunState) -> EvaluationRunState:
        with self._lock:
            return self._write(state)

    def load(self) -> EvaluationRunState:
        with self._lock:
            return EvaluationRunState.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )

    def mutate(
        self, mutator: Callable[[EvaluationRunState], EvaluationRunState]
    ) -> EvaluationRunState:
        with self._lock:
            return self._write(mutator(self.load()))

    def request_pause(self) -> EvaluationRunState:
        return self.mutate(self._request_pause)

    def request_cancel(self) -> EvaluationRunState:
        return self.mutate(self._request_cancel)

    def mark_running(self, *, stage: str = "collecting") -> EvaluationRunState:
        return self.mutate(lambda state: self._mark_running(state, stage=stage))

    def begin_sample(self, sample: EvaluationSample) -> EvaluationRunState:
        return self.mutate(lambda state: self._begin_sample(state, sample))

    def mark_paused(self) -> EvaluationRunState:
        return self.mutate(self._mark_paused)

    def mark_cancelled(self) -> EvaluationRunState:
        return self.mutate(self._mark_cancelled)

    def mark_completed(self, *, report_path: str = "") -> EvaluationRunState:
        return self.mutate(
            lambda state: self._mark_completed(state, report_path=report_path)
        )

    def mark_collected(self, qc_verdict: str = "") -> EvaluationRunState:
        """Transition a finished online collection to the QC gate state."""

        return self.mutate(lambda state: self._mark_collected(state, qc_verdict))

    def request_scoring(self) -> EvaluationRunState:
        """Queue the scoring phase after a passing collection QC gate."""

        return self.mutate(self._request_scoring)

    def complete_report_or_handle_control(
        self,
        *,
        report_path: str,
    ) -> EvaluationRunState:
        """Publish a report only when no pause or cancel request won the race."""
        with self._lock:
            state = self.load()
            if state.status in {"queued", "running"}:
                return self._write(self._mark_completed(state, report_path=report_path))
            if state.status == "pause_requested":
                self.remove_report_artifacts()
                return self._write(self._mark_paused(state))
            if state.status == "cancel_requested":
                self.remove_report_artifacts()
                return self._write(self._mark_cancelled(state))
            raise ValueError(f"cannot finalize report from {state.status!r}")

    def mark_failed(self, error_message: str = "") -> EvaluationRunState:
        return self.mutate(lambda state: self._mark_failed(state, error_message))

    def update_scoring_progress(
        self,
        completed_groups: int,
        total_groups: int,
        *,
        completed_items: int | None = None,
        total_items: int | None = None,
    ) -> EvaluationRunState:
        updates = {
            "scoring_completed_groups": completed_groups,
            "scoring_total_groups": total_groups,
        }
        if completed_items is not None:
            updates["scoring_completed_items"] = completed_items
        if total_items is not None:
            updates["scoring_total_items"] = total_items
        return self.mutate(lambda state: self._with_update(state, **updates))

    def publish_partial_report(
        self,
        *,
        report_path: str,
        error_message: str = "",
    ) -> EvaluationRunState:
        with self._lock:
            state = self.load()
            if state.status == "pause_requested":
                return self._write(
                    self._finish(state, "paused", report_path=report_path)
                )
            if state.status == "cancel_requested":
                return self._write(
                    self._finish(state, "cancelled", report_path=report_path)
                )
            return self._write(
                self._finish(
                    state,
                    "failed",
                    report_path=report_path,
                    error_message=error_message
                    or "evaluation worker failed; see application logs",
                )
            )

    def remove_report_artifacts(self) -> None:
        with self._lock:
            self._remove_report_artifacts(
                tuple(
                    self.path.parent / f"{name}{suffix}"
                    for name in _REPORT_ARTIFACT_NAMES
                    for suffix in ("", ".tmp")
                )
            )

    def mark_orphaned_as_paused(self) -> EvaluationRunState:
        return self.mutate(self._mark_orphaned_as_paused)

    def _write(self, state: EvaluationRunState) -> EvaluationRunState:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                state.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            self._replace_temporary_file(temporary)
        finally:
            if temporary.exists():
                temporary.unlink()
        return state

    def _replace_temporary_file(self, temporary: Path) -> None:
        for delay in _STATE_REPLACE_RETRY_DELAYS_SECONDS:
            try:
                os.replace(temporary, self.path)
                return
            except PermissionError:
                time.sleep(delay)
        try:
            os.replace(temporary, self.path)
        except PermissionError:
            logging.getLogger(__name__).error(
                "evaluation run state remained locked after %.2f seconds: %s",
                sum(_STATE_REPLACE_RETRY_DELAYS_SECONDS),
                self.path,
            )
            raise

    @staticmethod
    def _remove_report_artifacts(report_artifacts: tuple[Path, ...]) -> None:
        for path in report_artifacts:
            path.unlink(missing_ok=True)

    @staticmethod
    def _with_update(
        state: EvaluationRunState, **updates: object
    ) -> EvaluationRunState:
        return state.model_copy(update={"updated_at": _now(), **updates})

    @classmethod
    def _request_pause(cls, state: EvaluationRunState) -> EvaluationRunState:
        if state.status not in {"queued", "running"}:
            raise ValueError(f"cannot request pause from {state.status!r}")
        return cls._with_update(state, status="pause_requested")

    @classmethod
    def _request_cancel(cls, state: EvaluationRunState) -> EvaluationRunState:
        if state.status == "collected":
            return cls._finish(state, "cancelled")
        if state.status in {"queued", "running", "pause_requested"}:
            return cls._with_update(state, status="cancel_requested")
        if state.status == "paused":
            return cls._finish(state, "cancelled")
        raise ValueError(f"cannot request cancellation from {state.status!r}")

    @classmethod
    def _mark_running(
        cls, state: EvaluationRunState, *, stage: str
    ) -> EvaluationRunState:
        if state.status != "queued":
            raise ValueError(f"cannot mark running from {state.status!r}")
        return cls._with_update(
            state,
            status="running",
            stage=stage,
            started_at=state.started_at or _now(),
        )

    @classmethod
    def _begin_sample(
        cls, state: EvaluationRunState, sample: EvaluationSample
    ) -> EvaluationRunState:
        if state.status == "pause_requested":
            return cls._mark_paused(state)
        if state.status == "cancel_requested":
            return cls._mark_cancelled(state)
        if state.status != "running":
            raise ValueError(f"cannot begin sample from {state.status!r}")
        return cls._with_update(
            state,
            stage="collecting",
            current_sample_id=sample.id,
            current_question=sample.question,
        )

    @classmethod
    def _mark_paused(cls, state: EvaluationRunState) -> EvaluationRunState:
        if state.status != "pause_requested":
            raise ValueError(f"cannot mark paused from {state.status!r}")
        return cls._with_update(state, status="paused", stage="idle")

    @classmethod
    def _mark_collected(
        cls, state: EvaluationRunState, qc_verdict: str = ""
    ) -> EvaluationRunState:
        if state.status not in {"running", "pause_requested", "cancel_requested"}:
            raise ValueError(f"cannot mark collected from {state.status!r}")
        updates: dict[str, object] = {
            "status": "collected",
            "stage": "collected",
            "current_sample_id": "",
            "current_question": "",
            "finished_at": _now(),
        }
        if qc_verdict:
            evaluation_config = dict(state.evaluation_config or {})
            evaluation_config["collection_qc_verdict"] = qc_verdict
            updates["evaluation_config"] = evaluation_config
        return cls._with_update(state, **updates)

    @classmethod
    def _request_scoring(cls, state: EvaluationRunState) -> EvaluationRunState:
        if state.status != "collected":
            raise ValueError(f"cannot start scoring from {state.status!r}")
        return cls._with_update(
            state,
            status="queued",
            stage="idle",
            finished_at="",
            error_message="",
        )

    @classmethod
    def _mark_cancelled(cls, state: EvaluationRunState) -> EvaluationRunState:
        if state.status not in {"cancel_requested", "paused", "collected"}:
            raise ValueError(f"cannot mark cancelled from {state.status!r}")
        return cls._finish(state, "cancelled")

    @classmethod
    def _mark_completed(
        cls, state: EvaluationRunState, *, report_path: str = ""
    ) -> EvaluationRunState:
        if state.status not in {"queued", "running"}:
            raise ValueError(f"cannot mark completed from {state.status!r}")
        return cls._finish(state, "completed", report_path=report_path)

    @classmethod
    def _mark_failed(
        cls, state: EvaluationRunState, error_message: str
    ) -> EvaluationRunState:
        if state.status not in {
            "queued",
            "running",
            "pause_requested",
            "cancel_requested",
        }:
            raise ValueError(f"cannot mark failed from {state.status!r}")
        return cls._finish(state, "failed", error_message=error_message)

    @classmethod
    def _finish(
        cls,
        state: EvaluationRunState,
        status: str,
        *,
        error_message: str | None = None,
        report_path: str | None = None,
    ) -> EvaluationRunState:
        updates: dict[str, object] = {
            "status": status,
            "stage": "idle",
            "current_sample_id": "",
            "current_question": "",
            "finished_at": _now(),
        }
        if error_message is not None:
            updates["error_message"] = error_message
        if report_path is not None:
            updates["report_path"] = report_path
        return cls._with_update(state, **updates)

    @classmethod
    def _mark_orphaned_as_paused(cls, state: EvaluationRunState) -> EvaluationRunState:
        if state.status in {"queued", "running", "pause_requested", "cancel_requested"}:
            return cls._with_update(state, status="paused", stage="idle")
        return state


def discover_run_dirs(root: str | Path) -> list[Path]:
    root_path = Path(root)
    if not root_path.exists():
        return []
    return sorted(
        (
            path
            for path in root_path.iterdir()
            if path.is_dir() and (path / "run_state.json").is_file()
        ),
        key=lambda path: path.name,
    )


class EvaluationRunController:
    def __init__(self, service_factory: Callable[[], object], state_root: str | Path):
        self.service_factory = service_factory
        self.state_root = Path(state_root)
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.RLock()

    def create_online_run(
        self,
        dataset_path: str | Path,
        output_root: str | Path,
        samples: list[EvaluationSample],
        *,
        sample_ids: set[str] | list[str] | None = None,
        tags: set[str] | list[str] | None = None,
        kb_id: int | None = None,
        kb_name: str = "",
        department_id: int | None = None,
        created_by: str = "",
        dataset_total_count: int | None = None,
        matched_sample_count: int | None = None,
        filtered_sample_count: int = 0,
        cohort_fingerprint_value: str = "",
        validation_warnings: list[str] | None = None,
        snapshot_ownership_verified: bool = False,
        evaluation_config: dict[str, object] | None = None,
    ) -> EvaluationRunState:
        run_id = new_run_id()
        run_dir = self.state_root / run_id
        execution_path, source_digest, execution_digest = _freeze_execution_dataset(
            dataset_path,
            run_dir,
            samples,
        )
        denied_count = sum(sample.expected_access == "denied" for sample in samples)
        metadata = {
            "kb_id": kb_id,
            "kb_name": kb_name,
            "department_id": department_id,
            "created_by": created_by,
            "source_dataset_path": str(dataset_path),
            "dataset_sha256": source_digest,
            "execution_dataset_sha256": execution_digest,
            "dataset_total_count": dataset_total_count
            if dataset_total_count is not None
            else len(samples),
            "dataset_sample_count": len(samples),
            "matched_sample_count": matched_sample_count
            if matched_sample_count is not None
            else len(samples),
            "filtered_sample_count": filtered_sample_count,
            "normal_sample_count": len(samples) - denied_count,
            "expected_denied_sample_count": denied_count,
            "cohort_fingerprint": cohort_fingerprint_value
            or cohort_fingerprint(sample.id for sample in samples),
            "snapshot_ownership_verified": snapshot_ownership_verified,
            "validation_warnings": list(validation_warnings or []),
            "evaluation_config": dict(evaluation_config or {}),
        }
        state = EvaluationRunState.new_online(
            run_id=run_id,
            dataset_path=execution_path,
            snapshot_path=str(Path(output_root) / run_id / "snapshot.jsonl"),
            total_samples=len(samples),
            sample_ids=sorted(sample_ids or []),
            tags=sorted(tags or []),
            metadata=metadata,
        )
        return self._store(run_id).create(state)

    def create_offline_run(
        self,
        dataset_path: str | Path,
        output_root: str | Path,
        samples: list[EvaluationSample],
        snapshot_path: str | Path,
        *,
        sample_ids: set[str] | list[str] | None = None,
        tags: set[str] | list[str] | None = None,
        kb_id: int | None = None,
        kb_name: str = "",
        department_id: int | None = None,
        created_by: str = "",
        dataset_total_count: int | None = None,
        matched_sample_count: int | None = None,
        filtered_sample_count: int = 0,
        cohort_fingerprint_value: str = "",
        validation_warnings: list[str] | None = None,
        snapshot_ownership_verified: bool = False,
        evaluation_config: dict[str, object] | None = None,
    ) -> EvaluationRunState:
        run_id = new_run_id()
        run_dir = self.state_root / run_id
        execution_path, source_digest, execution_digest = _freeze_execution_dataset(
            dataset_path,
            run_dir,
            samples,
        )
        denied_count = sum(sample.expected_access == "denied" for sample in samples)
        state = EvaluationRunState(
            run_id=run_id,
            dataset_path=execution_path,
            snapshot_path=str(snapshot_path),
            mode="offline",
            kb_id=kb_id,
            kb_name=kb_name,
            department_id=department_id,
            created_by=created_by,
            source_dataset_path=str(dataset_path),
            dataset_sha256=source_digest,
            execution_dataset_sha256=execution_digest,
            dataset_total_count=dataset_total_count
            if dataset_total_count is not None
            else len(samples),
            dataset_sample_count=len(samples),
            matched_sample_count=matched_sample_count
            if matched_sample_count is not None
            else len(samples),
            filtered_sample_count=filtered_sample_count,
            normal_sample_count=len(samples) - denied_count,
            expected_denied_sample_count=denied_count,
            cohort_fingerprint=cohort_fingerprint_value
            or cohort_fingerprint(sample.id for sample in samples),
            snapshot_ownership_verified=snapshot_ownership_verified,
            validation_warnings=list(validation_warnings or []),
            evaluation_config=dict(evaluation_config or {}),
            total_samples=len(samples),
            sample_ids=sorted(sample_ids or []),
            tags=sorted(tags or []),
        )
        stored = self._store(run_id).create(state)
        if stored.kb_id is not None and Path(stored.snapshot_path).is_file():
            self._record_snapshot_manifest(self._store(run_id))
            stored = self._store(run_id).load()
        return stored

    def start(self, run_id: str) -> threading.Thread:
        with self._threads_lock:
            existing = self._threads.get(run_id)
            if existing is not None and existing.is_alive():
                raise RuntimeError(f"evaluation run {run_id!r} is already running")
            thread = thread_with_current_context(
                self.execute,
                run_id,
                daemon=True,
                name=f"evaluation-{run_id[:16]}",
            )
            self._threads[run_id] = thread
            thread.start()
            return thread

    def _record_snapshot_manifest(self, store: RunStateStore) -> EvaluationRunState:
        state = store.load()
        if state.kb_id is None or not Path(state.snapshot_path).is_file():
            return state
        manifest = write_snapshot_manifest(
            store.path.parent,
            snapshot_path=state.snapshot_path,
            kb_id=state.kb_id,
            kb_name=state.kb_name,
            department_id=state.department_id,
            cohort_fingerprint=state.cohort_fingerprint,
            ownership_verified=state.mode == "online"
            or state.snapshot_ownership_verified,
        )
        return store.mutate(
            lambda current: RunStateStore._with_update(
                current,
                snapshot_sha256=str(manifest["snapshot_sha256"]),
                snapshot_ownership_verified=bool(manifest["ownership_verified"]),
            )
        )

    @staticmethod
    def _service_public_metadata(service: object) -> dict[str, object]:
        candidates = [getattr(service, "config", None)]
        adapter = getattr(service, "ragas_adapter", None)
        candidates.append(getattr(adapter, "config", None))
        for config in candidates:
            public_metadata = getattr(config, "public_metadata", None)
            if callable(public_metadata):
                try:
                    value = public_metadata()
                except Exception:
                    continue
                if isinstance(value, dict):
                    return dict(value)
        return {}

    def _record_service_metadata(
        self, store: RunStateStore, service: object
    ) -> EvaluationRunState:
        state = store.load()
        public = self._service_public_metadata(service)
        if not public:
            return state
        evaluation_config = {**state.evaluation_config, **public}
        return store.mutate(
            lambda current: RunStateStore._with_update(
                current,
                evaluation_config=evaluation_config,
                llm_model=str(public.get("llm_model") or current.llm_model),
                embedding_model=str(
                    public.get("embedding_model") or current.embedding_model
                ),
            )
        )

    @staticmethod
    def _decorate_summary(
        summary: EvaluationSummary, state: EvaluationRunState
    ) -> EvaluationSummary:
        fields = (
            "created_at",
            "kb_id",
            "kb_name",
            "department_id",
            "created_by",
            "source_dataset_path",
            "dataset_sha256",
            "execution_dataset_sha256",
            "dataset_total_count",
            "dataset_sample_count",
            "matched_sample_count",
            "filtered_sample_count",
            "normal_sample_count",
            "expected_denied_sample_count",
            "cohort_fingerprint",
            "snapshot_sha256",
            "snapshot_ownership_verified",
            "validation_warnings",
            "llm_model",
            "embedding_model",
            "evaluation_config",
        )
        updates = {field: getattr(state, field) for field in fields}
        metadata = {
            **summary.metadata,
            "evaluation_run": {
                field: getattr(state, field)
                for field in fields
                if getattr(state, field) not in (None, "", {}, [], 0)
            },
        }
        updates["metadata"] = metadata
        return summary.model_copy(update=updates)

    def execute(self, run_id: str) -> EvaluationRunState:
        try:
            initial_state = self._store(run_id).load()
            mode = initial_state.mode
        except Exception:
            mode = "unknown"
        status = "failed"
        with observe.evaluator(
            "hdb.evaluation.run",
            run_id=run_id,
            mode=mode,
        ) as observation:
            try:
                result = self._execute_impl(run_id)
                status = result.status
                observation.set("hdb.evaluation.status", status)
                return result
            except Exception:
                status = "failed"
                raise
            finally:
                record_evaluation(status=status, mode=mode)

    def _execute_impl(self, run_id: str) -> EvaluationRunState:
        store = self._store(run_id)
        latest_checkpoint: tuple[object, object] | None = None
        try:
            state = self._refresh_progress(store)
            if state.status == "queued":
                store.mark_running(stage="collecting")
            elif state.status not in {"running", "pause_requested", "cancel_requested"}:
                raise ValueError(f"cannot execute run from {state.status!r}")

            samples = self._load_samples(store.load())
            state = store.load()
            service = self.service_factory()
            state = self._record_service_metadata(store, service)
            if state.mode == "online":
                errors = service.preflight_online(samples)
                if errors:
                    return store.mark_failed(
                        f"evaluation preflight failed: {'; '.join(errors)}"
                    )
                snapshots = service.collect(
                    samples,
                    state.snapshot_path,
                    resume=True,
                    before_sample=lambda sample, _done, _total: self._before_sample(
                        store, sample
                    ),
                    after_sample=lambda snapshot, completed, _total: self._after_sample(
                        store, snapshot, completed
                    ),
                )
            else:
                snapshots = SnapshotStore(state.snapshot_path).load_all()

            self._record_snapshot_manifest(store)

            if not self._checkpoint(store):
                return store.load()
            if store.load().status in {"paused", "cancelled"}:
                return store.load()

            state = store.load()
            # 采集/评分分离：在线 run 采集结束先做质检门禁，落盘
            # collection_qc.json 并进入 collected 状态，等待用户确认后
            # 通过 start_scoring() 显式进入评分（离线 run 不受影响）。
            if state.mode == "online" and not (state.evaluation_config or {}).get(
                "scoring_requested"
            ):
                qc = run_collection_qc(samples, snapshots)
                qc_path = store.path.parent / "collection_qc.json"
                qc_path.write_text(
                    json.dumps(qc, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8",
                )
                return store.mark_collected(str(qc.get("verdict", "")))

            # Scoring prerequisites are checked only when this execution is
            # actually entering the scoring phase.  Online runs reach this
            # point only after the explicit post-collection start_scoring()
            # action; collection itself must not contact the judge LLM or
            # embedding endpoint.
            scoring_preflight = getattr(service, "preflight_scoring", None)
            errors = scoring_preflight() if callable(scoring_preflight) else []
            if errors:
                return store.mark_failed(
                    f"evaluation scoring preflight failed: {'; '.join(errors)}"
                )

            store.mutate(
                lambda current: RunStateStore._with_update(current, stage="scoring")
            )

            def checkpoint(summary, results, completed_groups, total_groups):
                nonlocal latest_checkpoint
                latest_checkpoint = (summary, results)
                store.update_scoring_progress(completed_groups, total_groups)
                write_reports(
                    store.path.parent / ".checkpoint",
                    self._decorate_summary(summary, store.load()),
                    results,
                )
                return store.load().status not in {
                    "pause_requested",
                    "cancel_requested",
                }

            last_item_report_at = 0.0
            item_report_interval = 2.0
            item_report_every = 5

            def item_checkpoint(summary, results, completed_items, total_items):
                nonlocal last_item_report_at
                nonlocal latest_checkpoint
                latest_checkpoint = (summary, results)
                state = store.load()
                store.update_scoring_progress(
                    state.scoring_completed_groups,
                    state.scoring_total_groups,
                    completed_items=completed_items,
                    total_items=total_items,
                )
                now = time.monotonic()
                should_write = (
                    completed_items >= total_items
                    or completed_items % item_report_every == 0
                    or now - last_item_report_at >= item_report_interval
                    or state.status in {"pause_requested", "cancel_requested"}
                )
                if should_write:
                    write_reports(
                        store.path.parent / ".checkpoint",
                        self._decorate_summary(summary, store.load()),
                        results,
                    )
                    last_item_report_at = now

            scoring_service = (
                service if state.mode == "online" else self.service_factory()
            )
            summary, results = scoring_service.score(
                samples,
                snapshots,
                run_id=run_id,
                progress_callback=checkpoint,
                item_progress_callback=item_checkpoint,
            )
            # RAGAS may lazily construct its public config during score().
            # Persist that config after scoring as well as before collection so
            # completed reports can be compared reproducibly.
            state = self._record_service_metadata(store, scoring_service)
            if state.status in {"pause_requested", "cancel_requested"}:
                outcome_kind = (
                    "partial_paused"
                    if state.status == "pause_requested"
                    else "partial_cancelled"
                )
                paths = write_reports(
                    store.path.parent,
                    self._decorate_summary(summary, state),
                    results,
                    metadata={
                        "run_outcome": {
                            "kind": outcome_kind,
                            "completed_groups": state.scoring_completed_groups,
                            "total_groups": state.scoring_total_groups,
                        }
                    },
                )
                return store.publish_partial_report(report_path=str(paths.report_html))
            store.mutate(
                lambda current: RunStateStore._with_update(current, stage="reporting")
            )
            paths = write_reports(
                store.path.parent,
                self._decorate_summary(summary, store.load()),
                results,
                metadata={
                    "run_outcome": {
                        "kind": "completed",
                        "completed_groups": store.load().scoring_completed_groups,
                        "total_groups": store.load().scoring_total_groups,
                    }
                },
            )
            state = store.load()
            if state.status in {"pause_requested", "cancel_requested"}:
                outcome_kind = (
                    "partial_paused"
                    if state.status == "pause_requested"
                    else "partial_cancelled"
                )
                paths = write_reports(
                    store.path.parent,
                    self._decorate_summary(summary, state),
                    results,
                    metadata={
                        "run_outcome": {
                            "kind": outcome_kind,
                            "completed_groups": state.scoring_completed_groups,
                            "total_groups": state.scoring_total_groups,
                        }
                    },
                )
                return store.publish_partial_report(report_path=str(paths.report_html))
            return store.complete_report_or_handle_control(
                report_path=str(paths.report_html)
            )
        except Exception:
            logging.getLogger(__name__).exception("evaluation worker failed")
            if latest_checkpoint is not None:
                summary, results = latest_checkpoint
                try:
                    paths = write_reports(
                        store.path.parent,
                        self._decorate_summary(summary, store.load()),
                        results,
                        metadata={
                            "run_outcome": {
                                "kind": "partial_failed",
                                "completed_groups": store.load().scoring_completed_groups,
                                "total_groups": store.load().scoring_total_groups,
                            }
                        },
                    )
                    return store.publish_partial_report(
                        report_path=str(paths.report_html)
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "failed to publish partial evaluation report"
                    )
            try:
                store.remove_report_artifacts()
            except OSError:
                logging.getLogger(__name__).exception(
                    "failed to remove incomplete evaluation report artifacts"
                )
            try:
                return store.mark_failed(
                    "evaluation worker failed; see application logs"
                )
            except Exception:
                return store.load()

    def pause(self, run_id: str) -> EvaluationRunState:
        return self._store(run_id).request_pause()

    def cancel(self, run_id: str) -> EvaluationRunState:
        return self._store(run_id).request_cancel()

    def delete(self, run_id: str) -> EvaluationRunState:
        """Delete an allowed terminal run after confirming no worker is live."""
        run_dir = (self.state_root / run_id).resolve()
        state_root = self.state_root.resolve()
        if run_dir.parent != state_root:
            raise ValueError("invalid evaluation run id")

        with self._threads_lock:
            thread = self._threads.get(run_id)
            if thread is not None and thread.is_alive():
                raise ValueError(f"evaluation run {run_id!r} is still running")

            state_path = run_dir / "run_state.json"
            if not run_dir.is_dir() or not state_path.is_file():
                raise FileNotFoundError(f"evaluation run {run_id!r} was not found")

            state = RunStateStore(state_path).load()
            if state.status not in _DELETABLE_RUN_STATUSES:
                raise ValueError(
                    f"evaluation run {run_id!r} can only be deleted from a failed, cancelled, or completed state"
                )

            shutil.rmtree(run_dir)
            self._threads.pop(run_id, None)
            return state

    def resume(self, run_id: str) -> EvaluationRunState:
        def queue(state: EvaluationRunState) -> EvaluationRunState:
            if state.status not in {"paused", "cancelled"}:
                raise ValueError(f"cannot resume run from {state.status!r}")
            return RunStateStore._with_update(
                state,
                status="queued",
                stage="idle",
                current_sample_id="",
                current_question="",
                finished_at="",
                error_message="",
                report_path="",
            )

        store = self._store(run_id)
        store.mutate(queue)
        return self._refresh_progress(store)

    def load_collection_qc(self, run_id: str) -> dict[str, object] | None:
        """Return the persisted collection QC report, recomputing if absent."""

        qc_path = self.state_root / run_id / "collection_qc.json"
        if qc_path.is_file():
            try:
                return json.loads(qc_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        state = self._store(run_id).load()
        if state.mode != "online" or not Path(state.snapshot_path).is_file():
            return None
        samples = self._load_samples(state)
        snapshots = SnapshotStore(state.snapshot_path).load_all()
        qc = run_collection_qc(samples, snapshots)
        try:
            qc_path.write_text(
                json.dumps(qc, ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return qc

    def start_scoring(self, run_id: str, *, force: bool = False) -> EvaluationRunState:
        """Enter the scoring phase from the collected QC-gate state."""

        store = self._store(run_id)
        state = store.load()
        if state.status != "collected":
            raise ValueError(
                f"cannot start scoring from {state.status!r}; "
                "scoring requires a run in the collected state"
            )
        qc = self.load_collection_qc(run_id) or {}
        verdict = str(qc.get("verdict") or "fail")
        if verdict == "fail" and not force:
            issues = "; ".join(str(item) for item in (qc.get("issues") or []))
            raise ValueError(f"collection QC failed: {issues or verdict}")
        store.mutate(
            lambda current: RunStateStore._with_update(
                current,
                evaluation_config={
                    **(current.evaluation_config or {}),
                    "scoring_requested": True,
                },
            )
        )
        store.request_scoring()
        self.start(run_id)
        return store.load()

    def load_for_display(self, run_id: str) -> EvaluationRunState:
        with self._threads_lock:
            thread = self._threads.get(run_id)
            if thread is not None and thread.is_alive():
                return self._store(run_id).load()
            store = self._store(run_id)
            if not store.path.is_file():
                # Legacy run (pre-run_state): no run_state.json, only report
                # artifacts. Synthesise a completed state so it can be opened.
                return self._legacy_state(run_id)
            return store.mark_orphaned_as_paused()

    def _legacy_state(self, run_id: str) -> EvaluationRunState:
        """Build a completed state for a legacy run that has ``summary.json``
        but no ``run_state.json`` (created before run-state tracking existed)."""
        summary_path = self.state_root / run_id / "summary.json"
        if not summary_path.is_file():
            raise FileNotFoundError(
                f"evaluation run {run_id!r} has neither run_state.json nor summary.json"
            )
        try:
            summary = EvaluationSummary.model_validate_json(
                summary_path.read_text(encoding="utf-8-sig")
            )
        except Exception as exc:
            # Corrupt summary is a server-side problem, not "not found".
            raise ValueError(f"summary invalid: {exc}") from exc
        return EvaluationRunState(
            run_id=run_id,
            dataset_path="",
            snapshot_path="",
            mode="offline",
            status="completed",
            stage="reporting",
            total_samples=summary.sample_count,
            completed_samples=summary.sample_count,
            successful_samples=summary.successful_samples,
            failed_samples=summary.failed_samples,
        )

    def _store(self, run_id: str) -> RunStateStore:
        return RunStateStore(self.state_root / run_id / "run_state.json")

    @staticmethod
    def _checkpoint(store: RunStateStore) -> bool:
        state = store.load()
        if state.status == "pause_requested":
            store.mark_paused()
            return False
        if state.status == "cancel_requested":
            store.mark_cancelled()
            return False
        return True

    def _before_sample(self, store: RunStateStore, sample: EvaluationSample) -> bool:
        return store.begin_sample(sample).status == "running"

    def _after_sample(
        self, store: RunStateStore, _snapshot: AnswerSnapshot, completed: int
    ) -> EvaluationRunState:
        return self._refresh_progress(store)

    @staticmethod
    def _snapshot_counts(snapshot_path: str | Path) -> dict[str, int]:
        latest = {
            snapshot.sample_id: snapshot
            for snapshot in SnapshotStore(snapshot_path).load_all()
        }
        return {
            "completed_samples": len(latest),
            "successful_samples": sum(
                snapshot.status == "success" for snapshot in latest.values()
            ),
            "failed_samples": sum(
                snapshot.status == "failed" for snapshot in latest.values()
            ),
        }

    def _refresh_progress(self, store: RunStateStore) -> EvaluationRunState:
        state = store.load()
        counts = self._snapshot_counts(state.snapshot_path)
        return store.mutate(
            lambda current: RunStateStore._with_update(
                current,
                **counts,
                current_sample_id="",
                current_question="",
            )
        )

    @staticmethod
    def _load_samples(state: EvaluationRunState) -> list[EvaluationSample]:
        samples = load_dataset(state.dataset_path)
        if state.sample_ids:
            sample_ids = set(state.sample_ids)
            samples = [sample for sample in samples if sample.id in sample_ids]
        if state.tags:
            tags = set(state.tags)
            samples = [sample for sample in samples if tags.intersection(sample.tags)]
        return samples
