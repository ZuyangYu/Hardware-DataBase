import json

import pytest

from src.api.routes import evaluation
from src.api.schemas import CreateEvaluationRunRequest
from src.core.auth import AuthUser, KnowledgeBaseSummary
from src.evaluation.run_control import EvaluationRunController
from src.evaluation.schemas import EvaluationRunState, EvaluationSample
from src.evaluation.history import cohort_fingerprint


def test_load_sample_results_serializes_valid_results(tmp_path):
    (tmp_path / "results.jsonl").write_text(
        json.dumps(
            {
                "sample_id": "case-1",
                "question": "Q",
                "reference_answer": "R",
                "response": "A",
                "retrieved_contexts": ["context"],
                "critical": True,
                "metrics": [
                    {"sample_id": "case-1", "metric_name": "faithfulness", "score": 0.8}
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rows, error = evaluation._load_sample_results(tmp_path)

    assert error == ""
    assert rows == [
        {
            "sample_id": "case-1",
            "question": "Q",
            "reference_answer": "R",
            "response": "A",
            "scored_response": "",
            "retrieved_contexts": ["context"],
            "critical": True,
            "snapshot_status": "success",
            "metrics": [
                {
                    "sample_id": "case-1",
                    "metric_name": "faithfulness",
                    "score": 0.8,
                    "status": "success",
                    "reason": "",
                    "details": {},
                }
            ],
            "metadata": {},
        }
    ]


def test_load_sample_results_returns_safe_empty_data_for_missing_or_invalid_artifact(tmp_path):
    assert evaluation._load_sample_results(tmp_path) == ([], "")
    (tmp_path / "results.jsonl").write_text('{"sample_id":\n', encoding="utf-8")

    rows, error = evaluation._load_sample_results(tmp_path)

    assert rows == []
    assert "样本诊断不可用" in error


def test_get_run_keeps_summary_when_sample_diagnostics_are_unavailable(tmp_path, monkeypatch):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    state = EvaluationRunState.new_online(
        run_id="run-1",
        dataset_path="dataset.jsonl",
        snapshot_path="",
        total_samples=1,
    ).model_copy(update={"status": "completed"})
    (run_dir / "run_state.json").write_text(state.model_dump_json(), encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps({"run_id": "run-1", "sample_count": 1}), encoding="utf-8"
    )
    (run_dir / "results.jsonl").write_text('{"sample_id":\n', encoding="utf-8")

    class Controller:
        def load_for_display(self, run_id):
            assert run_id == "run-1"
            return state

    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path))
    monkeypatch.setattr(evaluation, "_controller", lambda _output_root: Controller())

    payload = evaluation.get_run("run-1", output_root=str(tmp_path))

    assert payload["summary"] == {
        "run_id": "run-1",
        "created_at": payload["summary"]["created_at"],
        "sample_count": 1,
        "successful_samples": 0,
        "failed_samples": 0,
        "metric_scores": {},
        "metric_counts": {},
        "metric_failures": {},
        "scoring_completed_items": 0,
        "scoring_total_items": 0,
        "gate": None,
        "metadata": {},
    }
    assert payload["sample_results"] == []
    assert "样本诊断不可用" in payload["sample_results_error"]


def test_get_run_reads_incremental_diagnostics_while_scoring(tmp_path, monkeypatch):
    state = EvaluationRunState.new_online(
        run_id="run-1",
        dataset_path="dataset.jsonl",
        snapshot_path="",
        total_samples=1,
    ).model_copy(update={"status": "running"})
    checkpoint = tmp_path / "run-1" / ".checkpoint"
    checkpoint.mkdir(parents=True)
    (checkpoint / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "sample_count": 1,
                "scoring_completed_items": 1,
                "scoring_total_items": 5,
            }
        ),
        encoding="utf-8",
    )
    (checkpoint / "results.jsonl").write_text(
        json.dumps(
            {
                "sample_id": "case-1",
                "question": "Q",
                "reference_answer": "R",
                "response": "A",
                "retrieved_contexts": ["context"],
                "metrics": [
                    {"sample_id": "case-1", "metric_name": "faithfulness", "score": 0.8}
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class Controller:
        def load_for_display(self, run_id):
            assert run_id == "run-1"
            return state

    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path))
    monkeypatch.setattr(evaluation, "_controller", lambda _output_root: Controller())
    payload = evaluation.get_run("run-1", output_root=str(tmp_path))

    assert payload["summary"]["scoring_completed_items"] == 1
    assert payload["summary"]["scoring_total_items"] == 5
    assert payload["sample_results"][0]["sample_id"] == "case-1"
    assert payload["sample_results_error"] == ""


def test_delete_run_removes_failed_run_artifacts(tmp_path, monkeypatch):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    state = EvaluationRunState.new_online(
        run_id="run-1",
        dataset_path="dataset.jsonl",
        snapshot_path="",
        total_samples=1,
    ).model_copy(update={"status": "failed"})
    (run_dir / "run_state.json").write_text(state.model_dump_json(), encoding="utf-8")
    (run_dir / "summary.json").write_text("partial report", encoding="utf-8")

    controller = EvaluationRunController(lambda: object(), tmp_path)
    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path))
    monkeypatch.setattr(evaluation, "_controller", lambda _output_root: controller)

    payload = evaluation.delete_run("run-1", output_root=str(tmp_path))

    assert payload == {"ok": True, "run_id": "run-1", "status": "failed"}
    assert not run_dir.exists()


def _evaluation_admin() -> AuthUser:
    return AuthUser(
        id=1,
        username="system-admin",
        role="system_admin",
        is_active=True,
        department_id=None,
    )


def _sample(sample_id: str = "case-1") -> EvaluationSample:
    return EvaluationSample(
        id=sample_id,
        question="Q",
        reference_answer="A",
        kb_name="shared",
        request_context={"allowed_kbs": ["999:shared"], "kb_permissions": {"999:shared": "admin"}},
    )


class _EvaluationPipeline:
    def __init__(self, existing=None):
        self.existing = existing or ["shared"]

    def list_all_knowledge_bases_for_admin(self, ctx=None):
        return list(self.existing)


class _EvaluationAuth:
    def __init__(self, summaries):
        self.summaries = summaries

    def list_knowledge_base_summaries(self, existing):
        return list(self.summaries)


def test_list_evaluation_knowledge_bases_exposes_registered_stable_id_rows():
    rows = evaluation.list_evaluation_knowledge_bases(
        _actor=_evaluation_admin(),
        pipeline=_EvaluationPipeline(),
        auth=_EvaluationAuth(
            [
                KnowledgeBaseSummary(
                    name="shared",
                    kb_id=7,
                    department_id=47,
                    department_name="硬件部",
                    registered=True,
                    physical_exists=True,
                ),
                KnowledgeBaseSummary(name="orphan", registered=False, physical_exists=True),
            ]
        ),
    )

    assert [row.kb_id for row in rows] == [7]
    assert rows[0].kb_name == "shared"
    assert rows[0].department_name == "硬件部"


def test_preflight_reports_selected_kb_counts_and_normalizes_dataset_scope(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(_sample().model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path / "runs"))
    monkeypatch.setattr(evaluation, "_check_dataset_path", lambda _value, _root: str(dataset))

    response = evaluation.preflight_run(
        CreateEvaluationRunRequest(
            dataset_path=str(dataset),
            kb_id=7,
            kb_name="shared",
        ),
        output_root="storage/evaluations",
        _actor=_evaluation_admin(),
        auth=_EvaluationAuth(
            [KnowledgeBaseSummary(name="shared", kb_id=7, department_id=47, registered=True)]
        ),
        pipeline=_EvaluationPipeline(),
    )

    assert response.can_create is True
    assert response.dataset_total_count == 1
    assert response.matched_sample_count == 1
    assert response.dataset_sample_count == 1
    assert response.normal_sample_count == 1
    assert response.kb_id == 7
    assert response.cohort_fingerprint


def test_create_run_uses_selected_kb_and_run_local_normalized_samples(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(_sample().model_dump_json() + "\n", encoding="utf-8")
    output_root = tmp_path / "runs"
    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(output_root))
    monkeypatch.setattr(evaluation, "_check_dataset_path", lambda _value, _root: str(dataset))

    captured = {}

    class Controller:
        def create_online_run(self, **kwargs):
            captured.update(kwargs)
            return EvaluationRunState.new_online(
                run_id="run-1",
                dataset_path=str(tmp_path / "runs" / "run-1" / "execution_dataset.jsonl"),
                snapshot_path=str(tmp_path / "runs" / "run-1" / "snapshot.jsonl"),
                total_samples=len(kwargs["samples"]),
                metadata={
                    "kb_id": kwargs["kb_id"],
                    "kb_name": kwargs["kb_name"],
                    "department_id": kwargs["department_id"],
                },
            )

    monkeypatch.setattr(evaluation, "_controller", lambda _root: Controller())
    state = evaluation.create_run(
        CreateEvaluationRunRequest(
            dataset_path=str(dataset),
            kb_id=7,
            kb_name="shared",
        ),
        output_root="storage/evaluations",
        _actor=_evaluation_admin(),
        auth=_EvaluationAuth(
            [KnowledgeBaseSummary(name="shared", kb_id=7, department_id=47, registered=True)]
        ),
        pipeline=_EvaluationPipeline(),
    )

    assert state["kb_id"] == 7
    assert captured["kb_id"] == 7
    assert captured["department_id"] == 47
    assert captured["samples"][0].request_context["allowed_kbs"] == ["47:shared"]
    assert captured["samples"][0].request_context["kb_permissions"] == {"47:shared": "read"}


def test_create_run_rejects_kb_id_and_name_mismatch(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(_sample().model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path))
    monkeypatch.setattr(evaluation, "_check_dataset_path", lambda _value, _root: str(dataset))

    with pytest.raises(evaluation.HTTPException) as exc_info:
        evaluation.create_run(
            CreateEvaluationRunRequest(
                dataset_path=str(dataset), kb_id=7, kb_name="other"
            ),
            output_root="storage/evaluations",
            _actor=_evaluation_admin(),
            auth=_EvaluationAuth(
                [KnowledgeBaseSummary(name="shared", kb_id=7, department_id=47, registered=True)]
            ),
            pipeline=_EvaluationPipeline(),
        )

    assert exc_info.value.status_code == 400
    assert "不匹配" in str(exc_info.value.detail)


def test_start_run_rejects_modified_run_local_execution_dataset(tmp_path, monkeypatch):
    execution_dataset = tmp_path / "run-1" / "execution_dataset.jsonl"
    execution_dataset.parent.mkdir()
    execution_dataset.write_text(_sample().model_dump_json() + "\n", encoding="utf-8")
    import hashlib

    state = EvaluationRunState.new_online(
        run_id="run-1",
        dataset_path=str(execution_dataset),
        snapshot_path=str(tmp_path / "run-1" / "snapshot.jsonl"),
        total_samples=1,
        metadata={
            "kb_id": 7,
            "kb_name": "shared",
            "department_id": 47,
            "execution_dataset_sha256": hashlib.sha256(execution_dataset.read_bytes()).hexdigest(),
            "cohort_fingerprint": "not-used-by-this-check",
        },
    )
    (execution_dataset.parent / "run_state.json").write_text(state.model_dump_json(), encoding="utf-8")
    execution_dataset.write_text("changed\n", encoding="utf-8")

    class Controller:
        started = False

        def start(self, _run_id):
            self.started = True

    controller = Controller()
    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path))
    monkeypatch.setattr(evaluation, "_controller", lambda _root: controller)

    with pytest.raises(evaluation.HTTPException) as exc_info:
        evaluation.start_run(
            "run-1",
            output_root="storage/evaluations",
            _actor=_evaluation_admin(),
            auth=_EvaluationAuth(
                [KnowledgeBaseSummary(name="shared", kb_id=7, department_id=47, registered=True)]
            ),
            pipeline=_EvaluationPipeline(),
        )

    assert exc_info.value.status_code == 409
    assert "副本" in str(exc_info.value.detail) or "哈希" in str(exc_info.value.detail)
    assert controller.started is False


def test_start_run_revalidates_filtered_cohort_using_saved_filters(tmp_path, monkeypatch):
    execution_dataset = tmp_path / "run-1" / "execution_dataset.jsonl"
    execution_dataset.parent.mkdir()
    execution_dataset.write_text(
        "\n".join(_sample(sample_id).model_dump_json() for sample_id in ("case-1", "case-2"))
        + "\n",
        encoding="utf-8",
    )
    import hashlib

    state = EvaluationRunState.new_online(
        run_id="run-1",
        dataset_path=str(execution_dataset),
        snapshot_path=str(tmp_path / "run-1" / "snapshot.jsonl"),
        total_samples=1,
        sample_ids=["case-1"],
        metadata={
            "kb_id": 7,
            "kb_name": "shared",
            "department_id": 47,
            "dataset_sample_count": 1,
            "execution_dataset_sha256": hashlib.sha256(execution_dataset.read_bytes()).hexdigest(),
            "cohort_fingerprint": cohort_fingerprint(["case-1"]),
        },
    )
    (execution_dataset.parent / "run_state.json").write_text(state.model_dump_json(), encoding="utf-8")

    class Controller:
        started = False

        def start(self, _run_id):
            self.started = True

    controller = Controller()
    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path))
    monkeypatch.setattr(evaluation, "_controller", lambda _root: controller)

    result = evaluation.start_run(
        "run-1",
        output_root="storage/evaluations",
        _actor=_evaluation_admin(),
        auth=_EvaluationAuth(
            [KnowledgeBaseSummary(name="shared", kb_id=7, department_id=47, registered=True)]
        ),
        pipeline=_EvaluationPipeline(),
    )

    assert result == {"ok": True, "run_id": "run-1"}
    assert controller.started is True


def _write_comparison_run(root, run_id: str, *, kb_id: int = 7, llm_model: str = "judge-a"):
    run_dir = root / run_id
    run_dir.mkdir()
    summary = {
        "run_id": run_id,
        "sample_count": 1,
        "successful_samples": 1,
        "kb_id": kb_id,
        "kb_name": "shared",
        "department_id": 47,
        "cohort_fingerprint": cohort_fingerprint(["case-1"]),
        "metric_scores": {"faithfulness": 0.8},
        "llm_model": llm_model,
        "embedding_model": "embed-a",
        "evaluation_config": {"llm_model": llm_model, "embedding_model": "embed-a"},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "results.jsonl").write_text(json.dumps({"sample_id": "case-1"}) + "\n", encoding="utf-8")


def test_compare_strict_rejects_incompatible_runs_and_relaxed_mode_warns(tmp_path, monkeypatch):
    _write_comparison_run(tmp_path, "current")
    _write_comparison_run(tmp_path, "baseline", kb_id=8)
    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path))

    with pytest.raises(evaluation.HTTPException) as exc_info:
        evaluation.compare_run(
            "current",
            baseline="baseline",
            output_root="storage/evaluations",
            strict=True,
            _actor=_evaluation_admin(),
        )
    assert exc_info.value.status_code == 409
    assert "kb_id" in str(exc_info.value.detail)

    result = evaluation.compare_run(
        "current",
        baseline="baseline",
        output_root="storage/evaluations",
        strict=False,
        _actor=_evaluation_admin(),
    )
    assert result.compatible is False
    assert result.strict is False
    assert result.warnings
