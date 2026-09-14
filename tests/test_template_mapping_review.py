from __future__ import annotations

from pathlib import Path
import pytest

from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import TemplateAnalysis, TemplateAnalysisSuggestion, TemplateAnalysisUnit
from src.document_authoring.template_mapping_review import apply_mapping_review, prepare_mapping_review
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext


class _Service:
    def __init__(self, analysis):
        self.analysis = analysis
        self.store = self
        self.corrected = None
        self.confirmed = None

    def get_template_analysis(self, template_id):
        return self.analysis if template_id == self.analysis.template_version_id else None

    def get_template_analysis_by_id(self, analysis_id):
        return self.analysis if analysis_id == self.analysis.analysis_id else None

    def get_template(self, template_id):
        from src.document_authoring.models import TemplateVersion
        return TemplateVersion(
            template_version_id="template-1", template_id="Review", format="xlsx",
            content_hash="a" * 64, template_schema_id="schema", template_schema_version="1",
            renderer_policy_id="policy",
        ) if template_id == "template-1" else None

    def get_template_analysis_for_review(self, ctx, *, analysis_id):
        assert analysis_id == self.analysis.analysis_id
        return self.analysis

    def correct_template_analysis(self, ctx, *, correction):
        self.corrected = correction
        return self.analysis.model_copy(update={"analysis_id": "corrected-1", "status": "ready_for_confirmation"})

    def confirm_template_analysis(self, ctx, *, analysis_id, display_name, execution_mode=None):
        self.confirmed = (analysis_id, display_name, execution_mode)
        return {"status": "approved", "analysis_id": analysis_id}


def _analysis():
    unit = TemplateAnalysisUnit(
        unit_id="sheet:Review!B2", locator={"sheet_name": "Review", "cell": "B2"},
        writable=True, value_kind="blank", structural_role_hint="scalar_input",
        candidate_for_auto_fill=True,
    )
    return TemplateAnalysis(
        analysis_id="analysis-1", template_version_id="template-1", content_hash="a" * 64,
        format="xlsx", status="ready_for_confirmation", units=[unit],
        suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="field", label="Field", target_unit_ids=[unit.unit_id], confidence=.99,
        )],
    )


class _Context:
    user_id = "alice"


class _TableSuggester:
    def suggest(self, analysis):
        targets = [
            unit.unit_id for unit in analysis.units
            if unit.locator.get("sheet_name") == "Sheet1"
            and 15 <= int("".join(char for char in unit.locator.get("cell", "") if char.isdigit())) <= 34
        ]
        analysis.suggestions = [TemplateAnalysisSuggestion(
            semantic_unit_id="pinout", label="Pinout", confidence=.99,
            value_shape="repeating_table",
            target_unit_ids=targets,
            overwrite_basis="sample_value",
        )]
        return analysis.suggestions


def _service_with_table(tmp_path):
    service = DocumentGenerationService(
        store=DocumentAuthoringStore(str(tmp_path / "review.db"), str(tmp_path / "files")),
        suggestion_provider=_TableSuggester(),
    )
    ctx = RequestContext(user_id="alice", tenant_id="tenant-a", roles=["user"])
    fixture = Path("/home/renfeng_zhang/workspace/Hardware-DataBase/docs/ADAS/icd_example.xlsx")
    if not fixture.is_file():
        pytest.skip("real ICD fixture is not available")
    analysis = service.analyze_uploaded_template(
        ctx, filename="icd_example.xlsx", content=fixture.read_bytes(), template_name="ICD",
    )
    return service, ctx, analysis.template_version_id


def test_prepare_mapping_review_is_dry_run_and_returns_safe_payload():
    service = _Service(_analysis())

    review = prepare_mapping_review(service, _Context(), "analysis-1")

    assert review["analysis_id"] == "analysis-1"
    assert review["target_unit_ids"] == ["sheet:Review!B2"]
    assert review["overwrite_unit_ids"] == []
    assert review["decision"]["status"] == "auto_accepted"
    assert review["scope"]["ranges"] == ["Review!B2:B2"]
    assert service.corrected is None


def test_apply_mapping_review_requires_explicit_consent_and_confirms_exact_revision():
    service = _Service(_analysis())
    review = prepare_mapping_review(service, "ctx", "analysis-1")
    review["consent"] = "允许替换模板示例数据"

    result = apply_mapping_review(service, _Context(), review)

    assert result == {"status": "approved", "analysis_id": "corrected-1"}
    assert service.corrected.expected_content_hash == "a" * 64
    assert service.confirmed == ("corrected-1", "Review", None)


def test_apply_mapping_review_rejects_missing_or_stale_review():
    service = _Service(_analysis())
    review = prepare_mapping_review(service, _Context(), "analysis-1")
    review["consent"] = "不允许"
    with pytest.raises(PermissionError):
        apply_mapping_review(service, _Context(), review)
    review["consent"] = "允许替换模板示例数据"
    review["content_hash"] = "b" * 64
    with pytest.raises(ValueError, match="stale"):
        apply_mapping_review(service, _Context(), review)


def test_prepare_mapping_review_does_not_offer_protected_target_consent():
    analysis = _analysis()
    analysis.units[0] = analysis.units[0].model_copy(update={"writable": False, "blocked_reason": "protected"})
    service = _Service(analysis)

    assert prepare_mapping_review(service, _Context(), "analysis-1") is None
    assert service.corrected is None


def test_real_uploaded_table_activates_only_after_consent(tmp_path):
    service, ctx, template_id = _service_with_table(tmp_path)

    review = prepare_mapping_review(service, ctx, template_id)

    assert review is not None
    assert review["counts"] == {"targets": 133, "sample_overwrites": 62}
    assert service.store.list_templates()[0].status == "draft"
    review["consent"] = "允许替换模板示例数据"

    template = apply_mapping_review(service, ctx, review)

    assert template.status == "approved"
    schema = service.store.get_document_schema(template.template_schema_id, template.template_schema_version)
    assert schema is not None and schema.document_type == "icd"
