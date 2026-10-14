from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.evaluation.history import cohort_fingerprint
from src.evaluation.reproducibility import execution_content_hash
from src.evaluation.schemas import EvaluationSample, EvaluationSummary, GateResult, MetricResult, SampleResult
from src.quality.schemas import ReviewRequest
from src.quality.store import QualityConflict
from src.quality.workflow import ChangeRequest, DatasetCase, FreezeDatasetRequest, WorkflowStore
from tests.quality.test_feedback import feedback, quality as quality_fixture, share  # noqa: F401


def prepare(q):
    from src.costs.store import CostStore

    CostStore(initialize=True)
    case_id = feedback(q)["case_id"]
    share(q, case_id)
    row = q.store.get_case(case_id, q.reviewer)
    review = q.store.submit_review(q.reviewer, case_id, ReviewRequest(row_version=row["row_version"], client_request_id="gold-review", judgment="confirmed", correctness="incorrect", reference_answer="Fixed correct reference"))
    workflow = WorkflowStore(q.store.db_path if q.store.backend == "sqlite" else None, initialize=True)
    frozen = workflow.freeze("production", q.reviewer, FreezeDatasetRequest(client_request_id="freeze", cases=[DatasetCase(case_id=case_id, review_id=review["review_id"], critical=True)]))
    return workflow, case_id, frozen["version_id"]


def test_dataset_version_materialization_and_revocation(quality):
    q = quality
    workflow, case_id, version_id = prepare(q)
    material = workflow.version(version_id, q.reviewer, materialize=True)
    assert material["samples"][0]["question"] == "Original question"
    assert material["samples"][0]["reference_answer"] == "Fixed correct reference"
    assert material["snapshots"][0]["response"] == "Original answer"
    assert workflow.version(version_id, q.owner, materialize=True)["samples"] == material["samples"]
    assert "Original answer" not in json.dumps(workflow.version(version_id, q.admin))
    with pytest.raises(PermissionError):
        workflow.version(version_id, q.admin, materialize=True)
    owner = q.store.get_case(case_id, q.owner)
    q.store.change_share(q.owner, case_id, row_version=owner["row_version"])
    with pytest.raises(PermissionError):
        workflow.version(version_id, q.reviewer, materialize=True)
    assert workflow.version(version_id, q.admin)["manifest_sha256"] == material["manifest_sha256"]


def test_superseded_review_cannot_supply_gold_reference(quality):
    q = quality
    workflow, case_id, version_id = prepare(q)
    row = q.store.get_case(case_id, q.reviewer)
    q.store.submit_review(q.reviewer, case_id, ReviewRequest(row_version=row["row_version"], client_request_id="superseded", judgment="needs_information", supersedes=row["reviews"][0]["review_id"]))
    with pytest.raises(QualityConflict, match="superseded"):
        workflow.version(version_id, q.reviewer, materialize=True)


def save_report(root, name, material, *, pass_gate=True, digest=None):
    run = root / name
    run.mkdir()
    sample = material["samples"][0]
    result = SampleResult(sample_id=sample["id"], question=sample["question"], reference_answer=sample["reference_answer"], critical=sample["critical"], metrics=[MetricResult(sample_id=sample["id"], metric_name="answer_correctness", score=1)])
    summary = EvaluationSummary(run_id=name, sample_count=1, successful_samples=1, scoring_completed_items=1, scoring_total_items=1,
        metric_scores={"answer_correctness": 1}, metric_counts={"answer_correctness": 1}, gate=GateResult(passed=pass_gate, verdict="pass" if pass_gate else "insufficient_evaluation"),
        execution_content_sha256=digest or execution_content_hash([EvaluationSample.model_validate(sample)]), kb_id=10,
        snapshot_ownership_verified=True, cohort_fingerprint=cohort_fingerprint([sample["id"]]),
        evaluation_config={"llm_model": "fixture-judge", "scoring_rules_sha256": "rules-v1"}, metadata={"run_outcome": {"kind": "completed"}})
    (run / 'summary.json').write_text(summary.model_dump_json())
    (run / 'results.jsonl').write_text(result.model_dump_json() + '\n')


def test_release_links_require_actual_compatible_inputs_and_gate(quality, tmp_path):
    q = quality
    workflow, case_id, version_id = prepare(q)
    material = workflow.version(version_id, q.reviewer, materialize=True)
    save_report(tmp_path, "baseline", material)
    save_report(tmp_path, "candidate", material, pass_gate=False)
    version = q.store.get_case(case_id, q.reviewer)["row_version"]
    request = ChangeRequest(row_version=version, dataset_version_id=version_id, change_ref="commit:fixture", baseline_run_id="baseline", candidate_run_id="candidate")
    with pytest.raises(QualityConflict, match="not passed"):
        workflow.link_change(case_id, q.reviewer, request, evaluation_root=tmp_path)
    assert q.store.get_case(case_id, q.reviewer)["status"] == "reviewed"
    save_report(tmp_path, "candidate-passed", material)
    result = workflow.link_change(case_id, q.reviewer, request.model_copy(update={"candidate_run_id": "candidate-passed"}), evaluation_root=tmp_path)
    assert result["status"] == "regression_verified"
    assert q.store.get_case(case_id, q.reviewer)["status"] == "regression_verified"
    save_report(tmp_path, "candidate-different-input", material, digest="different")
    with pytest.raises(QualityConflict, match="compatible"):
        workflow.link_change(case_id, q.reviewer, request.model_copy(update={"candidate_run_id": "candidate-different-input"}), evaluation_root=tmp_path)
    assert len(workflow.changes(case_id, q.reviewer)) == 1


def test_analysis_is_frozen_code_statistics_scoped_and_idempotent(quality):
    q = quality
    workflow, case_id, _version = prepare(q)
    cost_summary = {"scope": "department_and_owned_tasks", "calls": 2, "unknown_cost_calls": 2, "usage_coverage": 0, "price_coverage": 0}
    costs = SimpleNamespace(summary=lambda _: cost_summary)
    report = workflow.analyze(q.reviewer, "daily-report", costs)
    assert workflow.analyze(q.reviewer, "daily-report", costs)["deduplicated"]
    frozen = workflow.reports(q.reviewer, report["report_id"])[0]
    assert frozen["verified_statistics"]["case_count"] == 1
    assert frozen["verified_statistics"]["case_trigger_counts"] == {"feedback": 1}
    assert frozen["reporter"] == "deterministic_code"
    assert "Original question" not in json.dumps(frozen)
    assert "Private feedback" not in json.dumps(frozen)
    with pytest.raises(KeyError):
        workflow.reports(q.other, report["report_id"])
    q.reviewer.department_id = 20
    with pytest.raises(KeyError):
        workflow.reports(q.reviewer, report["report_id"])
    q.reviewer.department_id = 10
    q.store.change_share(q.owner, case_id, row_version=q.store.get_case(case_id, q.owner)["row_version"])
    assert workflow.reports(q.reviewer, report["report_id"])[0]["input_manifest_sha256"] == frozen["input_manifest_sha256"]


def test_case_closes_only_after_linked_revision_is_deployed_and_verified(quality, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import src.settings as settings

    q = quality
    workflow, case_id, version_id = prepare(q)
    material = workflow.version(version_id, q.owner, materialize=True)
    save_report(tmp_path, "baseline", material)
    save_report(tmp_path, "candidate", material)
    deployed = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(settings, "OBS_SERVICE_VERSION", "fixture-release-revision")
    session = q.conv.create_session(q.owner.id, "kb", department_id=10)
    validation = q.conv.create_turn(q.owner.id, session.id, "Original question")
    q.conv.complete_turn(q.owner.id, validation.id, "Fixed correct reference", {})
    state = q.store.feedback_state(q.owner.id, validation.id)
    from src.quality.schemas import FeedbackRequest

    event = q.store.submit_feedback(q.owner, validation.id, FeedbackRequest(assistant_message_id=validation.assistant_message_id, answer_revision=state["answer_revision"], client_request_id="validation-feedback", rating="helpful"))
    validation_case = q.store.get_case(event["case_id"], q.owner)
    q.store.submit_review(q.owner, event["case_id"], ReviewRequest(row_version=validation_case["row_version"], client_request_id="validation-review", judgment="not_an_error", correctness="correct"))
    request = ChangeRequest(row_version=q.store.get_case(case_id, q.owner)["row_version"], dataset_version_id=version_id,
                            change_ref="commit:fixture-release-revision", baseline_run_id="baseline", candidate_run_id="candidate",
                            release_ref="wrong-revision", deployed_at=deployed, validation_turn_id=validation.id)
    with pytest.raises(QualityConflict, match="release revision"):
        workflow.link_change(case_id, q.owner, request, evaluation_root=tmp_path)
    assert q.store.get_case(case_id, q.owner)["status"] == "reviewed"
    closed = workflow.link_change(case_id, q.owner, request.model_copy(update={"release_ref": "fixture-release-revision"}), evaluation_root=tmp_path)
    assert closed["status"] == "closed"
    assert q.store.get_case(case_id, q.owner)["status"] == "closed"


def test_governed_evaluation_bridge_blocks_admin_revocation_and_cleans_derivatives(quality, tmp_path, monkeypatch):
    from src.evaluation.run_control import EvaluationRunController
    from src.evaluation.reproducibility import EvaluationReproducibilityError
    from src.quality.evaluation_bridge import DatasetEvaluationRequest, create_dataset_run, reconcile_invalid_runs, validate_binding
    from src.api.routes.evaluation import _run_dir
    from fastapi import HTTPException
    import src.settings as settings

    q = quality
    workflow, case_id, version_id = prepare(q)
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    controller = EvaluationRunController(lambda: None, tmp_path / "runs")
    auth = SimpleNamespace(get_user_by_id=lambda user_id: q.reviewer if user_id == q.reviewer.id else q.owner,
                           get_knowledge_base_id=lambda *_, **__: 10, get_kb_permissions_for_user=lambda _: {"10:kb": "read"})
    state = create_dataset_run(workflow, version_id, q.reviewer, DatasetEvaluationRequest(), controller, auth=auth)
    (controller.state_root/state.run_id/'review-notes.txt').write_text('User-owned non-runtime notes')
    assert state.quality_binding["dataset_version_id"] == version_id
    assert validate_binding(state, workflow=workflow, auth=auth)["version_id"] == version_id
    assert (tmp_path / "runs" / state.run_id / "production-snapshot.jsonl").is_file()
    with pytest.raises(HTTPException) as forbidden:
        _run_dir(str(controller.state_root), state.run_id)
    assert forbidden.value.status_code == 403
    with pytest.raises(PermissionError, match="original answer owner"):
        create_dataset_run(workflow, version_id, q.reviewer, DatasetEvaluationRequest(mode="online"), controller, auth=auth)
    assert not list((tmp_path / "storage" / "quality-evaluation-inputs").iterdir())
    q.store.change_share(q.owner, case_id, row_version=q.store.get_case(case_id, q.owner)["row_version"])
    with pytest.raises(EvaluationReproducibilityError):
        validate_binding(state, workflow=workflow, auth=auth)
    assert reconcile_invalid_runs(controller, workflow=workflow, auth=auth) == 1
    assert {p.name for p in (controller.state_root / state.run_id).iterdir()} == {"run_state.json", "run_state.lock", "review-notes.txt"}
    assert controller._store(state.run_id).load().status == "failed"


@pytest.mark.parametrize("permissions", [{"20:kb": "admin"}, {"kb": "admin"}, {"10:kb": "unknown"}])
def test_governed_evaluation_requires_valid_permission_in_dataset_department(quality, tmp_path, monkeypatch, permissions):
    from src.evaluation.run_control import EvaluationRunController
    from src.quality.evaluation_bridge import DatasetEvaluationRequest, create_dataset_run
    import src.settings as settings

    q = quality
    workflow, _case_id, version_id = prepare(q)
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    controller = EvaluationRunController(lambda: None, tmp_path / "runs")
    auth = SimpleNamespace(get_knowledge_base_id=lambda *_, **__: 10,
                           get_kb_permissions_for_user=lambda _: permissions)
    with pytest.raises(PermissionError, match="knowledge base is not currently accessible"):
        create_dataset_run(workflow, version_id, q.owner, DatasetEvaluationRequest(), controller, auth=auth)
    assert not (tmp_path / "storage" / "quality-evaluation-inputs").exists()


def test_fixed_evidence_generation_never_uses_reference_or_latest_kb(monkeypatch):
    from src.quality.evaluation_bridge import FixedEvidenceRunner
    from src.core import chat_model_runtime, model_factory
    from src.quality import evaluation_bridge

    inputs = []
    monkeypatch.setattr(evaluation_bridge, "generator_identity", lambda: {"model": "fixture"})
    monkeypatch.setattr(model_factory, "create_chat_model", lambda **_: object())
    def invoke(_model, messages, **_):
        inputs.extend(messages)
        return SimpleNamespace(text="Candidate answer")
    monkeypatch.setattr(chat_model_runtime, "invoke_text", invoke)
    sample = EvaluationSample(id="case", question="Question", kb_name="kb", reference_answer="Private gold reference", reference_contexts=["Frozen source"])
    result = FixedEvidenceRunner({"generator_identity": {"model": "fixture"}, "system_prompt": "Fixture prompt", "dataset_version_id": "dataset-fixture"}).collect(sample)
    assert result.retrieved_contexts == ["Frozen source"]
    assert "Frozen source" in str(inputs)
    assert "Private gold reference" not in str(inputs)
    assert result.metadata["source_policy"] == "fixed_retrieved_evidence"


def test_governed_runs_execute_through_leased_worker_and_pair_actual_reports(quality, tmp_path, monkeypatch):
    import src.settings as settings
    from src.core import chat_model_runtime, model_factory
    from src.evaluation.run_control import EvaluationRunController
    from src.evaluation.service import EvaluationService
    from src.evaluation.worker import run_pending
    from src.evaluation.history import load_history_run
    from src.quality import evaluation_bridge as bridge
    from src.costs.context import current_context

    q = quality
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVAL_EXECUTION_MODE", "worker")
    workflow, case_id, version_id = prepare(q)
    auth = SimpleNamespace(get_user_by_id=lambda uid: q.reviewer if uid == q.reviewer.id else q.owner,
                           get_knowledge_base_id=lambda *_, **__: 10, get_kb_permissions_for_user=lambda _: {"10:kb": "read"})
    monkeypatch.setattr(bridge, "AuthService", lambda: auth)
    monkeypatch.setattr(bridge, "generator_identity", lambda: {"model": "fixture"})
    monkeypatch.setattr(model_factory, "create_chat_model", lambda **_: object())
    prompts, contexts = [], []
    def invoke(_model, messages, **_):
        prompts.extend(messages)
        contexts.append(current_context())
        return SimpleNamespace(text="Fixed correct reference")
    monkeypatch.setattr(chat_model_runtime, "invoke_text", invoke)

    class Judge:
        def score(self, samples, snapshots, metrics, on_result=None):
            contexts.append(current_context())
            results = [MetricResult(sample_id=s.id, metric_name=name,
                                    score=1 if snapshots[index].response == s.reference_answer else 0)
                       for index, s in enumerate(samples) for name in metrics]
            if on_result:
                for result in results:
                    on_result(result)
            return results
    config = SimpleNamespace(public_metadata=lambda: {"llm_model": "fixture-judge"}, max_workers=1)
    controller = EvaluationRunController(lambda: EvaluationService(ragas_adapter=Judge(), config=config), tmp_path / "runs")
    baseline = bridge.create_dataset_run(workflow, version_id, q.owner, bridge.DatasetEvaluationRequest(), controller, auth=auth)
    controller.start(baseline.run_id)
    assert run_pending(controller) == 1
    assert controller._store(baseline.run_id).load().status == "completed"
    candidate = bridge.create_dataset_run(workflow, version_id, q.owner, bridge.DatasetEvaluationRequest(mode="online"), controller, auth=auth)
    controller.start(candidate.run_id)
    assert run_pending(controller) == 1
    assert controller._store(candidate.run_id).load().status == "collected"
    controller.start_scoring(candidate.run_id)
    assert run_pending(controller) == 1
    assert controller._store(candidate.run_id).load().status == "completed"
    assert "Frozen evidence" in str(prompts) and "Fixed correct reference" not in str(prompts)
    assert all(c["execution_attempt_id"] and c["owner_user_id"] == q.owner.id for c in contexts)
    assert {c["task_id"] for c in contexts} == {baseline.run_id, candidate.run_id}
    request = ChangeRequest(row_version=q.store.get_case(case_id, q.owner)["row_version"], dataset_version_id=version_id,
                            change_ref="fixture:prompt-repair", baseline_run_id=baseline.run_id, candidate_run_id=candidate.run_id)
    assert workflow.link_change(case_id, q.owner, request, evaluation_root=controller.state_root)["status"] == "regression_verified"
    history = load_history_run(controller.state_root / candidate.run_id)
    assert history.summary.gate.verdict == "pass"
    assert not load_history_run(controller.state_root / baseline.run_id).summary.gate.passed
    paired = workflow.changes(case_id, q.owner)[0]["event"]["paired_statistics"]
    assert paired["metric_delta_means"]["answer_correctness"]["mean"] == 1
    assert paired["cohort_size"] == 1


def test_invalidated_old_evaluation_owner_cannot_purge_successor_material(quality, tmp_path):
    from src.evaluation.run_control import EvaluationRunController
    from src.evaluation.execution import EvaluationLeaseLost, execution_context
    from src.quality.evaluation_bridge import purge_run_material

    controller = EvaluationRunController(lambda: None, tmp_path / "runs")
    dataset = tmp_path / "dataset.jsonl"
    sample = EvaluationSample(id="case", question="Private input", reference_answer="Private reference", kb_name="kb")
    dataset.write_text(sample.model_dump_json() + '\n')
    state = controller.create_online_run(dataset, tmp_path / "runs", [sample], quality_binding={"dataset_version_id": "fixture"})
    old = controller.ownership.claim(state.run_id)
    with controller.ownership.transaction() as conn:
        conn.execute("UPDATE evaluation_execution_owners SET lease_until=0 WHERE run_id=?", (state.run_id,))
    current = controller.ownership.claim(state.run_id)
    assert current.fencing_token == old.fencing_token + 1
    with execution_context(controller.ownership, old), pytest.raises(EvaluationLeaseLost):
        purge_run_material(controller.state_root / state.run_id, state)
    assert (controller.state_root / state.run_id / "execution_dataset.jsonl").is_file()
    assert controller._store(state.run_id).load().status == "queued"
    controller.ownership.release(current)
