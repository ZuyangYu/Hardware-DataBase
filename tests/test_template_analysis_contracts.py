import hashlib
import json
import sqlite3

import pytest

from src.document_authoring.models import TemplateSecurityReport, TemplateVersion
from src.document_authoring.template_analysis import (
    DocxRegionSchema,
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)
from src.document_authoring.work_order_store import DocumentAuthoringStore


def _docx_analysis(template_version_id: str) -> TemplateAnalysis:
    return TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id=template_version_id,
        content_hash="a" * 64,
        format="docx",
        status="ready_for_confirmation",
        units=[TemplateAnalysisUnit(
            unit_id="paragraph-1", locator={"paragraph_index": 1}, writable=True,
        )],
    )


def _save_docx_template(store: DocumentAuthoringStore, template_version_id: str, content_hash: str) -> None:
    store.save_template(
        TemplateVersion(
            template_version_id=template_version_id,
            template_id="document-template",
            format="docx",
            content_hash=content_hash,
            template_schema_id="docx-schema",
            template_schema_version="1",
            renderer_policy_id="policy-1",
        ),
        b"docx fixture",
        TemplateSecurityReport(
            report_id="security-1", content_hash=content_hash, format="docx",
        ),
    )


def test_template_analysis_rejects_suggested_location_not_in_inventory():
    analysis = TemplateAnalysis(
        analysis_id="analysis-1", template_version_id="template-1", content_hash="a" * 64,
        format="docx", status="ready_for_confirmation",
        units=[TemplateAnalysisUnit(unit_id="paragraph-1", locator={"paragraph_index": 1}, writable=True)],
        suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="summary", label="摘要", target_unit_ids=["paragraph-99"], confidence=0.9,
        )],
    )

    with pytest.raises(ValueError, match="unknown analysis unit"):
        analysis.validate_suggestions()


def test_template_analysis_rejects_non_writable_suggestion_target():
    analysis = TemplateAnalysis(
        analysis_id="analysis-1", template_version_id="template-1", content_hash="a" * 64,
        format="docx", status="ready_for_confirmation",
        units=[TemplateAnalysisUnit(unit_id="paragraph-1", locator={"paragraph_index": 1})],
        suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="summary", label="摘要", target_unit_ids=["paragraph-1"], confidence=0.9,
        )],
    )

    with pytest.raises(PermissionError, match="non-writable analysis unit"):
        analysis.validate_suggestions()


def test_template_analysis_rejects_duplicate_suggestion_targets():
    analysis = TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash="a" * 64,
        format="docx",
        status="ready_for_confirmation",
        units=[
            TemplateAnalysisUnit(
                unit_id="paragraph-1",
                locator={"paragraph_index": 1},
                writable=True,
            ),
        ],
        suggestions=[
            TemplateAnalysisSuggestion(
                semantic_unit_id="summary",
                label="摘要",
                target_unit_ids=["paragraph-1"],
                confidence=0.9,
            ),
            TemplateAnalysisSuggestion(
                semantic_unit_id="detail",
                label="详情",
                target_unit_ids=["paragraph-1"],
                confidence=0.8,
            ),
        ],
    )

    with pytest.raises(ValueError, match="suggestion target may only be used once: paragraph-1"):
        analysis.validate_suggestions()


def test_template_analysis_rejects_duplicate_suggestion_semantic_ids():
    analysis = TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash="a" * 64,
        format="docx",
        status="ready_for_confirmation",
        units=[
            TemplateAnalysisUnit(
                unit_id="paragraph-1",
                locator={"paragraph_index": 1},
                writable=True,
            ),
            TemplateAnalysisUnit(
                unit_id="paragraph-2",
                locator={"paragraph_index": 2},
                writable=True,
            ),
        ],
        suggestions=[
            TemplateAnalysisSuggestion(
                semantic_unit_id="summary",
                label="摘要",
                target_unit_ids=["paragraph-1"],
                confidence=0.9,
            ),
            TemplateAnalysisSuggestion(
                semantic_unit_id="summary",
                label="摘要补充",
                target_unit_ids=["paragraph-2"],
                confidence=0.8,
            ),
        ],
    )

    with pytest.raises(ValueError, match="semantic unit may only be suggested once: summary"):
        analysis.validate_suggestions()


def test_template_analysis_rejects_duplicate_targets_within_one_suggestion():
    analysis = TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash="a" * 64,
        format="docx",
        status="ready_for_confirmation",
        units=[
            TemplateAnalysisUnit(
                unit_id="paragraph-1",
                locator={"paragraph_index": 1},
                writable=True,
            ),
        ],
        suggestions=[
            TemplateAnalysisSuggestion(
                semantic_unit_id="summary",
                label="摘要",
                target_unit_ids=["paragraph-1", "paragraph-1"],
                confidence=0.9,
            ),
        ],
    )

    with pytest.raises(ValueError, match="suggestion target may only be used once: paragraph-1"):
        analysis.validate_suggestions()


def test_store_round_trips_hash_bound_template_analysis(tmp_path):
    store = DocumentAuthoringStore(db_path=str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts"))
    analysis = _docx_analysis("template-1")
    _save_docx_template(store, analysis.template_version_id, analysis.content_hash)

    saved = store.save_template_analysis(analysis)

    assert store.get_template_analysis("template-1").content_hash == saved.content_hash


def test_store_rejects_template_analysis_with_mismatched_template_hash(tmp_path):
    store = DocumentAuthoringStore(db_path=str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts"))
    _save_docx_template(store, "template-1", hashlib.sha256(b"different document").hexdigest())

    with pytest.raises(ValueError, match="content hash does not match"):
        store.save_template_analysis(_docx_analysis("template-1"))


def test_store_rejects_template_analysis_for_the_wrong_template_format(tmp_path):
    store = DocumentAuthoringStore(db_path=str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts"))
    analysis = _docx_analysis("template-1")
    _save_docx_template(store, analysis.template_version_id, analysis.content_hash)
    template = store.get_template(analysis.template_version_id)
    store.replace_template(template.model_copy(update={"format": "xlsx"}))

    with pytest.raises(ValueError, match="format does not match"):
        store.save_template_analysis(analysis)


def test_store_rejects_invalid_suggestions_before_persisting_analysis(tmp_path):
    store = DocumentAuthoringStore(db_path=str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts"))
    analysis = _docx_analysis("template-1").model_copy(update={
        "suggestions": [TemplateAnalysisSuggestion(
            semantic_unit_id="summary", label="摘要", target_unit_ids=["missing"], confidence=0.9,
        )],
    })
    _save_docx_template(store, analysis.template_version_id, analysis.content_hash)

    with pytest.raises(ValueError, match="unknown analysis unit"):
        store.save_template_analysis(analysis)


def test_store_rejects_analysis_when_serialized_hash_is_tampered(tmp_path):
    db_path = tmp_path / "authoring.db"
    store = DocumentAuthoringStore(db_path=str(db_path), artifact_root=str(tmp_path / "artifacts"))
    analysis = _docx_analysis("template-1")
    _save_docx_template(store, analysis.template_version_id, analysis.content_hash)
    store.save_template_analysis(analysis)
    payload = analysis.model_dump(mode="json")
    payload["content_hash"] = "b" * 64
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE template_analyses SET payload_json = ? WHERE template_version_id = ?",
            (json.dumps(payload), analysis.template_version_id),
        )

    with pytest.raises(ValueError, match="content hash does not match"):
        store.get_template_analysis(analysis.template_version_id)


def test_store_rejects_analysis_when_serialized_template_id_is_tampered(tmp_path):
    db_path = tmp_path / "authoring.db"
    store = DocumentAuthoringStore(db_path=str(db_path), artifact_root=str(tmp_path / "artifacts"))
    analysis = _docx_analysis("template-1")
    _save_docx_template(store, analysis.template_version_id, analysis.content_hash)
    store.save_template_analysis(analysis)
    payload = analysis.model_dump(mode="json")
    payload["template_version_id"] = "template-2"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE template_analyses SET payload_json = ? WHERE template_version_id = ?",
            (json.dumps(payload), analysis.template_version_id),
        )

    with pytest.raises(ValueError, match="template version does not match"):
        store.get_template_analysis(analysis.template_version_id)


def test_docx_region_rejects_protected_role_with_writable_policy():
    with pytest.raises(ValueError, match="protected DOCX regions"):
        DocxRegionSchema(
            region_id="approval", locator={"paragraph_index": 0}, role="human_approval",
            write_policy="validated_draft", value_type="text",
        )
