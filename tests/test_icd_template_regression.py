from __future__ import annotations

from pathlib import Path

import pytest

from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.template_analysis import TemplateAnalysisSuggestion
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext


def _icd_template_path() -> Path:
    test_path = Path(__file__).resolve()
    candidates = (
        test_path.parents[1] / "docs" / "ADAS" / "icd_example.xlsx",
        test_path.parents[3] / "docs" / "ADAS" / "icd_example.xlsx",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip("real ICD regression fixture is not available")


class _ObservedUnsafeIcdSuggestion:
    def suggest(self, analysis):
        targets = [unit.unit_id for unit in analysis.units if unit.writable]
        if len(targets) < 241:
            raise AssertionError(f"expected at least 241 writable ICD cells, got {len(targets)}")
        slices = (
            ("header_row", 7),
            ("subheader_row2", 5),
            ("subtotal_row3", 5),
            ("data_rows", 224),
        )
        offset = 0
        suggestions = []
        for semantic_unit_id, count in slices:
            suggestions.append(TemplateAnalysisSuggestion(
                semantic_unit_id=semantic_unit_id,
                label=semantic_unit_id,
                target_unit_ids=targets[offset:offset + count],
                retrieval_terms=[semantic_unit_id],
                confidence=0.99,
                value_shape="scalar",
            ))
            offset += count
        analysis.suggestions = suggestions
        return suggestions


def test_icd_241_cell_scalar_mapping_requires_review_before_region_creation(
    tmp_path: Path,
):
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(
        store=store,
        suggestion_provider=_ObservedUnsafeIcdSuggestion(),
    )
    ctx = RequestContext(user_id="alice", tenant_id="tenant-a", roles=["user"])

    with pytest.raises(ValueError, match="automatic template activation failed"):
        service.analyze_and_activate_uploaded_template(
            ctx,
            filename="icd_example.xlsx",
            content=_icd_template_path().read_bytes(),
            template_name="ICD",
        )

    template = store.list_templates()[0]
    analysis = store.get_template_analysis(template.template_version_id)
    assert template.status == "draft"
    assert analysis is not None
    assert analysis.status == "requires_human"
    assert analysis.activation_decision is not None
    assert "scalar_target_fanout" in analysis.activation_decision.reason_codes
    assert analysis.activation_decision.metrics.target_count == 241
    assert store.list_workbook_regions(
        template.template_schema_id,
        template.template_schema_version,
    ) == []
