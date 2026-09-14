"""Task 5 strict, format-neutral document model tests."""

from __future__ import annotations

import pytest

from src.document_authoring.document_model import (
    Citation,
    DocumentModel,
    MissingItem,
    ParagraphBlock,
    TypedTableBlock,
)
from src.document_authoring.models import TypedTableRow


def test_document_model_preserves_typed_table_rows_citations_and_missing_items():
    model = DocumentModel(
        document_id="doc-1", plan_id="plan-1", plan_version=1, plan_hash="sha256:plan",
        blocks=[
            ParagraphBlock(
                block_id="block-cover", unit_id="cover", content="Verified cover",
                citations=[Citation(evidence_ids=["e1"], claim_id="cover")],
            ),
            TypedTableBlock(
                block_id="block-pins", unit_id="pins",
                columns=["connector", "pin", "signal"],
                rows=[TypedTableRow(
                    row_key="J1:1",
                    cells={"connector": "J1", "pin": "1", "signal": "CAN_TX"},
                    evidence_ids=["e2"],
                    cell_evidence_ids={
                        "connector": ["e2"], "pin": ["e2"], "signal": ["e2"],
                    },
                )],
                expected_row_keys=["J1:1"],
            ),
        ],
        missing_items=[MissingItem(
            unit_id="pins", row_key="J1:2", column_id="signal",
            reason="no allowed evidence", required=True,
        )],
        citations=[Citation(evidence_ids=["e1", "e2"], claim_id="document")],
    )

    round_trip = DocumentModel.model_validate(model.model_dump(mode="json"))

    assert round_trip.blocks[1].rows[0].row_key == "J1:1"
    assert round_trip.missing_items[0].row_key == "J1:2"
    assert round_trip.model_hash == model.model_hash
    assert model.model_hash


def test_document_model_rejects_duplicate_row_identity_and_raw_source_fields():
    with pytest.raises(ValueError, match="row key"):
        TypedTableBlock(
            block_id="block-pins", unit_id="pins", columns=["pin"],
            rows=[
                TypedTableRow(row_key="J1:1", cells={"pin": "1"}),
                TypedTableRow(row_key="J1:1", cells={"pin": "2"}),
            ],
        )

    with pytest.raises(ValueError):
        DocumentModel.model_validate({
            "document_id": "doc-1",
            "blocks": [{
                "kind": "paragraph", "block_id": "b", "unit_id": "u",
                "content": "ok", "evidence_content": "must not persist",
            }],
        })


def test_document_model_hash_is_stable_when_mapping_order_changes():
    first = DocumentModel(
        document_id="doc-1", blocks=[ParagraphBlock(
            block_id="b", unit_id="u", content="value",
            citations=[Citation(evidence_ids=["e1"], claim_id="c")],
        )],
        layout_hints={"u": {"region": "r1", "column": "A"}},
    )
    second = DocumentModel(
        document_id="doc-1", blocks=[ParagraphBlock(
            block_id="b", unit_id="u", content="value",
            citations=[Citation(claim_id="c", evidence_ids=["e1"])],
        )],
        layout_hints={"u": {"column": "A", "region": "r1"}},
    )

    assert first.model_hash == second.model_hash

