from __future__ import annotations

from src.agents.claim_evidence import InformationRequirement
from src.document_authoring.writers.query_rewriter import QueryRewriter


def _requirement(subject: str = "额定电压") -> InformationRequirement:
    return InformationRequirement(
        requirement_id="req-1",
        semantic_unit_id="field:f1",
        claim_type="attribute",
        subject=subject,
        predicate="规格",
        required_capabilities=["entity_lookup"],
    )


class _TextModel:
    def __init__(self, response: str | Exception):
        self.response = response
        self.calls: list[tuple[object, dict]] = []

    def with_structured_output(self, schema):
        raise NotImplementedError("structured output is not supported")

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_rewriter_returns_rewrite_string_from_json():
    model = _TextModel('{"rewrite": "额定电压 规格参数 电源电压"}')
    rewriter = QueryRewriter(model=model)
    result = rewriter.rewrite(_requirement())
    assert result == "额定电压 规格参数 电源电压"
    assert model.calls and model.calls[0][1] == {}


def test_rewriter_returns_text_when_not_json():
    model = _TextModel("电源电压 额定值 参数")
    rewriter = QueryRewriter(model=model)
    result = rewriter.rewrite(_requirement())
    assert result == "电源电压 额定值 参数"


def test_rewriter_strips_code_fences():
    model = _TextModel("```json\n{\"rewrite\": \"重写串\"}\n```")
    rewriter = QueryRewriter(model=model)
    assert rewriter.rewrite(_requirement()) == "重写串"


def test_rewriter_returns_none_on_llm_exception():
    rewriter = QueryRewriter(model=_TextModel(RuntimeError("llm down")))
    assert rewriter.rewrite(_requirement()) is None


def test_rewriter_returns_none_on_empty_response():
    rewriter = QueryRewriter(model=_TextModel("   \n  "))
    assert rewriter.rewrite(_requirement()) is None
