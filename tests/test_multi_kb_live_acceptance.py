"""Acceptance scores must bind quoted anchors to the expected source identity."""

import pytest

from scripts.production_hardening import multi_kb_live_acceptance as acceptance


def evidence(kb_id, content, *, record_id="record-1"):
    return {"id": f"e-{kb_id}", "content": content,
            "metadata": {"kb_id": kb_id, "local_record_id": record_id}}


def test_same_text_in_wrong_project_does_not_satisfy_a_relevance_anchor():
    case = {"expected": [{"source": "eps60", "patterns": [r"18\s*-\s*32\s*V"]}]}
    result = acceptance.score_case(case, [evidence(3, "18-32V")], {"eps60": 2})
    assert result["anchor_recall"] == 0
    assert result["passed"] is False


def test_every_required_anchor_and_source_must_be_present():
    case = {"expected": [{"source": "eps60", "patterns": [r"18-32V", r"200\s*uA"]},
                         {"source": "eps62", "patterns": [r"9-36V"]}]}
    incomplete = [evidence(2, "18-32V"), evidence(3, "9-36V")]
    assert acceptance.score_case(case, incomplete, {"eps60": 2, "eps62": 3})["anchor_recall"] == 2 / 3
    complete = incomplete + [evidence(2, "200 uA")]
    scored = acceptance.score_case(case, complete, {"eps60": 2, "eps62": 3})
    assert scored["passed"] is True
    assert scored["source_recall"] == 1


def test_missing_local_record_identity_is_a_provenance_failure():
    case = {"expected": [{"source": "eps60", "patterns": ["18-32V"]}]}
    scored = acceptance.score_case(case, [evidence(2, "18-32V", record_id="")], {"eps60": 2})
    assert scored["passed"] is False
    assert scored["provenance_failures"] == ["e-2"]


def test_capacity_summary_counts_timeouts_as_failures_not_fast_successes():
    summary = acceptance.summarize_capacity([
        {"elapsed_seconds": 1, "partial": False, "calls_used": 4, "status": "ok"},
        {"elapsed_seconds": 30, "partial": True, "calls_used": 4, "status": "timeout"},
    ])
    assert summary["failure_count"] == 1
    assert summary["p95_seconds"] >= 28
    assert summary["calls_used"] == 8


@pytest.mark.parametrize(("failure_counts", "expected_exit_code"), [
    ([0, 0], 0),
    ([0, 1], 1),
    ([1, 0], 1),
])
def test_cli_exit_code_includes_every_capacity_group(
    tmp_path, monkeypatch, failure_counts, expected_exit_code,
):
    config = tmp_path / "active.env"
    config.write_text("")
    monkeypatch.setenv("HDB_ENV_PATH", str(config))
    report = {
        "summary": {"case_count": 1, "passed_cases": 1, "boundary_passed": True},
        "capacity": [{"failure_count": count} for count in failure_counts],
    }
    # Evaluation owns external retrieval; keep the CLI argument/environment
    # checks and exit-status decision real while supplying a completed report.
    monkeypatch.setattr(acceptance, "evaluate", lambda *args, **kwargs: report)

    assert acceptance.main(["--deployment", str(tmp_path)]) == expected_exit_code
