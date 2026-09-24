from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import zipfile

import src.document_authoring.icd_comparison as icd_comparison
import src.evaluation.document_generation_metrics as metrics_module
from src.pipelines.spreadsheet.xlsx_parser import ParsedSheet, ParsedWorkbook


FIXTURE_DIR = Path("tests/fixtures/document_authoring/icd_pin_definition")
FIXTURE = FIXTURE_DIR / "pin_definition_baseline.json"
THRESHOLDS = FIXTURE_DIR / "baseline_thresholds.json"


def _api(module, name: str):
    function = getattr(module, name, None)
    assert callable(function), f"Task 7 API is missing: {name}"
    return function


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _workbook(spec: dict) -> ParsedWorkbook:
    return ParsedWorkbook(
        file_name=spec["file_name"],
        sheets=[ParsedSheet(name="Pin Definition", rows=spec["rows"])],
    )


def _package(*, macro: bool) -> bytes:
    content_type = (
        "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
        if macro
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    )
    rels = (
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
    )
    if macro:
        rels += (
            '<Relationship Id="rVba" '
            'Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" '
            'Target="vbaProject.bin"/>'
        )
    entries = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Override PartName="/xl/workbook.xml" ContentType="{content_type}"/>'
            "</Types>"
        ).encode(),
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<workbookProtection lockStructure="1"/>'
            '<sheets><sheet name="Pin Definition" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ).encode(),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels}</Relationships>"
        ).encode(),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetProtection sheet="1"/><sheetData><row r="1">'
            '<c r="A1" t="inlineStr"><is><t>Pin Definition</t></is></c>'
            '</row></sheetData><mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>'
            "</worksheet>"
        ).encode(),
    }
    if macro:
        entries["xl/vbaProject.bin"] = b"synthetic-non-executable-vba-sentinel"
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_fixture_is_portable_and_records_provenance_without_customer_workbook_bytes():
    fixture = _load_fixture()
    assert fixture["provenance"]["kind"] == "synthetic-reviewed"
    assert fixture["provenance"]["source"].startswith("Phase 2 Task 7")
    assert fixture["human"]["file_name"].endswith(".xlsx")
    assert fixture["xlsm"]["file_name"].endswith(".xlsm")
    assert not list(FIXTURE_DIR.glob("*.xlsm"))
    assert not list(FIXTURE_DIR.glob("*.xlsx"))


def test_xlsx_and_xlsm_logical_outputs_match_the_human_pin_definition_baseline():
    fixture = _load_fixture()
    compare = _api(icd_comparison, "compare_pin_definition_workbooks")
    reference = _workbook(fixture["human"])
    reference_evidence = fixture["human"]["evidence_by_key"]

    xlsx_result = compare(
        reference,
        _workbook(fixture["xlsx"]),
        reference_evidence=reference_evidence,
        generated_evidence=fixture["xlsx"]["evidence_by_key"],
        reference_package=_package(macro=False),
        generated_package=_package(macro=False),
        reference_format="xlsx",
        generated_format="xlsx",
    )
    xlsm_result = compare(
        reference,
        _workbook(fixture["xlsm"]),
        reference_evidence=reference_evidence,
        generated_evidence=fixture["xlsm"]["evidence_by_key"],
        reference_package=_package(macro=False),
        generated_package=_package(macro=True),
        reference_format="xlsx",
        generated_format="xlsm",
    )

    for result in (xlsx_result, xlsm_result):
        assert result["metrics"]["row_key_f1"]["value"] == 1.0
        assert result["metrics"]["required_column_completeness"]["value"] == 1.0
        assert result["metrics"]["evidence_support_rate"]["value"] == 1.0
        assert result["metrics"]["physical_protection_rate"]["value"] == 1.0
        assert result["package"]["preservation_ok"] is True

    assert (
        xlsx_result["normalized"]["generated"]["row_keys"]
        == xlsm_result["normalized"]["generated"]["row_keys"]
    )


def test_benchmark_metrics_are_versioned_bound_to_fixture_and_thresholds_fail_closed():
    fixture = _load_fixture()
    compare = _api(icd_comparison, "compare_pin_definition_workbooks")
    aggregate = _api(metrics_module, "aggregate_document_authoring_parity_metrics")
    load_thresholds = _api(metrics_module, "load_parity_thresholds")
    evaluate = _api(metrics_module, "evaluate_parity_thresholds")
    reference = _workbook(fixture["human"])
    results = [
        compare(
            reference,
            _workbook(fixture[name]),
            reference_evidence=fixture["human"]["evidence_by_key"],
            generated_evidence=fixture[name]["evidence_by_key"],
            reference_package=_package(macro=False),
            generated_package=_package(macro=name == "xlsm"),
            reference_format="xlsx",
            generated_format=name,
        )
        for name in ("xlsx", "xlsm")
    ]

    report = aggregate(results, fixture_id=fixture["fixture_id"])
    thresholds = load_thresholds(THRESHOLDS)
    gate = evaluate(report, thresholds)

    assert gate["passed"] is True
    assert report["metric_version"] == fixture["metric_version"]
    assert report["fixture_id"] == fixture["fixture_id"]
    assert report["metrics"]["row_key_f1"].denominator == 12
    assert report["metrics"]["physical_protection_rate"].denominator == 2
    assert thresholds["threshold_version"] == "icd-pin-definition-thresholds-v1"

    degraded = dict(report)
    degraded["metrics"] = dict(report["metrics"])
    degraded["metrics"]["row_key_recall"] = degraded["metrics"][
        "row_key_recall"
    ].model_copy(update={"value": 0.5})
    degraded_gate = evaluate(degraded, thresholds)
    assert degraded_gate["passed"] is False
    assert "row_key_recall" in degraded_gate["failures"]


def test_legacy_field_metrics_remain_unversioned_and_keep_their_existing_contract():
    observation = metrics_module.FieldObservation(
        record_id="r-1",
        field_id="f-1",
        required=True,
        success=True,
        typed_value_ok=True,
    )
    legacy = metrics_module.aggregate_document_generation_metrics([observation])

    assert legacy["field_success_rate"].value == 1.0
    assert legacy["field_success_rate"].metric_version is None
    assert "row_key_f1" not in legacy
