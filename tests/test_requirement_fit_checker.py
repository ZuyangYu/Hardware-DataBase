from __future__ import annotations

from unittest.mock import Mock

from src.agents.claim_evidence import InformationRequirement
from src.core.llm_client import LLMClient
from src.document_authoring.models import DocumentUnitDraft
from src.document_authoring.writers.requirement_fit_checker import RequirementFitChecker


def _requirement() -> InformationRequirement:
    return InformationRequirement(
        requirement_id="req-1", semantic_unit_id="field:f1", claim_type="attribute",
        subject="额定电流", predicate="电源拓扑",
    )


def _draft(content: str = "额定电流为 10A") -> DocumentUnitDraft:
    return DocumentUnitDraft(
        unit_id="field:f1", run_id="run-1", generated_by="managed_writer", content=content,
    )


def _client(return_value: str = '{"fit": true, "reason": "ok"}', *, side_effect=None) -> Mock:
    client = Mock(spec=LLMClient)
    if side_effect is not None:
        client.chat.side_effect = side_effect
    else:
        client.chat.return_value = return_value
    return client


def test_check_returns_fit_true_when_llm_says_fit():
    checker = RequirementFitChecker(client=_client('{"fit": true, "reason": "covers spec"}'))

    verdict = checker.check(_draft(), _requirement())

    assert verdict["fit"] is True
    assert verdict["reason"] == "covers spec"


def test_check_returns_fit_false_when_llm_says_unfit():
    checker = RequirementFitChecker(client=_client('{"fit": false, "reason": "missing spec value"}'))

    verdict = checker.check(_draft(), _requirement())

    assert verdict["fit"] is False
    assert verdict["reason"] == "missing spec value"


def test_check_degrades_to_pass_on_llm_failure():
    checker = RequirementFitChecker(client=_client(side_effect=RuntimeError("boom")))

    verdict = checker.check(_draft(), _requirement())

    assert verdict["fit"] is True  # never blocks on LLM failure
    assert "unavailable" in verdict["reason"]


def test_check_degrades_to_pass_on_parse_failure():
    for bad in ("not json", "42", '"a string"'):
        checker = RequirementFitChecker(client=_client(bad))
        verdict = checker.check(_draft(), _requirement())
        assert verdict["fit"] is True, f"failed for: {bad!r}"
        assert "unavailable" in verdict["reason"]


def test_check_uses_requirement_fit_check_usage_stage():
    client = _client('{"fit": true, "reason": "ok"}')
    RequirementFitChecker(client=client).check(_draft(), _requirement())

    assert client.chat.call_args.kwargs.get("usage_stage") == "requirement_fit_check"
