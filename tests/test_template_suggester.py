from __future__ import annotations

from src.document_authoring.template_analysis import TemplateAnalysis, TemplateAnalysisUnit
from src.document_authoring.template_suggester import LLMTemplateSuggestionProvider


class RecordingClient:
    def __init__(self, response: str):
        self.response = response
        self.messages: list[dict[str, str]] = []
        self.usage_stage: str | None = None

    def chat(self, messages: list[dict[str, str]], **kwargs: object) -> str:
        self.messages = messages
        self.usage_stage = str(kwargs.get("usage_stage"))
        return self.response


def _safe_docx_analysis() -> TemplateAnalysis:
    return TemplateAnalysis(
        analysis_id="analysis-safe",
        template_version_id="template-safe",
        content_hash="a" * 64,
        format="docx",
        status="ready_for_confirmation",
        units=[TemplateAnalysisUnit(
            unit_id="p-1", locator={"paragraph_index": 0}, label="Paragraph 1", writable=True,
        )],
    )


def test_llm_suggester_only_sends_structural_inventory_and_parses_valid_json():
    client = RecordingClient(response=(
        '[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["p-1"],'
        '"retrieval_terms":["summary"],"confidence":0.9}]'
    ))

    suggestions = LLMTemplateSuggestionProvider(client).suggest(_safe_docx_analysis())

    assert "content_hash" in client.messages[1]["content"]
    assert "PK" not in client.messages[1]["content"]
    assert "template-safe" in client.messages[1]["content"]
    assert client.usage_stage == "template_analysis"
    assert suggestions[0].semantic_unit_id == "summary"


def test_llm_suggester_requires_human_review_on_invalid_json_or_invalid_target():
    analysis = _safe_docx_analysis()

    suggestions = LLMTemplateSuggestionProvider(RecordingClient("not json")).suggest(analysis)

    assert suggestions == []
    assert analysis.status == "requires_human"

    invalid_target = _safe_docx_analysis()
    suggestions = LLMTemplateSuggestionProvider(RecordingClient(
        '[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["not-in-inventory"],'
        '"retrieval_terms":[],"confidence":0.9}]'
    )).suggest(invalid_target)
    assert suggestions == []
    assert invalid_target.status == "requires_human"


def test_llm_suggester_rejects_extra_or_missing_suggestion_fields():
    analysis = _safe_docx_analysis()
    response = '[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["p-1"],"confidence":0.9,"extra":true}]'

    suggestions = LLMTemplateSuggestionProvider(RecordingClient(response)).suggest(analysis)

    assert suggestions == []
    assert analysis.status == "requires_human"
