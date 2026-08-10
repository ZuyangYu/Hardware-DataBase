from __future__ import annotations

from unittest.mock import Mock

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


def test_rewriter_returns_rewrite_string_from_json():
    client = Mock()
    client.chat.return_value = '{"rewrite": "额定电压 规格参数 电源电压"}'
    rewriter = QueryRewriter(client=client)
    result = rewriter.rewrite(_requirement())
    assert result == "额定电压 规格参数 电源电压"
    assert client.chat.call_args.kwargs.get("usage_stage") == "query_rewrite"
    assert client.chat.call_args.kwargs.get("timeout") == 20


def test_rewriter_returns_text_when_not_json():
    client = Mock()
    client.chat.return_value = "电源电压 额定值 参数"
    rewriter = QueryRewriter(client=client)
    result = rewriter.rewrite(_requirement())
    assert result == "电源电压 额定值 参数"


def test_rewriter_strips_code_fences():
    client = Mock()
    client.chat.return_value = "```json\n{\"rewrite\": \"重写串\"}\n```"
    rewriter = QueryRewriter(client=client)
    assert rewriter.rewrite(_requirement()) == "重写串"


def test_rewriter_returns_none_on_llm_exception():
    client = Mock()
    client.chat.side_effect = RuntimeError("llm down")
    rewriter = QueryRewriter(client=client)
    assert rewriter.rewrite(_requirement()) is None


def test_rewriter_returns_none_on_empty_response():
    client = Mock()
    client.chat.return_value = "   \n  "
    rewriter = QueryRewriter(client=client)
    assert rewriter.rewrite(_requirement()) is None
