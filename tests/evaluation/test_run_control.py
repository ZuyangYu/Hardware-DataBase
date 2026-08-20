import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.evaluation import reporters
from src.evaluation.run_control import (
    EvaluationRunController,
    RunStateStore,
    discover_run_dirs,
)
from src.evaluation.reporters import write_reports
from src.evaluation.schemas import (
    AnswerSnapshot,
    EvaluationRunState,
    EvaluationSample,
    EvaluationSummary,
    SampleResult,
)
from src.evaluation.snapshot_store import SnapshotStore


def _state(status: str = "queued") -> EvaluationRunState:
    return EvaluationRunState.new_online(
        run_id="run-1",
        dataset_path="dataset.jsonl",
        snapshot_path="runs/run-1/snapshot.jsonl",
        total_samples=2,
        score_enabled=True,
    ).model_copy(
        update={
            "status": status,
            "current_sample_id": "sample-1",
            "current_question": "What is the voltage?",
        }
    )


class RunStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _store(self, status: str = "queued") -> RunStateStore:
        store = RunStateStore(self.root / "run-1" / "run_state.json")
        store.create(_state(status))
        return store

    def test_state_store_round_trips_and_removes_temporary_file(self):
        state = EvaluationRunState.new_online(
            run_id="run-1",
            dataset_path="dataset.jsonl",
            snapshot_path="runs/run-1/snapshot.jsonl",
            total_samples=2,
            score_enabled=True,
        )
        store = RunStateStore(self.root / "run-1" / "run_state.json")

        store.create(state)

        self.assertEqual(store.load().status, "queued")
        self.assertFalse(store.path.with_suffix(".json.tmp").exists())

    def test_scoring_progress_tracks_groups_and_items(self):
        store = self._store(status="running")

        state = store.update_scoring_progress(1, 2, completed_items=3, total_items=10)

        self.assertEqual((state.scoring_completed_groups, state.scoring_total_groups), (1, 2))
        self.assertEqual((state.scoring_completed_items, state.scoring_total_items), (3, 10))

    def test_pause_cancel_and_orphan_transitions_are_valid(self):
        store = self._store(status="running")

        self.assertEqual(store.request_pause().status, "pause_requested")
        self.assertEqual(store.mark_paused().status, "paused")
        self.assertEqual(store.request_cancel().status, "cancelled")
        self.assertEqual(
            self._store(status="running").mark_orphaned_as_paused().status, "paused"
        )

    def test_invalid_control_requests_raise_value_error(self):
        with self.assertRaises(ValueError):
            self._store(status="paused").request_pause()
        with self.assertRaises(ValueError):
            self._store(status="completed").request_cancel()

    def test_terminal_methods_finish_runs_and_clear_current_sample(self):
        for method_name, source_status, expected_status in (
            ("mark_cancelled", "cancel_requested", "cancelled"),
            ("mark_completed", "running", "completed"),
            ("mark_failed", "running", "failed"),
        ):
            with self.subTest(method_name=method_name):
                state = getattr(self._store(status=source_status), method_name)()

                self.assertEqual(state.status, expected_status)
                self.assertEqual(state.current_sample_id, "")
                self.assertEqual(state.current_question, "")
                self.assertTrue(state.finished_at)
                self.assertTrue(state.updated_at)

    def test_worker_transitions_require_expected_source_state(self):
        with self.assertRaises(ValueError):
            self._store(status="running").mark_paused()
        with self.assertRaises(ValueError):
            self._store(status="running").mark_cancelled()

    def test_worker_transitions_reject_terminal_state_rewrites(self):
        for status in ("cancelled", "completed", "failed"):
            for method_name in (
                "mark_paused",
                "mark_cancelled",
                "mark_completed",
                "mark_failed",
            ):
                with self.subTest(status=status, method_name=method_name):
                    with self.assertRaises(ValueError):
                        getattr(self._store(status=status), method_name)()

    def test_write_failure_removes_temporary_file(self):
        store = RunStateStore(self.root / "run-1" / "run_state.json")
        temporary = store.path.with_suffix(".json.tmp")
        original_write_text = Path.write_text

        def write_then_fail(path: Path, *args, **kwargs):
            original_write_text(path, *args, **kwargs)
            if path == temporary:
                raise OSError("simulated write failure")
            return None

        with patch.object(
            Path, "write_text", autospec=True, side_effect=write_then_fail
        ):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                store.create(_state())

        self.assertFalse(temporary.exists())

    def test_replace_failure_removes_temporary_file(self):
        store = RunStateStore(self.root / "run-1" / "run_state.json")
        temporary = store.path.with_suffix(".json.tmp")

        with patch(
            "src.evaluation.run_control.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated replace failure"):
                store.create(_state())

        self.assertFalse(temporary.exists())

    def test_replace_retries_a_transient_permission_error(self):
        store = RunStateStore(self.root / "run-1" / "run_state.json")
        replace = os.replace
        attempts = 0

        def fail_once_then_replace(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("simulated transient file lock")
            return replace(source, destination)

        with patch(
            "src.evaluation.run_control.os.replace",
            side_effect=fail_once_then_replace,
        ):
            store.create(_state())

        self.assertEqual(attempts, 2)
        self.assertEqual(store.load().status, "queued")

    def test_load_acquires_the_state_lock(self):
        class TrackingLock:
            def __init__(self):
                self.entered = 0

            def __enter__(self):
                self.entered += 1
                return self

            def __exit__(self, *args):
                return False

        store = self._store()
        lock = TrackingLock()
        store._lock = lock

        state = store.load()

        self.assertEqual(state.status, "queued")
        self.assertEqual(lock.entered, 1)

    def test_orphan_transition_leaves_terminal_states_unchanged(self):
        for status in ("paused", "cancelled", "completed", "failed"):
            with self.subTest(status=status):
                self.assertEqual(
                    self._store(status=status).mark_orphaned_as_paused().status, status
                )

    def test_discover_run_dirs_returns_state_directories_in_name_order(self):
        self._store()
        second = RunStateStore(self.root / "run-2" / "run_state.json")
        second.create(_state().model_copy(update={"run_id": "run-2"}))
        (self.root / "not-a-run").mkdir()

        self.assertEqual(
            discover_run_dirs(self.root), [self.root / "run-1", self.root / "run-2"]
        )


def _sample(sample_id: str, *, tags: list[str] | None = None) -> EvaluationSample:
    return EvaluationSample(
        id=sample_id,
        question=f"Question {sample_id}",
        reference_answer=f"Answer {sample_id}",
        kb_name="hardware",
        tags=tags or [],
    )


class FakeEvaluationService:
    def __init__(self):
        self.collected_ids: list[str] = []
        self.control_after_id = None
        self.score_control = None
        self.raise_after_score_checkpoint = False
        self.preflight_errors: list[str] = []

    def preflight_online(self, samples):
        return self.preflight_errors

    def collect(self, samples, snapshot_path, *, resume, before_sample, after_sample):
        store = SnapshotStore(snapshot_path)
        completed_ids = store.completed_ids() if resume else set()
        completed = len(completed_ids)
        total = len(samples)
        for sample in samples:
            if sample.id in completed_ids:
                continue
            if not before_sample(sample, completed, total):
                break
            snapshot = AnswerSnapshot(
                sample_id=sample.id,
                question=sample.question,
                kb_name=sample.kb_name,
                response=sample.reference_answer,
            )
            store.append(snapshot)
            self.collected_ids.append(sample.id)
            completed += 1
            after_sample(snapshot, completed, total)
            control = self.control_after_id
            if control is not None and control[1] == sample.id:
                self.control_after_id = None
                getattr(control[2], control[0])(control[3])
        return store.load_all()

    def score(
        self,
        samples,
        snapshots,
        *,
        run_id,
        progress_callback=None,
        item_progress_callback=None,
    ):
        snapshot_ids = {snapshot.sample_id for snapshot in snapshots}
        successful = sum(sample.id in snapshot_ids for sample in samples)
        summary = EvaluationSummary(
            run_id=run_id,
            sample_count=len(samples),
            successful_samples=successful,
            failed_samples=len(samples) - successful,
        )
        results = [
            SampleResult(
                sample_id=sample.id,
                question=sample.question,
                reference_answer=sample.reference_answer,
                snapshot_status="success"
                if sample.id in snapshot_ids
                else "failed",
            )
            for sample in samples
        ]
        if progress_callback is not None:
            if self.score_control is not None:
                getattr(self.score_control[0], self.score_control[1])(self.score_control[2])
                self.score_control = None
            progress_callback(summary, results, 1, 1)
            if self.raise_after_score_checkpoint:
                raise RuntimeError("scoring exploded")
        if item_progress_callback is not None:
            item_progress_callback(summary, results, 1, 1)
        return summary, results


class EvaluationRunControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "runs"
        self.dataset = Path(self.temp_dir.name) / "dataset.jsonl"
        self.samples = [_sample("q1", tags=["core"]), _sample("q2", tags=["edge"])]
        self.dataset.write_text(
            "\n".join(sample.model_dump_json() for sample in self.samples) + "\n",
            encoding="utf-8",
        )
        self.snapshot_path = Path(self.temp_dir.name) / "snapshots.jsonl"
        self.fake_service = FakeEvaluationService()
        self.controller = EvaluationRunController(lambda: self.fake_service, self.root)

    def test_collection_only_run_updates_counts_and_writes_no_report(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )

        final = self.controller.execute(state.run_id)

        self.assertEqual(final.status, "completed")
        self.assertEqual((final.completed_samples, final.successful_samples), (2, 2))
        self.assertFalse((self.root / state.run_id / "summary.json").exists())

    def test_scoring_item_checkpoint_is_persisted_during_run(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=True
        )

        final = self.controller.execute(state.run_id)
        checkpoint = self.root / state.run_id / ".checkpoint"

        self.assertEqual(final.status, "completed")
        self.assertTrue((checkpoint / "summary.json").is_file())
        self.assertTrue((checkpoint / "results.jsonl").is_file())
        self.assertEqual(final.scoring_completed_items, 1)
        self.assertEqual(final.scoring_total_items, 1)

    def test_online_run_fails_before_collection_when_preflight_reports_errors(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )
        self.fake_service.preflight_errors = ["q1: no discoverable sources for hardware"]

        final = self.controller.execute(state.run_id)

        self.assertEqual(final.status, "failed")
        self.assertEqual(self.fake_service.collected_ids, [])
        self.assertIn("preflight", final.error_message)

    def test_delete_failed_run_removes_all_run_artifacts(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )
        store = RunStateStore(self.root / state.run_id / "run_state.json")
        store.mark_running(stage="collecting")
        store.mark_failed("preflight failed")
        (self.root / state.run_id / "extra-artifact.txt").write_text(
            "artifact", encoding="utf-8"
        )

        deleted = self.controller.delete(state.run_id)

        self.assertEqual(deleted.status, "failed")
        self.assertFalse((self.root / state.run_id).exists())

    def test_delete_rejects_non_terminal_or_successful_runs(self):
        for status in ("queued", "running", "paused", "completed"):
            with self.subTest(status=status):
                state = self.controller.create_online_run(
                    self.dataset, self.root, self.samples, score_enabled=False
                )
                store = RunStateStore(self.root / state.run_id / "run_state.json")
                if status != "queued":
                    store.mutate(lambda current: current.model_copy(update={"status": status}))

                with self.assertRaises(ValueError):
                    self.controller.delete(state.run_id)
                self.assertTrue((self.root / state.run_id).exists())

    def test_delete_rejects_run_id_path_traversal(self):
        with self.assertRaises(ValueError):
            self.controller.delete("../outside")

    def test_pause_then_resume_skips_existing_successful_snapshot(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )
        self.fake_service.control_after_id = (
            "pause",
            "q1",
            self.controller,
            state.run_id,
        )

        paused = self.controller.execute(state.run_id)
        resumed_state = self.controller.resume(state.run_id)
        resumed = self.controller.execute(resumed_state.run_id)

        self.assertEqual(paused.status, "paused")
        self.assertEqual(paused.current_sample_id, "")
        self.assertEqual(paused.current_question, "")
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(self.fake_service.collected_ids, ["q1", "q2"])

    def test_cancelled_run_publishes_partial_report_and_can_resume(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=True
        )
        self.fake_service.score_control = (self.controller, "cancel", state.run_id)

        final = self.controller.execute(state.run_id)
        self.assertEqual(final.status, "cancelled")
        self.assertTrue((self.root / state.run_id / "summary.json").exists())
        partial = EvaluationSummary.model_validate_json(
            (self.root / state.run_id / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(partial.metadata["run_outcome"]["kind"], "partial_cancelled")
        resumed_state = self.controller.resume(state.run_id)
        self.assertEqual(
            self.controller.execute(resumed_state.run_id).status, "completed"
        )

    def test_scoring_exception_publishes_latest_checkpoint(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=True
        )
        self.fake_service.raise_after_score_checkpoint = True

        final = self.controller.execute(state.run_id)

        self.assertEqual(final.status, "failed")
        summary = EvaluationSummary.model_validate_json(
            (self.root / state.run_id / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary.metadata["run_outcome"]["kind"], "partial_failed")

    def test_offline_score_writes_report_path(self):
        SnapshotStore(self.snapshot_path).append(
            AnswerSnapshot(sample_id="q1", question="Question q1", kb_name="hardware")
        )
        state = self.controller.create_offline_run(
            self.dataset, self.root, self.samples, self.snapshot_path
        )

        final = self.controller.execute(state.run_id)

        self.assertEqual(final.status, "completed")
        self.assertTrue(Path(final.report_path).is_file())
        self.assertEqual(Path(final.report_path).parent, self.root / state.run_id)

    def test_pause_requested_during_report_write_publishes_partial_report(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=True
        )
        original_write_reports = write_reports
        requested = False

        def write_then_request_pause(*args, **kwargs):
            nonlocal requested
            paths = original_write_reports(*args, **kwargs)
            if Path(args[0]) == self.root / state.run_id and not requested:
                self.controller.pause(state.run_id)
                requested = True
            return paths

        with patch(
            "src.evaluation.run_control.write_reports",
            side_effect=write_then_request_pause,
        ):
            final = self.controller.execute(state.run_id)

        self.assertEqual(final.status, "paused")
        summary = EvaluationSummary.model_validate_json(
            (self.root / state.run_id / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary.metadata["run_outcome"]["kind"], "partial_paused")

    def test_cancel_requested_during_report_write_publishes_partial_report(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=True
        )
        original_write_reports = write_reports
        requested = False

        def write_then_request_cancel(*args, **kwargs):
            nonlocal requested
            paths = original_write_reports(*args, **kwargs)
            if Path(args[0]) == self.root / state.run_id and not requested:
                self.controller.cancel(state.run_id)
                requested = True
            return paths

        with patch(
            "src.evaluation.run_control.write_reports",
            side_effect=write_then_request_cancel,
        ):
            final = self.controller.execute(state.run_id)

        self.assertEqual(final.status, "cancelled")
        summary = EvaluationSummary.model_validate_json(
            (self.root / state.run_id / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary.metadata["run_outcome"]["kind"], "partial_cancelled")

    def test_report_write_failure_removes_published_report_artifacts(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=True
        )
        original_atomic_text = reporters._atomic_text

        def publish_summary_then_fail(path, *args, **kwargs):
            original_atomic_text(path, *args, **kwargs)
            if path.name == "summary.json":
                raise OSError("simulated report write failure")

        with patch(
            "src.evaluation.reporters._atomic_text",
            side_effect=publish_summary_then_fail,
        ):
            final = self.controller.execute(state.run_id)

        self.assertEqual(final.status, "failed")
        for name in (
            "summary.json",
            "results.jsonl",
            "summary.csv",
            "report.html",
            "summary.json.tmp",
            "results.jsonl.tmp",
            "summary.csv.tmp",
            "report.html.tmp",
        ):
            self.assertFalse((self.root / state.run_id / name).exists())

    def test_unowned_running_state_is_recovered_as_paused(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )
        RunStateStore(self.root / state.run_id / "run_state.json").mark_running(
            stage="collecting"
        )

        self.assertEqual(
            self.controller.load_for_display(state.run_id).status, "paused"
        )

    def test_orphaned_scoring_run_without_checkpoint_does_not_fabricate_report(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=True
        )
        RunStateStore(self.root / state.run_id / "run_state.json").mark_running(
            stage="scoring"
        )

        recovered = self.controller.load_for_display(state.run_id)

        self.assertEqual(recovered.status, "paused")
        self.assertFalse((self.root / state.run_id / "summary.json").exists())

    def test_legacy_run_without_run_state_opens_as_completed(self):
        # Legacy run generated before run-state tracking: report artifacts only,
        # no run_state.json. load_for_display must not FileNotFoundError.
        run_dir = self.root / "legacy-20260723"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            EvaluationSummary(
                run_id="legacy-20260723",
                sample_count=7,
                successful_samples=6,
                failed_samples=1,
            ).model_dump_json(),
            encoding="utf-8",
        )

        state = self.controller.load_for_display("legacy-20260723")

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.stage, "reporting")
        self.assertEqual(state.total_samples, 7)
        self.assertEqual(state.successful_samples, 6)
        self.assertEqual(state.failed_samples, 1)

    def test_load_for_display_raises_when_neither_state_nor_summary_exists(self):
        with self.assertRaises(FileNotFoundError):
            self.controller.load_for_display("no-such-run")

    def test_display_recovery_does_not_pause_a_concurrently_registered_worker(self):
        class StartAfterReleaseLock:
            def __init__(self, controller, run_id):
                self._lock = threading.RLock()
                self._controller = controller
                self._run_id = run_id
                self._started = threading.Event()
                self._armed = True

            def __enter__(self):
                self._lock.acquire()
                return self

            def __exit__(self, *_args):
                self._lock.release()
                if self._armed:
                    self._armed = False

                    def start():
                        self._controller.start(self._run_id)
                        self._started.set()

                    threading.Thread(target=start).start()
                    self._started.wait()

        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )
        observed_worker_liveness = []
        original_mark_orphaned = RunStateStore.mark_orphaned_as_paused

        def mark_orphaned(store):
            worker = self.controller._threads.get(state.run_id)
            observed_worker_liveness.append(worker is not None and worker.is_alive())
            return original_mark_orphaned(store)

        self.controller._threads_lock = StartAfterReleaseLock(
            self.controller, state.run_id
        )
        with patch.object(RunStateStore, "mark_orphaned_as_paused", new=mark_orphaned):
            self.controller.load_for_display(state.run_id)

        worker = self.controller._threads[state.run_id]
        worker.join()
        self.assertEqual(observed_worker_liveness, [False])

    def test_sample_transition_does_not_overwrite_interleaved_control_request(self):
        for control, expected_status in (("pause", "paused"), ("cancel", "cancelled")):
            with self.subTest(control=control):
                state = self.controller.create_online_run(
                    self.dataset, self.root, self.samples, score_enabled=False
                )
                store = RunStateStore(self.root / state.run_id / "run_state.json")
                original_mutate = RunStateStore.mutate
                requested = False

                def request_control_before_transition(current_store, mutator):
                    nonlocal requested
                    if current_store.path == store.path and not requested:
                        requested = True
                        getattr(self.controller, control)(state.run_id)
                    return original_mutate(current_store, mutator)

                with patch.object(
                    RunStateStore,
                    "mutate",
                    new=request_control_before_transition,
                ):
                    started = self.controller._before_sample(store, self.samples[0])

                self.assertFalse(started)
                self.assertEqual(store.load().status, expected_status)

    def test_execute_recomputes_snapshot_persisted_before_callback(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )
        SnapshotStore(state.snapshot_path).append(
            AnswerSnapshot(sample_id="q1", question="Question q1", kb_name="hardware")
        )

        final = self.controller.execute(state.run_id)

        self.assertEqual(
            (final.completed_samples, final.successful_samples, final.failed_samples),
            (2, 2, 0),
        )
        self.assertEqual(self.fake_service.collected_ids, ["q2"])

    def test_after_sample_recomputes_counts_from_latest_snapshots(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )
        store = RunStateStore(self.root / state.run_id / "run_state.json")
        store.mark_running()
        first = AnswerSnapshot(
            sample_id="q1", question="Question q1", kb_name="hardware"
        )
        second = AnswerSnapshot(
            sample_id="q2", question="Question q2", kb_name="hardware"
        )
        snapshots = SnapshotStore(state.snapshot_path)
        snapshots.append(first)
        snapshots.append(second)

        final = self.controller._after_sample(store, second, completed=2)

        self.assertEqual(
            (final.completed_samples, final.successful_samples, final.failed_samples),
            (2, 2, 0),
        )

    def test_resume_recomputes_failed_snapshot_retried_as_success(self):
        state = self.controller.create_online_run(
            self.dataset, self.root, self.samples, score_enabled=False
        )
        store = RunStateStore(self.root / state.run_id / "run_state.json")
        store.mark_running()
        snapshots = SnapshotStore(state.snapshot_path)
        failed = AnswerSnapshot(
            sample_id="q1",
            question="Question q1",
            kb_name="hardware",
            status="failed",
        )
        snapshots.append(failed)
        self.controller._after_sample(store, failed, completed=1)
        store.request_pause()
        store.mark_paused()
        snapshots.append(
            AnswerSnapshot(sample_id="q1", question="Question q1", kb_name="hardware")
        )

        resumed = self.controller.resume(state.run_id)

        self.assertEqual(
            (
                resumed.completed_samples,
                resumed.successful_samples,
                resumed.failed_samples,
            ),
            (1, 1, 0),
        )


if __name__ == "__main__":
    unittest.main()
