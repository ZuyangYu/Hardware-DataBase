from __future__ import annotations

import pytest

from src.document_authoring.models import HarnessPolicy
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.writers.evidence_reranker import EvidenceReranker
from src.document_authoring.writers.requirement_fit_checker import RequirementFitChecker


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


def test_default_allowed_tools_do_not_include_adaptive_recovery():
    # Adaptive recovery is opt-in: it is a status-CHANGING retrieval gate
    # (blocked -> requires_human with a low-confidence draft) and performs an
    # extra real backend retrieve. Deployments enable it explicitly after
    # confirming the balanced-route retry is desirable, so it is not in the
    # default allowlist. See stage 5 design.
    policy = _make_policy()
    assert "adaptive_recovery" not in policy.allowed_tools


def test_adaptive_recovery_rounds_defaults_to_zero():
    # Zero means the recovery branch is disabled even if the tool is
    # allowlisted; the tool + budget form a double switch (same precedent as
    # rewrite_query + max_query_rewrite_rounds).
    policy = _make_policy()
    assert policy.max_adaptive_recovery_rounds == 0


def test_adaptive_recovery_rounds_accepts_positive():
    policy = _make_policy(max_adaptive_recovery_rounds=1)
    assert policy.max_adaptive_recovery_rounds == 1


def test_adaptive_recovery_rounds_rejects_negative():
    with pytest.raises(ValueError):
        _make_policy(max_adaptive_recovery_rounds=-1)
