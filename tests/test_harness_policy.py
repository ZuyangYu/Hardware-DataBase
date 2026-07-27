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
