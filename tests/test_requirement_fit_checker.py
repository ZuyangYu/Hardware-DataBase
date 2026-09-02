from __future__ import annotations

from src.agents.claim_evidence import InformationRequirement
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


class _TextModel:
    def __init__(self, response: str | Exception = '{"fit": true, "reason": "ok"}'):
        self.response = response
        self.calls: list[tuple[object, dict]] = []

    def with_structured_output(self, schema):
        raise NotImplementedError("structured output is not supported")

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_check_returns_fit_true_when_llm_says_fit():
    checker = RequirementFitChecker(model=_TextModel('{"fit": true, "reason": "covers spec"}'))

    verdict = checker.check(_draft(), _requirement())

    assert verdict["fit"] is True
    assert verdict["reason"] == "covers spec"


def test_check_returns_fit_false_when_llm_says_unfit():
    checker = RequirementFitChecker(model=_TextModel('{"fit": false, "reason": "missing spec value"}'))

    verdict = checker.check(_draft(), _requirement())

    assert verdict["fit"] is False
    assert verdict["reason"] == "missing spec value"


def test_check_degrades_to_pass_on_llm_failure():
    checker = RequirementFitChecker(model=_TextModel(RuntimeError("boom")))

    verdict = checker.check(_draft(), _requirement())

    assert verdict["fit"] is True  # never blocks on LLM failure
    assert "unavailable" in verdict["reason"]


def test_check_degrades_to_pass_on_parse_failure():
    for bad in ("not json", "42", '"a string"'):
        checker = RequirementFitChecker(model=_TextModel(bad))
        verdict = checker.check(_draft(), _requirement())
        assert verdict["fit"] is True, f"failed for: {bad!r}"
        assert "unavailable" in verdict["reason"]


def test_check_uses_requirement_fit_check_usage_stage():
    model = _TextModel('{"fit": true, "reason": "ok"}')
    RequirementFitChecker(model=model).check(_draft(), _requirement())

    assert len(model.calls) == 1
    assert model.calls[0][1] == {}
