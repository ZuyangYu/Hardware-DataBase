from __future__ import annotations

import multiprocessing
import os
import threading
import subprocess
import sys

import pytest

from src.evaluation.execution import EvaluationExecutionLedger, EvaluationLeaseLost, execution_context
from src.evaluation.reporters import write_reports
from src.evaluation.reproducibility import EvaluationReproducibilityError, execution_content_hash
from src.evaluation.run_control import EvaluationRunController, RunStateStore
from src.evaluation.schemas import AnswerSnapshot, EvaluationRunState, EvaluationSample, EvaluationSummary, MetricResult, SampleResult
from src.evaluation.snapshot_store import SnapshotStore
from src.evaluation.worker import run_pending


class BlockingCollector:
    def __init__(self, entered, release):
        self.entered, self.release = entered, release

    def preflight_online(self, samples):
        return []

    def collect(self, samples, path, *, before_sample, after_sample, **kwargs):
        self.entered.set()
        assert self.release.wait(10)
        snapshots = []
        for sample in samples:
            if not before_sample(sample, 0, len(samples)):
                break
            snapshot = AnswerSnapshot(sample_id=sample.id, question=sample.question, kb_name=sample.kb_name, response="answer")
            SnapshotStore(path).append(snapshot)
            snapshots.append(snapshot)
            after_sample(snapshot, len(snapshots), len(samples))
        return snapshots


def _claim_in_process(root, ready, release):
    ledger = EvaluationExecutionLedger(root, backend="sqlite")
    claim = ledger.claim("run")
    ready.put(claim.fencing_token)
    release.wait(30)


def _increment_in_process(path, count):
    store = RunStateStore(path)
    for _ in range(count):
        store.mutate(lambda state: state.model_copy(update={"completed_samples": state.completed_samples + 1}))


def _controller_run(tmp_path, service):
    sample = EvaluationSample(id="q1", question="question", reference_answer="answer", kb_name="kb")
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(sample.model_dump_json() + "\n")
    root = tmp_path / "runs"
    controller = EvaluationRunController(lambda: service, root)
    state = controller.create_online_run(dataset, root, [sample])
    return controller, state


def test_other_controller_reads_live_run_without_mutation_and_can_pause(tmp_path):
    entered, release = threading.Event(), threading.Event()
    owner, state = _controller_run(tmp_path, BlockingCollector(entered, release))
    reader = EvaluationRunController(lambda: None, owner.state_root)
    worker = owner.start(state.run_id)
    try:
        assert entered.wait(10)
        path = owner._store(state.run_id).path
        before = path.read_bytes()
        assert reader.load_for_display(state.run_id).status == "running"
        assert path.read_bytes() == before
        with pytest.raises(RuntimeError, match="already running"):
            reader.start(state.run_id)
        with pytest.raises(RuntimeError, match="already running"):
            reader.recover_orphan(state.run_id)
        reader.pause(state.run_id)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert reader.load_for_display(state.run_id).status == "paused"


def test_expired_owner_cannot_write_state_snapshot_report_or_release_successor(tmp_path):
    ledger = EvaluationExecutionLedger(tmp_path, backend="sqlite")
    path = tmp_path / "run" / "run_state.json"
    store = RunStateStore(path)
    store.create(EvaluationRunState(run_id="run", dataset_path="", snapshot_path="", mode="offline"))
    old = ledger.claim("run")
    with ledger.transaction() as conn:
        conn.execute("UPDATE evaluation_execution_owners SET lease_until=0 WHERE scope_id=?", (ledger.scope,))
    successor = ledger.claim("run")
    assert successor.fencing_token > old.fencing_token
    with execution_context(ledger, old):
        with pytest.raises(EvaluationLeaseLost):
            store.mark_running()
        with pytest.raises(EvaluationLeaseLost):
            SnapshotStore(tmp_path / "snapshot.jsonl").append(AnswerSnapshot(sample_id="q", question="q", kb_name="kb"))
        with pytest.raises(EvaluationLeaseLost):
            write_reports(tmp_path / "run", EvaluationSummary(run_id="run"), [])
    ledger.release(old)
    assert ledger.heartbeat(successor)
    assert store.load().status == "queued"
    assert not (tmp_path / "snapshot.jsonl").exists()
    assert not (tmp_path / "run" / "summary.json").exists()


def test_execution_claim_is_shared_across_real_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=_claim_in_process, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.get(timeout=30) == 1
        ledger = EvaluationExecutionLedger(tmp_path, backend="sqlite")
        with pytest.raises(RuntimeError, match="already running"):
            ledger.claim("run")
        process.terminate()
        process.join(10)
        with ledger.transaction() as conn:
            conn.execute("UPDATE evaluation_execution_owners SET lease_until=0 WHERE scope_id=?", (ledger.scope,))
        assert ledger.claim("run").fencing_token == 2
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)


def test_state_mutation_does_not_lose_cross_process_updates(tmp_path):
    path = tmp_path / "run_state.json"
    store = RunStateStore(path)
    store.create(EvaluationRunState(run_id="run", dataset_path="", snapshot_path="", mode="offline"))
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_increment_in_process, args=(str(path), 15)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(10)
    assert store.load().completed_samples == 45


def test_worker_mode_enqueues_only_on_start_and_independent_worker_consumes(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    release.set()
    controller, state = _controller_run(tmp_path, BlockingCollector(entered, release))
    monkeypatch.setenv("EVAL_EXECUTION_MODE", "worker")
    assert controller.ownership.pending_ids() == []
    assert controller.start(state.run_id) is None
    assert state.run_id in controller.ownership.pending_ids()
    consumer = EvaluationRunController(controller.service_factory, controller.state_root)
    assert run_pending(consumer) == 1
    assert controller.load_for_display(state.run_id).status == "collected"
    assert run_pending(consumer) == 0


def test_content_identity_detects_changed_reference_but_ignores_order():
    first = EvaluationSample(id="a", question="q", reference_answer="old", kb_name="kb")
    second = first.model_copy(update={"id": "b"})
    assert execution_content_hash([first, second]) == execution_content_hash([second, first])
    assert execution_content_hash([first]) != execution_content_hash([first.model_copy(update={"reference_answer": "new"})])


def test_worker_entry_point_starts_and_exits_without_model_calls(tmp_path):
    env = {**os.environ, "HDB_ENV_PATH": os.devnull, "OBS_ENABLED": "false",
           "CONTROL_PLANE_BACKEND": "sqlite", "CONTROL_PLANE_DATABASE_URL": "",
           "EVALUATION_STORE_BACKEND": "sqlite", "EVALUATION_STORE_DATABASE_URL": "",
           "STORAGE_DIR": str(tmp_path), "AUTH_DB_PATH": str(tmp_path / "auth.db")}
    result = subprocess.run(
        [sys.executable, "-m", "src.evaluation.worker", "--root", str(tmp_path / "evaluations"), "--once"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_worker_default_root_matches_api_storage_when_started_from_other_directory(tmp_path):
    from pathlib import Path

    storage = tmp_path / "storage"
    other = tmp_path / "other"
    other.mkdir()
    env = {**os.environ, "HDB_ENV_PATH": os.devnull, "OBS_ENABLED": "false",
           "CONTROL_PLANE_BACKEND": "sqlite", "CONTROL_PLANE_DATABASE_URL": "",
           "EVALUATION_STORE_BACKEND": "sqlite", "EVALUATION_STORE_DATABASE_URL": "",
           "STORAGE_DIR": str(storage), "AUTH_DB_PATH": str(storage / "auth.db"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    env.pop("EVAL_OUTPUT_ROOT", None)
    result = subprocess.run(
        [sys.executable, "-m", "src.evaluation.worker", "--once"],
        cwd=other, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (storage / "evaluations" / ".execution-ledger.db").is_file()
    assert not (other / "storage").exists()


def test_frozen_execution_data_is_validated_inside_worker(tmp_path):
    controller, state = _controller_run(tmp_path, None)
    path = controller._store(state.run_id).load().dataset_path
    from pathlib import Path

    Path(path).write_text(Path(path).read_text().replace('"reference_answer":"answer"', '"reference_answer":"changed"'))
    failed = controller.execute(state.run_id)
    assert failed.status == "failed"
    assert "冻结执行数据集" in failed.error_message


def test_checkpoint_reuse_requires_same_inputs_and_scorer(tmp_path):
    state = EvaluationRunState(
        run_id="run", dataset_path="", snapshot_path="", mode="offline",
        execution_content_sha256="input-v1", snapshot_sha256="snapshot-v1",
        evaluation_config={"llm_model": "judge-a", "scoring_rules_sha256": "rules-v1", "scoring_resume": "continue"},
    )
    summary = EvaluationSummary(
        run_id="run", execution_content_sha256="input-v1", snapshot_sha256="snapshot-v1",
        evaluation_config={"llm_model": "judge-a", "scoring_rules_sha256": "rules-v1"},
    )
    results = [SampleResult(sample_id="q", metrics=[MetricResult(sample_id="q", metric_name="faithfulness", score=0.9)])]
    write_reports(tmp_path / ".checkpoint", summary, results)
    assert EvaluationRunController._validated_checkpoint_metrics(tmp_path, state)["q"][0].score == 0.9
    for update in (
        {"snapshot_sha256": "snapshot-v2"}, {"execution_content_sha256": "input-v2"},
        {"evaluation_config": {"llm_model": "judge-b", "scoring_rules_sha256": "rules-v1"}},
        {"evaluation_config": {"llm_model": "judge-a", "scoring_rules_sha256": "rules-v2"}},
    ):
        with pytest.raises(EvaluationReproducibilityError):
            EvaluationRunController._validated_checkpoint_metrics(tmp_path, state.model_copy(update=update))


def test_copied_thread_context_cannot_inherit_a_held_write_guard(tmp_path):
    from src.observability import thread_with_current_context

    ledger = EvaluationExecutionLedger(tmp_path, backend="sqlite")
    store = RunStateStore(tmp_path / "run_state.json")
    store.create(EvaluationRunState(run_id="run", dataset_path="", snapshot_path="", mode="offline"))
    old = ledger.claim("run")
    errors = []

    def write():
        try:
            store.mark_running()
        except Exception as exc:
            errors.append(exc)

    with execution_context(ledger, old), store._lock:
        worker = thread_with_current_context(write)
    with ledger.transaction() as conn:
        conn.execute("UPDATE evaluation_execution_owners SET lease_until=0 WHERE scope_id=?", (ledger.scope,))
    ledger.claim("run")
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], EvaluationLeaseLost)
    assert store.load().status == "queued"


def test_completed_run_cannot_be_reexecuted_or_replaced(tmp_path):
    controller, state = _controller_run(tmp_path, None)
    store = controller._store(state.run_id)
    store.mutate(lambda current: current.model_copy(update={"status": "completed"}))
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="not executable"):
        controller.execute(state.run_id)
    assert store.path.read_bytes() == before


def test_unknown_schema_version_fails_closed(tmp_path):
    ledger = EvaluationExecutionLedger(tmp_path, backend="sqlite")
    with ledger.transaction() as conn:
        conn.execute("UPDATE evaluation_execution_schema SET version=999")
    with pytest.raises(RuntimeError, match="schema version"):
        EvaluationExecutionLedger(tmp_path, backend="sqlite")


@pytest.mark.skipif(not os.getenv("HDB_TEST_POSTGRES_DSN"), reason="HDB_TEST_POSTGRES_DSN not configured")
def test_postgres_claim_and_fencing(tmp_path):
    ledger = EvaluationExecutionLedger(tmp_path, backend="postgres", dsn=os.environ["HDB_TEST_POSTGRES_DSN"], initialize=True)
    try:
        old = ledger.claim("run")
        with pytest.raises(RuntimeError, match="already running"):
            ledger.claim("run")
        with ledger.transaction() as conn:
            conn.execute("UPDATE evaluation_execution_owners SET lease_until=0 WHERE scope_id=?", (ledger.scope,))
        current = ledger.claim("run")
        with pytest.raises(EvaluationLeaseLost), ledger.fenced(old):
            pass
        ledger.release(old)
        assert ledger.heartbeat(current)
    finally:
        with ledger.transaction() as conn:
            conn.execute("DELETE FROM evaluation_execution_owners WHERE scope_id=?", (ledger.scope,))
