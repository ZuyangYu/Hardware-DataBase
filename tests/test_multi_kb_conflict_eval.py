"""The fixed conflict gate exercises raw text and durable authoring state."""

import copy
import json
from pathlib import Path

import pytest

import src.settings as settings
from scripts.production_hardening import multi_kb_conflict_eval as evaluation


def case_fixture():
    return {
        "id": "internal-voltage-conflict", "category": "internal_conflict",
        "origin": "synthetic", "review": {"status": "pending", "reviewer": None},
        "purpose": "compose", "unit_label": "输入电压",
        "sources": [{"kb_id": 1, "role": "primary"}],
        "evidence": [
            {"id": "a", "kb_id": 1, "content": "输入电压5V。", "approval_status": "approved"},
            {"id": "b", "kb_id": 1, "content": "输入电压12V。", "approval_status": "approved"},
        ],
        "expected": {
            "claim_count": 2, "retained_evidence_ids": ["a", "b"],
            "groups": [{
                "field_key": "operating_voltage", "kind": "technical_fact_conflict",
                "severity": "S2", "status": "open", "blocking": True, "action": None,
                "evidence_ids": ["a", "b"], "selected_evidence_ids": [],
            }],
        },
    }


def test_raw_conflicting_text_reaches_persisted_review_and_writer_block(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED", True)
    report = evaluation.evaluate([case_fixture()])

    assert report["verdict"] == "pass"
    assert report["release_ready"] is False
    observed = report["cases"][0]["observed"]
    assert observed["groups"] == case_fixture()["expected"]["groups"]
    assert observed["provenance_valid"] is True
    assert len(observed["claims"]) == 2
    assert report["summary"]["critical_block_misses"] == 0


def test_gate_rejects_a_missed_critical_conflict_even_when_extraction_count_matches():
    observed = {"claim_count": 2, "retained_evidence_ids": ["a", "b"],
                "groups": [], "provenance_valid": True}
    failures = evaluation.score_case(case_fixture(), observed, auto_resolve=False)

    assert any("groups" in failure for failure in failures)


@pytest.mark.parametrize("mutation", ["foreign_evidence", "lost_provenance", "silent_no_extraction"])
def test_gate_detects_wrong_source_identity_or_silent_extraction_failure(mutation):
    observed = {**copy.deepcopy(case_fixture()["expected"]), "provenance_valid": True}
    if mutation == "foreign_evidence":
        observed["groups"][0]["evidence_ids"] = ["a", "foreign"]
    elif mutation == "lost_provenance":
        observed["provenance_valid"] = False
    else:
        observed["claim_count"] = 0

    assert evaluation.score_case(case_fixture(), observed, auto_resolve=False)


def test_pending_human_labels_cannot_pass_the_release_review_gate():
    report = evaluation.evaluate([case_fixture()], require_human_review=True)

    assert report["verdict"] == "insufficient_evaluation"
    assert report["release_ready"] is False
    assert report["summary"]["passed_cases"] == 1
    assert report["pending_human_review"] == ["internal-voltage-conflict"]


def test_reviewed_labels_satisfy_only_the_label_gate():
    case = case_fixture()
    case["review"] = {"status": "approved", "reviewer": "independent-engineer"}

    report = evaluation.evaluate([case], require_human_review=True)

    assert report["verdict"] == "pass"
    assert report["pending_human_review"] == []
    assert report["release_ready"] is False


def test_critical_miss_count_includes_each_missed_field(monkeypatch):
    from src.document_authoring.conflicts.detector import ConflictDetector

    case = case_fixture()
    case["evidence"][0]["content"] += "静态电流小于200uA。"
    case["evidence"][1]["content"] += "静态电流小于300uA。"
    case["expected"]["claim_count"] = 4
    case["expected"]["groups"].append({**case["expected"]["groups"][0], "field_key": "quiescent_current"})
    monkeypatch.setattr(ConflictDetector, "detect", lambda _self, _claims: [])

    report = evaluation.evaluate([case])

    assert report["verdict"] == "fail"
    assert report["summary"]["critical_block_misses"] == 2


def test_offline_evaluation_does_not_initialize_deployment_storage(tmp_path, monkeypatch):
    deployment_storage = tmp_path / "deployment"
    deployment_storage.mkdir()
    monkeypatch.setattr(settings, "STORAGE_DIR", str(deployment_storage))

    report = evaluation.evaluate([case_fixture()])

    assert report["verdict"] == "pass"
    assert list(deployment_storage.iterdir()) == []


@pytest.mark.parametrize("records", [[], [case_fixture(), case_fixture()]])
def test_empty_or_duplicate_dataset_is_rejected(tmp_path, records):
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(case) for case in records))

    with pytest.raises(ValueError):
        evaluation.load_cases(path)


@pytest.mark.parametrize("mutation", ["unknown_source", "missing_expectation", "unattributed_review"])
def test_invalid_label_or_source_binding_is_rejected(tmp_path, mutation):
    case = case_fixture()
    if mutation == "unknown_source":
        case["evidence"][0]["kb_id"] = 99
    elif mutation == "missing_expectation":
        del case["expected"]["groups"]
    else:
        case["review"] = {"status": "approved", "reviewer": ""}
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case))

    with pytest.raises(ValueError):
        evaluation.load_cases(path)


def test_cli_keeps_failed_report_and_returns_nonzero(tmp_path):
    case = case_fixture()
    case["expected"]["groups"] = []
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(case))
    output = tmp_path / "report.json"

    assert evaluation.main(["--dataset", str(dataset), "--output", str(output)]) == 2
    report = json.loads(output.read_text())
    assert report["verdict"] == "fail"
    assert report["dataset_sha256"]
    assert report["cases"][0]["failures"]


def test_cli_human_gate_returns_distinct_incomplete_status(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(case_fixture()))
    output = tmp_path / "report.json"

    assert evaluation.main(["--dataset", str(dataset), "--output", str(output),
                            "--require-human-review"]) == 3
    assert json.loads(output.read_text())["release_ready"] is False


@pytest.mark.parametrize("enabled", [False, True])
def test_fixed_dataset_passes_both_rollout_modes_without_claiming_human_acceptance(monkeypatch, enabled):
    monkeypatch.setattr(settings, "MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED", enabled)
    dataset = Path(__file__).parents[1] / "evaluation/datasets/multi_kb_conflicts_v1.jsonl"
    cases = evaluation.load_cases(dataset)

    report = evaluation.evaluate(cases)

    assert report["verdict"] == "pass", report["cases"]
    assert report["summary"]["passed_cases"] == len(cases)
    assert report["release_ready"] is False
    assert {case["category"] for case in cases} >= {
        "unit_equivalence", "different_conditions", "different_objects", "different_versions",
        "internal_conflict", "project_difference", "missing_primary", "four_sources",
        "untrusted_instruction", "primary_authority", "draft_standard",
    }
