from __future__ import annotations

from unittest.mock import MagicMock

from src.core.app_pipeline import AppPipeline
from src.document_authoring.template_analysis import TemplateMappingCorrection


def test_pipeline_exposes_review_lookup_and_hash_bound_correction():
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = MagicMock()
    pipeline.document_generation.get_template_analysis_for_review.return_value = {
        "analysis_id": "analysis-1",
    }
    pipeline.document_generation.correct_template_analysis.return_value = {
        "analysis_id": "analysis-2",
    }
    correction = TemplateMappingCorrection(
        analysis_id="analysis-1",
        expected_content_hash="a" * 64,
        suggestions=[],
        actor_id="alice",
        comment="Remove unsafe mappings.",
    )

    review = pipeline.get_document_template_analysis_for_review(
        "ctx",
        analysis_id="analysis-1",
    )
    corrected = pipeline.correct_document_template_analysis(
        "ctx",
        correction=correction,
    )

    assert review == {"analysis_id": "analysis-1"}
    assert corrected == {"analysis_id": "analysis-2"}
    pipeline.document_generation.get_template_analysis_for_review.assert_called_once_with(
        "ctx",
        analysis_id="analysis-1",
    )
    pipeline.document_generation.correct_template_analysis.assert_called_once_with(
        "ctx",
        correction=correction,
    )
