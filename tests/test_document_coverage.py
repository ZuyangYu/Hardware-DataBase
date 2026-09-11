"""Task 4 coverage contract tests."""

from __future__ import annotations

from src.document_authoring.models import DocumentUnitDraft, DraftAssertion, TypedFieldValue, TypedTableRow
from src.document_authoring.planning.models import CoverageContract, CoverageRequirement
from src.document_authoring.planning.coverage import CoverageEvaluator


EVIDENCE = {
    "e1": {"id": "e1", "content": "Voltage is 12 V", "metadata": {}},
    "e2": {"id": "e2", "content": "J1:1 CAN_TX P13.0", "metadata": {}},
    "e3": {"id": "e3", "content": "J1:2 CAN_RX P13.1", "metadata": {}},
}


def _scalar(unit_id: str = "field:voltage", value: str = "12 V") -> DocumentUnitDraft:
    return DocumentUnitDraft(
        unit_id=unit_id,
        run_id="run-1",
        generated_by="managed_writer",
        content=f"Voltage is {value}",
        evidence_ids=["e1"],
        typed_value=TypedFieldValue(
            kind="scalar", normalized_values=[value], display_value=value,
            evidence_ids=["e1"],
        ),
        assertions=[DraftAssertion(
            assertion_id=f"a-{unit_id}", claim_id=unit_id,
            text=f"Voltage is {value}", evidence_ids=["e1"],
        )],
        validation_status="supported",
    )


def _table(rows: list[TypedTableRow]) -> DocumentUnitDraft:
    return DocumentUnitDraft(
        unit_id="field:interfaces",
        run_id="run-1",
        generated_by="managed_writer",
        content="Interfaces",
        evidence_ids=["e2", "e3"],
        typed_value=TypedFieldValue(
            kind="table", normalized_values=[row.row_key for row in rows],
            display_value=f"{len(rows)} rows", evidence_ids=["e2", "e3"], rows=rows,
        ),
        assertions=[DraftAssertion(
            assertion_id="a-interfaces", claim_id="interfaces",
            text="J1:1 CAN_TX P13.0", evidence_ids=["e2"],
        )],
        validation_status="supported",
    )


def _contract() -> CoverageContract:
    return CoverageContract(requirements=[
        CoverageRequirement(
            requirement_id="voltage", unit_id="voltage", kind="scalar",
            min_evidence_items=1,
        ),
        CoverageRequirement(
            requirement_id="interfaces", unit_id="interfaces", kind="table",
            row_keys=["J1:1", "J1:2"], required_columns=["signal", "pin"],
        ),
    ])


def test_complete_scalar_and_typed_table_produce_covered_report():
    rows = [
        TypedTableRow(
            row_key="J1:1", cells={"signal": "CAN_TX", "pin": "P13.0"},
            evidence_ids=["e2"], cell_evidence_ids={"signal": ["e2"], "pin": ["e2"]},
        ),
        TypedTableRow(
            row_key="J1:2", cells={"signal": "CAN_RX", "pin": "P13.1"},
            evidence_ids=["e3"], cell_evidence_ids={"signal": ["e3"], "pin": ["e3"]},
        ),
    ]

    report = CoverageEvaluator().evaluate(
        _contract(),
        {"field:voltage": _scalar(), "field:interfaces": _table(rows)},
        EVIDENCE,
    )

    assert report.expected_count == 2
    assert report.covered_count == 2
    assert report.missing_count == 0
    assert report.unsupported_count == 0
    assert report.duplicate_count == 0
    assert report.requirement_results["voltage"].status == "complete"
    assert report.requirement_results["interfaces"].status == "complete"
    assert report.report_hash


def test_table_coverage_reports_missing_unexpected_duplicate_rows_and_columns():
    rows = [
        TypedTableRow(
            row_key="J1:1", cells={"signal": "CAN_TX"}, evidence_ids=["e2"],
            cell_evidence_ids={"signal": ["e2"]},
        ),
        TypedTableRow(
            row_key="J1:1", cells={"signal": "CAN_TX", "pin": "P13.0"}, evidence_ids=["e2"],
            cell_evidence_ids={"signal": ["e2"], "pin": ["e2"]},
        ),
        TypedTableRow(
            row_key="J1:3", cells={"signal": "CAN_RX", "pin": "P13.1"}, evidence_ids=["e3"],
            cell_evidence_ids={"signal": ["e3"], "pin": ["e3"]},
        ),
    ]

    result = CoverageEvaluator().evaluate(
        _contract(), {"interfaces": _table(rows)}, EVIDENCE,
    ).requirement_results["interfaces"]

    assert result.status in {"partial", "unsupported"}
    assert result.missing_row_keys == ["J1:2"]
    assert result.unexpected_row_keys == ["J1:3"]
    assert result.duplicate_count == 1
    assert "pin" in result.missing_columns
    assert {issue.row_key for issue in result.issues if issue.row_key} >= {"J1:1", "J1:3"}


def test_coverage_rejects_unsupported_evidence_and_table_scalarization():
    scalarized = _table([]).model_copy(update={
        "typed_value": TypedFieldValue(
            kind="scalar", normalized_values=["J1:1"], display_value="J1:1",
            evidence_ids=["e2"],
        ),
    })
    report = CoverageEvaluator().evaluate(
        _contract(), {"interfaces": scalarized}, EVIDENCE,
    )
    result = report.requirement_results["interfaces"]
    assert result.status == "unsupported"
    assert any(issue.code == "table_scalarized" for issue in result.issues)

    unsupported = _scalar().model_copy(update={
        "typed_value": TypedFieldValue(
            kind="scalar", normalized_values=["12 V"], display_value="12 V",
            evidence_ids=["unknown"],
        ),
        "evidence_ids": ["unknown"],
    })
    result = CoverageEvaluator().evaluate(
        _contract(), {"voltage": unsupported}, EVIDENCE,
    ).requirement_results["voltage"]
    assert result.status == "unsupported"
    assert any(issue.code == "unknown_evidence" for issue in result.issues)


def test_cross_unit_conflicts_are_explicit_and_report_hash_is_order_stable():
    contract = CoverageContract(requirements=[CoverageRequirement(
        requirement_id="cross-voltage", unit_id="voltage", kind="cross_unit",
    )])
    first = _scalar("field:a", "12 V").model_copy(update={
        "assertions": [DraftAssertion(
            assertion_id="a", claim_id="a", text="Voltage 12 V", evidence_ids=["e1"],
            value="12 V", consistency_key="voltage",
        )],
    })
    second = _scalar("field:b", "24 V").model_copy(update={
        "assertions": [DraftAssertion(
            assertion_id="b", claim_id="b", text="Voltage 24 V", evidence_ids=["e1"],
            value="24 V", consistency_key="voltage",
        )],
    })
    evaluator = CoverageEvaluator()
    report = evaluator.evaluate(contract, {"a": first, "b": second}, EVIDENCE)
    reordered = evaluator.evaluate(contract, {"b": second, "a": first}, EVIDENCE)

    assert report.requirement_results["cross-voltage"].status == "conflicting"
    assert any(issue.code == "cross_unit_conflict" for issue in report.requirement_results["cross-voltage"].issues)
    assert report.report_hash == reordered.report_hash



def test_table_without_declared_row_keys_accepts_server_owned_identities():
    """Frozen connector rows are legitimate when the contract declares no keys."""
    contract = CoverageContract(requirements=[
        CoverageRequirement(
            requirement_id="interfaces", unit_id="interfaces", kind="table",
            row_keys=[], required_columns=["signal", "pin"],
        ),
    ])
    rows = [
        TypedTableRow(
            row_key="X1900-1", cells={"signal": "CAN_TX", "pin": "P13.0"},
            evidence_ids=["e2"], cell_evidence_ids={"signal": ["e2"], "pin": ["e2"]},
        ),
        TypedTableRow(
            row_key="X1900-2", cells={"signal": "CAN_RX", "pin": "P13.1"},
            evidence_ids=["e3"], cell_evidence_ids={"signal": ["e3"], "pin": ["e3"]},
        ),
    ]

    result = CoverageEvaluator().evaluate(
        contract, {"interfaces": _table(rows)}, EVIDENCE,
    ).requirement_results["interfaces"]

    assert result.status == "complete", [issue.code for issue in result.issues]
    assert result.unexpected_row_keys == []
    assert result.unexpected_row_keys == [] and result.missing_row_keys == []
