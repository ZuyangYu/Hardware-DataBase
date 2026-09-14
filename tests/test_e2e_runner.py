"""Offline validation for the live E2E evaluation harness."""

from __future__ import annotations

import io
import json

import pytest

from src.evaluation.e2e_runner import (
    E2EDatasetError,
    _passes_gate,
    evaluate_case_expectations,
    fixture_bytes,
    load_e2e_dataset,
    main,
)

DATASET = "evaluation/datasets/attachment_editing_v1.jsonl"


def test_dataset_loads_with_expected_shape():
    cases = load_e2e_dataset(DATASET)

    assert len(cases) >= 50
    assert len({case.case_id for case in cases}) == len(cases)
    replace_cases = [case for case in cases if case.expect.get("operation") == "replace_text"]
    section_cases = [case for case in cases if case.expect.get("operation") == "replace_section"]
    blocked_cases = [case for case in cases if case.expect.get("expect_blocked")]
    release_cases = [case for case in cases if case.expect.get("release_request")]
    assert replace_cases and section_cases
    assert len(blocked_cases) >= 3
    assert len(release_cases) >= 5


def test_dataset_rejects_malformed_entries(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps({"id": "x", "query": "q", "fixture": {"kind": "pdf"}, "expect": {"turn_status": "completed"}}),
        encoding="utf-8",
    )
    with pytest.raises(E2EDatasetError, match="fixture.kind"):
        load_e2e_dataset(path)

    path.write_text(
        json.dumps({"id": "x", "query": "q", "fixture": {"kind": "md", "content": "a"}, "expect": {"nope": 1}}),
        encoding="utf-8",
    )
    with pytest.raises(E2EDatasetError, match="unknown expect keys"):
        load_e2e_dataset(path)

    path.write_text(
        json.dumps({"id": "x", "query": "q", "fixture": {"kind": "md", "content": "a"}, "expect": {"turn_status": "completed"}})
        + "\n"
        + json.dumps({"id": "x", "query": "q", "fixture": {"kind": "md", "content": "b"}, "expect": {"turn_status": "completed"}}),
        encoding="utf-8",
    )
    with pytest.raises(E2EDatasetError, match="duplicate id"):
        load_e2e_dataset(path)


def test_fixture_bytes_build_docx_headings():
    filename, content, media = fixture_bytes(
        {"kind": "docx", "lines": ["# 2 硬件架构", "正文", "## 2.1 子系统"]},
        "case-1",
    )
    assert filename == "case-1.docx"
    assert media.endswith("wordprocessingml.document")

    from docx import Document

    document = Document(io.BytesIO(content))
    assert document.paragraphs[0].text == "2 硬件架构"
    assert document.paragraphs[0].style.name.startswith("Heading")
    assert document.paragraphs[2].style.name.startswith("Heading")


def _observation(**overrides):
    observation = {
        "turn_status": "completed",
        "tool_names": ["attachment_search", "attachment_edit"],
        "events": [],
        "revision": {
            "operation": "replace_text",
            "change_summary": "将「A」替换为「B」",
            "revision_id": "attachment-revision-1",
            "release_state": "not_requested",
        },
        "revision_count": 1,
        "child_texts": ["B", "保留"],
        "parent_texts": ["A", "保留"],
        "released_texts": [],
        "release_state": "not_requested",
    }
    observation.update(overrides)
    return observation


def test_expectation_evaluator_passes_a_matching_case():
    expect = {
        "turn_status": "completed",
        "must_have_tools": ["attachment_edit"],
        "must_not_have_tools": ["request_attachment_release"],
        "operation": "replace_text",
        "change_summary_contains": ["替换为"],
        "child_texts_contain": ["B"],
        "child_texts_not_contain": ["A"],
        "parent_texts_contain": ["A"],
        "release_state": "not_requested",
    }
    assert evaluate_case_expectations(expect, _observation()) == []


def test_expectation_evaluator_reports_each_failure_class():
    expect = {
        "must_have_tools": ["attachment_edit"],
        "must_not_have_tools": ["request_attachment_release"],
        "operation": "replace_section",
        "change_summary_contains": ["替换章节"],
        "child_texts_contain": ["新内容"],
        "child_texts_not_contain": ["旧内容"],
        "release_state": "approved",
        "released_texts_contain": ["正式"],
        "min_revisions": 2,
    }
    observation = _observation(
        tool_names=["request_attachment_release"],
        revision={
            "operation": "replace_text",
            "change_summary": "将「A」替换为「B」",
            "revision_id": "r1",
        },
        child_texts=["旧内容"],
        release_state="pending",
        revision_count=1,
    )
    failures = evaluate_case_expectations(expect, observation)

    joined = "\n".join(failures)
    assert "attachment_edit" in joined
    assert "request_attachment_release" in joined
    assert "operation" in joined
    assert "change_summary" in joined
    assert "child_texts" in joined
    assert "release_state" in joined
    assert "released_texts" in joined
    assert "revision_count" in joined


def test_expectation_evaluator_requires_blocked_event():
    expect = {"expect_blocked": True, "blocked_error_code": "attachment_revision_rejected"}
    observation = _observation(revision=None)
    failures = evaluate_case_expectations(expect, observation)
    assert any("blocked stage event" in failure for failure in failures)

    observation["events"] = [{
        "event": "stage",
        "payload": {"status": "blocked", "error_code": "attachment_revision_rejected"},
    }]
    assert evaluate_case_expectations(expect, observation) == []

    expect_no = {"expect_no_revision": True}
    assert evaluate_case_expectations(expect_no, _observation()) == [
        "expected no candidate revision but one was created"
    ]


def test_expectation_evaluator_checks_the_block_reason_in_the_answer():
    expect = {"expect_no_revision": True, "answer_contains_any": ["不存在", "未找到"]}

    good = _observation(revision=None, turn_answer="附件中未找到该标题，未生成候选。")
    assert evaluate_case_expectations(expect, good) == []

    bad = _observation(revision=None, turn_answer="好的，已处理。")
    failures = evaluate_case_expectations(expect, bad)
    assert any("does not explain" in failure for failure in failures)


def test_pass_rate_gate():
    assert _passes_gate(10, 10, 0.9) is True
    assert _passes_gate(9, 10, 0.9) is True
    assert _passes_gate(8, 10, 0.9) is False
    assert _passes_gate(0, 0, 0.9) is False


def test_main_refuses_to_run_without_opt_in(monkeypatch, capsys):
    monkeypatch.delenv("HDB_E2E", raising=False)

    assert main(["--dataset", DATASET, "--limit", "1"]) == 2
    assert "HDB_E2E=1" in capsys.readouterr().err
