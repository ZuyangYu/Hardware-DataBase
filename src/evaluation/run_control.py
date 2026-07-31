from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
import logging
from pathlib import Path

from .dataset_loader import load_dataset
from .reporters import write_reports
from .schemas import AnswerSnapshot, EvaluationRunState, EvaluationSample
from .service import new_run_id
from .snapshot_store import SnapshotStore


_STATE_LOCK = threading.RLock()
_STATE_REPLACE_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)
_REPORT_ARTIFACT_NAMES = (
    "summary.json",
    "results.jsonl",
    "summary.csv",
    "report.html",
    "report_complete.json",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def complete_report_or_handle_control(
        self,
        *,
        report_path: str,
    ) -> EvaluationRunState:
        """Publish a report only when no pause or cancel request won the race."""
        with self._lock:
            state = self.load()
            if state.status in {"queued", "running"}:
                return self._write(
                    self._mark_completed(state, report_path=report_path)
                )
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
        self, completed_groups: int, total_groups: int
    ) -> EvaluationRunState:
        return self.mutate(
            lambda state: self._with_update(
                state,
                scoring_completed_groups=completed_groups,
                scoring_total_groups=total_groups,
            )
        )

    def publish_partial_report(
        self,
        *,
        report_path: str,
        error_message: str = "",
    ) -> EvaluationRunState:
        with self._lock:
            state = self.load()
            if state.status == "pause_requested":
                return self._write(self._finish(state, "paused", report_path=report_path))
            if state.status == "cancel_requested":
                return self._write(self._finish(state, "cancelled", report_path=report_path))
            return self._write(
                self._finish(
                    state,
                    "failed",
                    report_path=report_path,
                    error_message=error_message or "evaluation worker failed; see application logs",
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
    def _mark_cancelled(cls, state: EvaluationRunState) -> EvaluationRunState:
        if state.status not in {"cancel_requested", "paused"}:
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
        score_enabled: bool,
        sample_ids: set[str] | list[str] | None = None,
        tags: set[str] | list[str] | None = None,
    ) -> EvaluationRunState:
        run_id = new_run_id()
        state = EvaluationRunState.new_online(
            run_id=run_id,
            dataset_path=str(dataset_path),
            snapshot_path=str(Path(output_root) / run_id / "snapshot.jsonl"),
            total_samples=len(samples),
            score_enabled=score_enabled,
            sample_ids=sorted(sample_ids or []),
            tags=sorted(tags or []),
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
    ) -> EvaluationRunState:
        run_id = new_run_id()
        state = EvaluationRunState(
            run_id=run_id,
            dataset_path=str(dataset_path),
            snapshot_path=str(snapshot_path),
            mode="offline",
            score_enabled=True,
            total_samples=len(samples),
            sample_ids=sorted(sample_ids or []),
            tags=sorted(tags or []),
        )
        return self._store(run_id).create(state)

    def start(self, run_id: str) -> threading.Thread:
        with self._threads_lock:
            existing = self._threads.get(run_id)
            if existing is not None and existing.is_alive():
                raise RuntimeError(f"evaluation run {run_id!r} is already running")
            thread = threading.Thread(target=self.execute, args=(run_id,), daemon=True)
            self._threads[run_id] = thread
            thread.start()
            return thread

    def execute(self, run_id: str) -> EvaluationRunState:
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
            if state.mode == "online":
                service = self.service_factory()
                errors = service.preflight_online(samples)
                if errors:
                    return store.mark_failed(f"evaluation preflight failed: {'; '.join(errors)}")
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

            if not self._checkpoint(store):
                return store.load()
            if store.load().status in {"paused", "cancelled"}:
                return store.load()

            state = store.load()
            if not state.score_enabled:
                return store.mark_completed()

            store.mutate(
                lambda current: RunStateStore._with_update(current, stage="scoring")
            )
            def checkpoint(summary, results, completed_groups, total_groups):
                nonlocal latest_checkpoint
                latest_checkpoint = (summary, results)
                store.update_scoring_progress(completed_groups, total_groups)
                write_reports(store.path.parent / ".checkpoint", summary, results)
                return store.load().status not in {"pause_requested", "cancel_requested"}

            summary, results = (service if state.mode == "online" else self.service_factory()).score(
                samples, snapshots, run_id=run_id, progress_callback=checkpoint
            )
            state = store.load()
            if state.status in {"pause_requested", "cancel_requested"}:
                outcome_kind = (
                    "partial_paused" if state.status == "pause_requested" else "partial_cancelled"
                )
                paths = write_reports(
                    store.path.parent,
                    summary,
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
                summary,
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
                    "partial_paused" if state.status == "pause_requested" else "partial_cancelled"
                )
                paths = write_reports(
                    store.path.parent,
                    summary,
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
            return store.complete_report_or_handle_control(report_path=str(paths.report_html))
        except Exception:
            logging.getLogger(__name__).exception("evaluation worker failed")
            if latest_checkpoint is not None:
                summary, results = latest_checkpoint
                try:
                    paths = write_reports(
                        store.path.parent,
                        summary,
                        results,
                        metadata={
                            "run_outcome": {
                                "kind": "partial_failed",
                                "completed_groups": store.load().scoring_completed_groups,
                                "total_groups": store.load().scoring_total_groups,
                            }
                        },
                    )
                    return store.publish_partial_report(report_path=str(paths.report_html))
                except Exception:
                    logging.getLogger(__name__).exception("failed to publish partial evaluation report")
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

    def load_for_display(self, run_id: str) -> EvaluationRunState:
        with self._threads_lock:
            thread = self._threads.get(run_id)
            if thread is not None and thread.is_alive():
                return self._store(run_id).load()
            return self._store(run_id).mark_orphaned_as_paused()

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
