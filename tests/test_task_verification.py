"""Deterministic task acceptance checks."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.task_verification import build_task_verification


def _artifact(name: str = "out/result.md"):
    return SimpleNamespace(name=name, kind="document")


def test_complete_task_passes():
    result = build_task_verification(
        artifacts=[_artifact()],
        answer_text="已完成 [1][2]",
        evidence_count=2,
        kb_mounted=True,
    )
    assert result["status"] == "pass"
    assert all(item["status"] == "pass" for item in result["checks"])


def test_missing_artifacts_and_citations_warn():
    result = build_task_verification(
        artifacts=[],
        answer_text="没有引用的回答",
        evidence_count=0,
        kb_mounted=True,
    )
    assert result["status"] == "warn"
    ids = {item["id"]: item["status"] for item in result["checks"]}
    assert ids["artifact_present"] == "warn"
    assert ids["citations"] == "warn"
    assert ids["evidence"] == "warn"


def test_partial_delivery_is_flagged():
    result = build_task_verification(
        artifacts=[_artifact()],
        answer_text="部分结果 [1]",
        evidence_count=1,
        partial=True,
        kb_mounted=True,
    )
    assert result["status"] == "warn"
    assert any(item["id"] == "partial" for item in result["checks"])
