from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.agents.claim_evidence import InformationRequirement, RetrievalOutcome
from src.document_authoring.harness.graph import AuthoringGraph, DocumentAuthoringState
from src.document_authoring.harness.policy import HarnessToolPolicy
from src.document_authoring.models import HarnessPolicy
from src.document_authoring.writers.managed import DeterministicEvidenceWriter, ManagedWriter


def _requirement(subject: str = "电压") -> InformationRequirement:
    return InformationRequirement(
        requirement_id="req-1",
        semantic_unit_id="field:f1",
        claim_type="attribute",
        subject=subject,
        required_capabilities=["entity_lookup"],
    )


def _outcome(status: str) -> RetrievalOutcome:
    return RetrievalOutcome(
        requirement_id="req-1", status=status, evidences=[], source_outcomes=[],
        query_fingerprint="fp", applied_source_set_snapshot_id="snap-1",
        applied_region_policy_versions={},
    )


def _make_graph(rewriter=None, allowed_tools=None) -> AuthoringGraph:
    policy = HarnessPolicy(
        harness_policy_id="p1", version="1", status="approved",
        max_retrieval_attempts_per_unit=2, max_retrieval_rounds=4, max_steps=40,
        allowed_tools=allowed_tools if allowed_tools is not None else [
            "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
            "detect_template_contamination", "validate_cross_unit", "rewrite_query",
        ],
    )
    writer = ManagedWriter(DeterministicEvidenceWriter())
    return AuthoringGraph(HarnessToolPolicy(policy), writer, rewriter=rewriter)


def _state() -> DocumentAuthoringState:
    return {
        "work_order": None, "harness_run": None, "run_manifest": None,
        "document_schema": None, "source_set_snapshot": None,
        "current_node": "initialize", "step_count": 0, "retrieval_round_count": 0,
    }


def test_uses_rewrite_on_success_empty():
    calls = []

    def retrieve(requirement, attempt, query_override=None):
        calls.append((attempt, query_override))
        return _outcome("success_empty") if attempt == 1 else _outcome("success_with_hits")

    rewriter = Mock()
    rewriter.rewrite.return_value = "rewrite-query"
    graph = _make_graph(rewriter=rewriter)

    outcome = graph._retrieve_with_budget(_state(), _requirement(), retrieve)

    assert outcome.status == "success_with_hits"
    assert calls[0] == (1, None)
    assert calls[1] == (2, "rewrite-query")
    rewriter.rewrite.assert_called_once()


def test_falls_back_to_original_when_rewriter_returns_none():
    calls = []

    def retrieve(requirement, attempt, query_override=None):
        calls.append((attempt, query_override))
        return _outcome("success_empty") if attempt == 1 else _outcome("success_with_hits")

    rewriter = Mock()
    rewriter.rewrite.return_value = None
    graph = _make_graph(rewriter=rewriter)

    outcome = graph._retrieve_with_budget(_state(), _requirement(), retrieve)

    assert outcome.status == "success_with_hits"
    assert calls[1] == (2, None)


def test_no_rewriter_uses_original_on_success_empty():
    calls = []

    def retrieve(requirement, attempt, query_override=None):
        calls.append((attempt, query_override))
        return _outcome("success_empty") if attempt == 1 else _outcome("success_with_hits")

    graph = _make_graph(rewriter=None)

    outcome = graph._retrieve_with_budget(_state(), _requirement(), retrieve)

    assert outcome.status == "success_with_hits"
    assert calls[1] == (2, None)


def test_retries_on_failure_status_without_rewrite():
    calls = []

    def retrieve(requirement, attempt, query_override=None):
        calls.append((attempt, query_override))
        return _outcome("retrieval_failed") if attempt == 1 else _outcome("success_with_hits")

    rewriter = Mock()
    graph = _make_graph(rewriter=rewriter)

    outcome = graph._retrieve_with_budget(_state(), _requirement(), retrieve)

    assert outcome.status == "success_with_hits"
    # failure status should retry without invoking the rewriter
    rewriter.rewrite.assert_not_called()
    assert calls[1] == (2, None)


def test_success_with_hits_does_not_retry():
    calls = []

    def retrieve(requirement, attempt, query_override=None):
        calls.append((attempt, query_override))
        return _outcome("success_with_hits")

    rewriter = Mock()
    graph = _make_graph(rewriter=rewriter)

    outcome = graph._retrieve_with_budget(_state(), _requirement(), retrieve)

    assert outcome.status == "success_with_hits"
    assert len(calls) == 1
    rewriter.rewrite.assert_not_called()


def test_rewrite_gate_rejects_when_tool_not_allowed():
    def retrieve(requirement, attempt, query_override=None):
        return _outcome("success_empty") if attempt == 1 else _outcome("success_with_hits")

    rewriter = Mock()
    rewriter.rewrite.return_value = "rewrite-query"
    # rewriter injected but policy lacks rewrite_query -> must raise
    graph = _make_graph(
        rewriter=rewriter,
        allowed_tools=[
            "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
            "detect_template_contamination", "validate_cross_unit",
        ],
    )

    with pytest.raises(PermissionError, match="rewrite_query"):
        graph._retrieve_with_budget(_state(), _requirement(), retrieve)


def test_rewrite_exception_degrades_to_original():
    calls = []

    def retrieve(requirement, attempt, query_override=None):
        calls.append((attempt, query_override))
        return _outcome("success_empty") if attempt == 1 else _outcome("success_with_hits")

    rewriter = Mock()
    rewriter.rewrite.side_effect = RuntimeError("llm down")
    graph = _make_graph(rewriter=rewriter)

    outcome = graph._retrieve_with_budget(_state(), _requirement(), retrieve)

    assert outcome.status == "success_with_hits"
    assert calls[1] == (2, None)
