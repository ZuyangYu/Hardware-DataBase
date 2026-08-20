import json

from src.api.routes import evaluation
from src.evaluation.run_control import EvaluationRunController
from src.evaluation.schemas import EvaluationRunState


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
        score_enabled=True,
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
        score_enabled=True,
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
        score_enabled=True,
    ).model_copy(update={"status": "failed"})
    (run_dir / "run_state.json").write_text(state.model_dump_json(), encoding="utf-8")
    (run_dir / "summary.json").write_text("partial report", encoding="utf-8")

    controller = EvaluationRunController(lambda: object(), tmp_path)
    monkeypatch.setattr(evaluation, "_check_output_root", lambda _value: str(tmp_path))
    monkeypatch.setattr(evaluation, "_controller", lambda _output_root: controller)

    payload = evaluation.delete_run("run-1", output_root=str(tmp_path))

    assert payload == {"ok": True, "run_id": "run-1", "status": "failed"}
    assert not run_dir.exists()
