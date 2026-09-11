"""Coverage panel projection: schema labels + unit statuses + evidence matrix."""
from __future__ import annotations

from types import SimpleNamespace

from src.core.app_pipeline import AppPipeline, _completed_document_coverage_is_valid
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    ReviewItemSchema,
)


def _schema() -> DocumentSchema:
    return DocumentSchema.model_validate({
        "document_schema_id": "ds-1", "version": "1", "document_type": "ICD",
        "status": "approved", "execution_mode": "internal_harness",
        "fields": [
            DocumentFieldSchema.model_validate({
                "field_id": "rated_current", "label": "额定电流",
                "retrieval_policy_id": "r-1", "verification_policy_id": "v-1",
                "required": True,
            }),
            DocumentFieldSchema.model_validate({
                "field_id": "pin_map", "label": "管脚定义",
                "retrieval_policy_id": "r-2", "verification_policy_id": "v-2",
                "required": False,
            }),
        ],
        "review_items": [
            ReviewItemSchema.model_validate({
                "review_item_id": "unit_consistency", "label": "单位一致性",
                "evaluation_mode": "deterministic_auto",
                "retrieval_rule_id": "rule-1", "deterministic_rule_id": "det-1",
                "pass_policy_id": "pass-1",
            }),
        ],
    })


def _pipeline(matrix_rows: list[dict]) -> AppPipeline:
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(store=SimpleNamespace(
        get_document_schema=lambda _schema_id, _version: _schema(),
        get_evidence_matrix=lambda _work_order_id: matrix_rows,
    ))
    return pipeline


def _order(unit_statuses: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        work_order_id="wo-1", document_schema_id="ds-1", document_schema_version="1",
        unit_statuses=unit_statuses,
    )


def test_coverage_block_combines_schema_statuses_and_matrix():
    pipeline = _pipeline([
        {
            "field_id": "rated_current", "review_item_id": None,
            "coverage_status": "supported", "display_value": "10 A",
            "evidence_ids": ["e1", "e2"],
        },
        {
            "field_id": None, "review_item_id": "unit_consistency",
            "coverage_status": "missing", "display_value": "",
            "evidence_ids": [],
        },
    ])
    coverage = pipeline._document_coverage_block(_order({
        "field:rated_current": "ready_to_render",
        "field:pin_map": "insufficient_evidence",
        "review:unit_consistency": "passed",
    }))

    assert coverage["total"] == 3
    assert coverage["summary"] == {"covered": 2, "missing": 1, "conflicting": 0, "failed": 0, "pending": 0}
    rated = next(entry for entry in coverage["fields"] if entry["field_id"] == "rated_current")
    assert rated == {
        "kind": "field", "field_id": "rated_current", "unit_id": "field:rated_current",
        "label": "额定电流", "required": True, "status": "ready_to_render",
        "coverage_status": "supported", "display_value": "10 A", "evidence_count": 2,
    }
    pin = next(entry for entry in coverage["fields"] if entry["field_id"] == "pin_map")
    assert pin["status"] == "insufficient_evidence"
    assert pin["required"] is False
    assert pin["coverage_status"] is None
    review = next(entry for entry in coverage["fields"] if entry["kind"] == "review")
    assert review["label"] == "单位一致性"
    assert review["coverage_status"] == "missing"


def test_coverage_block_survives_missing_schema_and_matrix_and_extras():
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(store=SimpleNamespace(
        get_document_schema=lambda _schema_id, _version: (_ for _ in ()).throw(RuntimeError("db closed")),
        get_evidence_matrix=lambda _work_order_id: (_ for _ in ()).throw(RuntimeError("db closed")),
    ))

    coverage = pipeline._document_coverage_block(_order({
        "field:extra_signal": "conflicting",
        "field:late_field": "planned",
    }))

    assert coverage["summary"] == {"covered": 0, "missing": 0, "conflicting": 1, "failed": 0, "pending": 1}
    extra = next(entry for entry in coverage["fields"] if entry["field_id"] == "extra_signal")
    assert extra["label"] == "extra_signal"
    assert extra["coverage_status"] is None
    assert extra["evidence_count"] == 0


def test_coverage_block_does_not_duplicate_schema_ids_that_already_include_kind_prefix():
    schema = DocumentSchema.model_validate({
        "document_schema_id": "ds-prefixed", "version": "1", "document_type": "ICD",
        "status": "approved", "execution_mode": "internal_harness",
        "fields": [DocumentFieldSchema.model_validate({
            "field_id": "field:sheet:Sheet1!C16", "label": "Sheet1!C16",
            "retrieval_policy_id": "r-1", "verification_policy_id": "v-1",
            "required": True,
        })],
        "review_items": [],
    })
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(store=SimpleNamespace(
        get_document_schema=lambda _schema_id, _version: schema,
        get_evidence_matrix=lambda _work_order_id: [{
            "field_id": "field:sheet:Sheet1!C16",
            "coverage_status": "supported", "display_value": "Power supply",
            "evidence_ids": ["e1"],
        }],
    ))

    coverage = pipeline._document_coverage_block(SimpleNamespace(
        work_order_id="wo-prefixed", document_schema_id="ds-prefixed",
        document_schema_version="1",
        unit_statuses={"field:sheet:Sheet1!C16": "ready_to_render"},
    ))

    assert coverage["total"] == 1
    assert coverage["summary"]["covered"] == 1
    assert coverage["fields"][0]["display_value"] == "Power supply"


def test_coverage_buckets_cover_every_known_status():
    cases = {
        "ready_to_render": "covered", "passed": "covered",
        "tbd": "missing", "insufficient_evidence": "missing",
        "conflicting": "conflicting",
        "retrieval_failed": "failed", "failed": "failed", "blocked": "failed",
        "planned": "pending", "requires_human": "pending", "": "pending",
    }
    for status, bucket in cases.items():
        assert AppPipeline._coverage_bucket(status) == bucket, status


def test_completed_projection_requires_every_required_unit_to_be_covered():
    assert _completed_document_coverage_is_valid({
        "fields": [{"required": True, "status": "ready_to_render"}],
    })
    assert not _completed_document_coverage_is_valid({
        "fields": [{"required": True, "status": "planned"}],
    })
