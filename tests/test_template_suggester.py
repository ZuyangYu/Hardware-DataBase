from __future__ import annotations

import json

from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisUnit,
    TemplateNeighbor,
)
from src.document_authoring.template_suggester import (
    LLMTemplateSuggestionProvider,
    TemplateSuggestionBatch,
)


class RecordingClient:
    def __init__(self, response: str):
        self.response = response
        self.messages: list[dict[str, str]] = []
        self.usage_stage: str | None = None

    def invoke(self, messages: list[dict[str, str]], **kwargs: object) -> str:
        self.messages = messages
        self.usage_stage = str(kwargs.get("usage_stage"))
        return self.response


class FailingClient:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages: list[dict[str, str]], **kwargs: object) -> str:
        self.calls += 1
        raise TimeoutError("template analysis timed out")


class _SuggestionRunnable:
    def __init__(self, response):
        self.response = response

    def invoke(self, messages, **kwargs):
        return self.response


class _StructuredSuggestionModel:
    provider = "custom"
    model_name = "suggestion-test"

    def __init__(self, response):
        self.response = response
        self.schema = None

    def with_structured_output(self, schema):
        self.schema = schema
        return _SuggestionRunnable(self.response)


def test_llm_suggester_prefers_native_structured_output():
    model = _StructuredSuggestionModel({
        "suggestions": [{
            "semantic_unit_id": "summary",
            "label": "摘要",
            "target_unit_ids": ["p-1"],
            "retrieval_terms": ["summary"],
            "confidence": 0.9,
        }],
    })

    suggestions = LLMTemplateSuggestionProvider(model=model).suggest(_safe_docx_analysis())

    assert model.schema is TemplateSuggestionBatch
    assert [item.semantic_unit_id for item in suggestions] == ["summary"]


def test_llm_suggester_uses_explicit_text_compatibility_model():
    class _TextOnlyModel:
        provider = "ollama"
        model_name = "text-only"

        def with_structured_output(self, schema):
            raise NotImplementedError("structured output is not supported")

        def invoke(self, messages, **kwargs):
            return '[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["p-1"],"retrieval_terms":["summary"],"confidence":0.9}]'

    suggestions = LLMTemplateSuggestionProvider(model=_TextOnlyModel()).suggest(_safe_docx_analysis())

    assert [item.semantic_unit_id for item in suggestions] == ["summary"]


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
    assert suggestions[0].semantic_unit_id == "summary"


def test_llm_suggester_approves_only_server_confirmed_sample_value_targets():
    analysis = _safe_docx_analysis()
    client = RecordingClient(response=(
        '[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["p-1"],'
        '"retrieval_terms":["summary"],"confidence":0.9}]'
    ))

    suggestions = LLMTemplateSuggestionProvider(client).suggest(analysis)

    assert suggestions[0].semantic_unit_id == "summary"
    assert analysis.approved_overwrite_unit_ids == []


def test_llm_suggester_marks_only_sample_value_basis_as_approved_overwrite():
    analysis = TemplateAnalysis(
        analysis_id="analysis-sample",
        template_version_id="template-sample",
        content_hash="a" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=[TemplateAnalysisUnit(
            unit_id="sheet:Review!B1",
            locator={"sheet_name": "Review", "cell": "B1"},
            label="Sample value",
            writable=True,
            value_preview="Example project",
            value_kind="text",
            structural_role_hint="sample_value",
            candidate_for_auto_fill=True,
        )],
    )
    client = RecordingClient(response=(
        '[{"semantic_unit_id":"summary","label":"摘要",'
        '"target_unit_ids":["sheet:Review!B1"],"retrieval_terms":["summary"],'
        '"confidence":0.9,"overwrite_basis":"sample_value"}]'
    ))

    suggestions = LLMTemplateSuggestionProvider(client).suggest(analysis)

    assert suggestions[0].overwrite_basis == "sample_value"
    assert analysis.approved_overwrite_unit_ids == ["sheet:Review!B1"]


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


def test_llm_suggester_heals_retrieval_terms_format_drift():
    """retrieval_terms 输出为单个字符串时归一化为列表，不丢弃整条建议。"""
    analysis = _safe_docx_analysis()
    client = RecordingClient(response=(
        '[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["p-1"],'
        '"retrieval_terms":"summary","confidence":0.9}]'
    ))

    suggestions = LLMTemplateSuggestionProvider(client).suggest(analysis)

    assert len(suggestions) == 1
    assert suggestions[0].retrieval_terms == ["summary"]
    assert analysis.status == "ready_for_confirmation"


def test_llm_suggester_filters_non_string_retrieval_terms():
    """列表里混入非字符串的 retrieval_terms 成员被剔除而非废弃整条。"""
    analysis = _safe_docx_analysis()
    client = RecordingClient(response=(
        '[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["p-1"],'
        '"retrieval_terms":["summary", 42],"confidence":0.9}]'
    ))

    suggestions = LLMTemplateSuggestionProvider(client).suggest(analysis)

    assert len(suggestions) == 1
    assert suggestions[0].retrieval_terms == ["summary"]


def test_llm_suggester_tolerates_extra_fields():
    """多余字段被忽略（不再让整批回退人工），缺字段仍跳过该条。"""
    analysis = _safe_docx_analysis()
    response = (
        '[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["p-1"],'
        '"retrieval_terms":["summary"],"confidence":0.9,"extra":true}]'
    )

    suggestions = LLMTemplateSuggestionProvider(RecordingClient(response)).suggest(analysis)

    assert len(suggestions) == 1
    assert suggestions[0].semantic_unit_id == "summary"
    assert analysis.status == "ready_for_confirmation"


def test_llm_suggester_chunks_large_template_and_merges():
    """大模板按 _SUGGESTION_CHUNK_SIZE 分块调用，逐块解析后合并；单块失败不致命。"""
    units = [
        TemplateAnalysisUnit(
            unit_id=f"u-{i}", locator={"cell": f"A{i + 1}"}, label=f"字段{i}", writable=True,
        )
        for i in range(60)
    ]
    analysis = TemplateAnalysis(
        analysis_id="analysis-big",
        template_version_id="template-big",
        content_hash="b" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=units,
    )

    class SequencingClient:
        def __init__(self):
            self.calls = 0
            self.payloads: list[dict] = []

        def invoke(self, messages, **kwargs):
            self.calls += 1
            payload = json.loads(messages[1]["content"])
            self.payloads.append(payload)
            ids = [unit["unit_id"] for unit in payload["units"]]
            return json.dumps([
                {
                    "semantic_unit_id": uid,
                    "label": uid,
                    "target_unit_ids": [uid],
                    "retrieval_terms": [uid],
                    "confidence": 0.9,
                }
                for uid in ids
            ])

    client = SequencingClient()
    suggestions = LLMTemplateSuggestionProvider(client).suggest(analysis)

    assert client.calls == 2  # 60 units / 50 per chunk
    assert len(suggestions) == 60
    assert all(len(s.target_unit_ids) == 1 for s in suggestions)
    assert analysis.status == "ready_for_confirmation"
    assert analysis.approved_overwrite_unit_ids == []


def test_llm_suggester_survives_partial_chunk_failure():
    """某块返回非法 JSON 时只丢该块，其余块仍能产出可用建议。"""
    units = [
        TemplateAnalysisUnit(
            unit_id=f"u-{i}", locator={"cell": f"A{i + 1}"}, label=f"字段{i}", writable=True,
        )
        for i in range(110)  # 3 chunks of 50/50/10
    ]
    analysis = TemplateAnalysis(
        analysis_id="analysis-partial",
        template_version_id="template-partial",
        content_hash="c" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=units,
    )

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 2:
                return "not json"  # 第二块整体失败
            payload = json.loads(messages[1]["content"])
            ids = [unit["unit_id"] for unit in payload["units"]]
            return json.dumps([
                {
                    "semantic_unit_id": uid,
                    "label": uid,
                    "target_unit_ids": [uid],
                    "retrieval_terms": [uid],
                    "confidence": 0.9,
                }
                for uid in ids
            ])

    client = FlakyClient()
    suggestions = LLMTemplateSuggestionProvider(client).suggest(analysis)

    assert client.calls == 3
    # 第一块 50 + 第三块 10 = 60 条建议存活，第二块丢失。
    assert len(suggestions) == 60
    assert analysis.status == "ready_for_confirmation"


def test_llm_suggester_falls_back_to_function_table_cells_when_model_is_unavailable():
    analysis = TemplateAnalysis(
        analysis_id="analysis-table-fallback",
        template_version_id="template-table-fallback",
        content_hash="d" * 64,
        format="xlsx",
        status="ready_for_confirmation",
        units=[
            TemplateAnalysisUnit(
                unit_id="sheet:Pinout!C1",
                locator={"sheet_name": "Pinout", "cell": "C1"},
                label="Pinout!C1",
                writable=True,
                value_preview="功能描述 Function",
                value_kind="text",
                structural_role_hint="table_header",
                neighborhood=[],
            ),
            TemplateAnalysisUnit(
                unit_id="sheet:Pinout!A2",
                locator={"sheet_name": "Pinout", "cell": "A2"},
                label="Pinout!A2",
                writable=True,
                value_preview="DP1600-1",
                value_kind="text",
                structural_role_hint="sample_value",
                neighborhood=[],
            ),
            TemplateAnalysisUnit(
                unit_id="sheet:Pinout!B2",
                locator={"sheet_name": "Pinout", "cell": "B2"},
                label="Pinout!B2",
                writable=True,
                value_preview="UBD",
                value_kind="text",
                structural_role_hint="sample_value",
                neighborhood=[],
            ),
            TemplateAnalysisUnit(
                unit_id="sheet:Pinout!C2",
                locator={"sheet_name": "Pinout", "cell": "C2"},
                label="Pinout!C2",
                writable=True,
                value_kind="blank",
                structural_role_hint="scalar_input",
                candidate_for_auto_fill=True,
                neighborhood=[
                    TemplateNeighbor(relative_row=-1, relative_column=0, value_preview="功能描述 Function"),
                    TemplateNeighbor(relative_row=0, relative_column=-1, value_preview="UBD"),
                ],
            ),
            TemplateAnalysisUnit(
                unit_id="sheet:Pinout!C8",
                locator={"sheet_name": "Pinout", "cell": "C8"},
                label="Pinout!C8",
                writable=True,
                value_kind="blank",
                structural_role_hint="scalar_input",
                candidate_for_auto_fill=True,
                neighborhood=[],
            ),
        ],
    )

    client = FailingClient()
    suggestions = LLMTemplateSuggestionProvider(client).suggest(analysis)

    assert {target for suggestion in suggestions for target in suggestion.target_unit_ids} == {
        "sheet:Pinout!C2", "sheet:Pinout!C8",
    }
    assert "功能描述 Function" in next(
        suggestion for suggestion in suggestions
        if suggestion.target_unit_ids == ["sheet:Pinout!C2"]
    ).retrieval_terms
    assert "UBD" in next(
        suggestion for suggestion in suggestions
        if suggestion.target_unit_ids == ["sheet:Pinout!C2"]
    ).retrieval_terms
    assert analysis.status == "ready_for_confirmation"
    assert client.calls == 0
