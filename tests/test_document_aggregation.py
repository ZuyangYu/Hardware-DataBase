"""Task 5 deterministic aggregation tests."""

from __future__ import annotations

from src.document_authoring.aggregation import DocumentAggregator
from src.document_authoring.models import DocumentUnitDraft, DraftAssertion, TypedFieldValue, TypedTableRow
from src.document_authoring.planning.models import CoverageContract, CoverageRequirement
from types import SimpleNamespace


def _draft(unit_id: str, *, text: str, evidence_id: str, typed=None) -> DocumentUnitDraft:
    return DocumentUnitDraft(
        unit_id=unit_id, run_id="run-1", generated_by="managed_writer", content=text,
        evidence_ids=[evidence_id], typed_value=typed,
        assertions=[DraftAssertion(
            assertion_id=f"a-{unit_id}", claim_id=unit_id, text=text,
            evidence_ids=[evidence_id],
        )], validation_status="supported",
    )


def _plan():
    return SimpleNamespace(
        document_plan_id="plan-1", version=1, plan_hash="sha256:plan",
        semantic_units=[
            SimpleNamespace(unit_id="cover", kind="section", required=True),
            SimpleNamespace(unit_id="pins", kind="table", required=True),
        ],
        coverage_contract=CoverageContract(requirements=[
            CoverageRequirement(requirement_id="cover", unit_id="cover", kind="section"),
            CoverageRequirement(
                requirement_id="pins", unit_id="pins", kind="table",
                row_keys=["J1:1", "J1:2"], required_columns=["pin", "signal"],
            ),
        ]),
    )


def test_aggregator_preserves_plan_and_row_key_order_without_scalarizing_tables():
    table = TypedFieldValue(
        kind="table", normalized_values=["J1:2", "J1:1"], display_value="2 rows",
        evidence_ids=["e2", "e3"], rows=[
            TypedTableRow(
                row_key="J1:2", cells={"pin": "2", "signal": "LIN"}, evidence_ids=["e3"],
                cell_evidence_ids={"pin": ["e3"], "signal": ["e3"]},
            ),
            TypedTableRow(
                row_key="J1:1", cells={"pin": "1", "signal": "CAN"}, evidence_ids=["e2"],
                cell_evidence_ids={"pin": ["e2"], "signal": ["e2"]},
            ),
        ],
    )
    drafts = {
        "field:pins": _draft("field:pins", text="Pins", evidence_id="e2", typed=table),
        "field:cover": _draft("field:cover", text="Cover", evidence_id="e1"),
    }

    model = DocumentAggregator().aggregate(_plan(), drafts)

    assert [block.unit_id for block in model.blocks] == ["cover", "pins"]
    table_block = model.blocks[1]
    assert table_block.kind == "table"
    assert [row.row_key for row in table_block.rows] == ["J1:1", "J1:2"]
    assert [row.cells for row in table_block.rows] == [
        {"pin": "1", "signal": "CAN"}, {"pin": "2", "signal": "LIN"},
    ]
    assert model.draft_hashes["pins"]
    assert model.plan_hash == "sha256:plan"


def test_aggregator_keeps_distinct_rows_with_same_display_values_and_marks_missing_rows():
    table = TypedFieldValue(
        kind="table", normalized_values=["J1:1", "J1:2"], display_value="2 rows",
        evidence_ids=["e2"], rows=[
            TypedTableRow(
                row_key="J1:1", cells={"pin": "1", "signal": "CAN"}, evidence_ids=["e2"],
            ),
            TypedTableRow(
                row_key="J1:2", cells={"pin": "1", "signal": "CAN"}, evidence_ids=["e2"],
            ),
        ],
    )
    model = DocumentAggregator().aggregate(
        _plan(), {
            "cover": _draft("cover", text="Cover", evidence_id="e2"),
            "pins": _draft("pins", text="Pins", evidence_id="e2", typed=table),
        },
    )

    assert len(model.blocks[1].rows) == 2
    assert model.blocks[1].rows[0].row_key != model.blocks[1].rows[1].row_key
    assert not model.missing_items

    missing = DocumentAggregator().aggregate(
        _plan(), {"cover": _draft("cover", text="Cover", evidence_id="e1")},
    )
    assert any(item.row_key == "J1:1" for item in missing.missing_items)
    assert any(item.row_key == "J1:2" for item in missing.missing_items)


def test_aggregator_preserves_prefixed_plan_unit_identity_for_template_binding():
    unit_id = "field:sheet:Sheet1!C16"
    plan = SimpleNamespace(
        document_plan_id="plan-prefixed", version=1, plan_hash="sha256:plan",
        semantic_units=[SimpleNamespace(unit_id=unit_id, kind="field", required=True)],
        coverage_contract=CoverageContract(requirements=[
            CoverageRequirement(
                requirement_id=unit_id, unit_id=unit_id, kind="paragraph",
            ),
        ]),
    )

    model = DocumentAggregator().aggregate(
        plan,
        {unit_id: _draft(unit_id, text="Controller power supply", evidence_id="e1")},
    )

    assert [block.unit_id for block in model.blocks] == [unit_id]
    assert model.blocks[0].content == "Controller power supply"
    assert not model.missing_items
