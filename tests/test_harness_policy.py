from __future__ import annotations

import pytest

from src.document_authoring.models import HarnessPolicy


def _make_policy(**overrides) -> HarnessPolicy:
    base = dict(
        harness_policy_id="p1",
        version="1",
        status="approved",
    )
    base.update(overrides)
    return HarnessPolicy(**base)


def test_default_allowed_tools_include_rewrite_query():
    policy = _make_policy()
    assert "rewrite_query" in policy.allowed_tools


def test_default_max_query_rewrite_rounds_is_one():
    policy = _make_policy()
    assert policy.max_query_rewrite_rounds == 1


def test_policy_rejects_negative_rewrite_rounds():
    with pytest.raises(ValueError):
        _make_policy(max_query_rewrite_rounds=-1)


def test_policy_rejects_zero_rewrite_rounds():
    with pytest.raises(ValueError):
        _make_policy(max_query_rewrite_rounds=0)


from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.writers.evidence_reranker import EvidenceReranker


def test_default_allowed_tools_include_rerank_evidence():
    policy = _make_policy()
    assert "rerank_evidence" in policy.allowed_tools


def test_reranker_for_policy_returns_reranker_when_allowed():
    policy = _make_policy(allowed_tools=["rerank_evidence", "retrieve_evidence"])
    reranker = DocumentGenerationService._reranker_for_policy(policy)
    assert isinstance(reranker, EvidenceReranker)


def test_reranker_for_policy_returns_none_when_not_allowed():
    policy = _make_policy(allowed_tools=["retrieve_evidence"])
    assert DocumentGenerationService._reranker_for_policy(policy) is None


from src.document_authoring.writers.requirement_fit_checker import RequirementFitChecker


def test_default_allowed_tools_do_not_include_requirement_fit_check():
    # Requirement fit check is opt-in: it is the first status-CHANGING LLM
    # gate (unfit -> requires_human), unlike the status-preserving rerank/
    # rewrite tools. Deployments enable it explicitly after validating the
    # LLM's fit judgment, so it is not in the default allowlist.
    policy = _make_policy()
    assert "requirement_fit_check" not in policy.allowed_tools


def test_fit_checker_for_policy_returns_checker_when_allowed():
    policy = _make_policy(allowed_tools=["requirement_fit_check", "retrieve_evidence"])
    checker = DocumentGenerationService._fit_checker_for_policy(policy)
    assert isinstance(checker, RequirementFitChecker)


def test_fit_checker_for_policy_returns_none_when_not_allowed():
    policy = _make_policy(allowed_tools=["retrieve_evidence"])
    assert DocumentGenerationService._fit_checker_for_policy(policy) is None
