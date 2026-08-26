from src.document_authoring.icd_comparison import compare_workbooks
from src.pipelines.spreadsheet.xlsx_parser import ParsedSheet, ParsedWorkbook


def _workbook(rows):
    return ParsedWorkbook(
        file_name="icd.xlsx",
        sheets=[ParsedSheet(name="Pin Definition", rows=rows)],
    )


def _workbook_with_sheets(*sheets):
    return ParsedWorkbook(
        file_name="icd.xlsx",
        sheets=[ParsedSheet(name=name, rows=rows) for name, rows in sheets],
    )


def test_comparison_reports_matching_mismatched_and_generated_only_pins():
    reference = _workbook([
        ["Connector", "Pin Number", "Pin Definition"],
        ["J1", "1", "CAN_H"],
        ["J1", "2", "NC"],
    ])
    generated = _workbook([
        ["Connector", "Pin Number", "Pin Definition"],
        ["J1", "1", "CAN_H"],
        ["J1", "2", "GND"],
        ["J1", "3", "ETH_P"],
    ])

    result = compare_workbooks(reference, generated)

    assert result["summary"] == {
        "reference_pin_count": 2,
        "generated_pin_count": 3,
        "matching_pin_count": 1,
        "mismatched_pin_count": 1,
        "reference_only_pin_count": 0,
        "generated_only_pin_count": 1,
        "exact_match_rate": 0.5,
        "reference_coverage": 1.0,
    }
    assert result["mismatched"] == [{
        "key": "j1:2",
        "reference_definition": "nc",
        "generated_definition": "gnd",
    }]
    assert result["generated_only"] == [{
        "key": "j1:3",
        "definition": "eth_p",
    }]
    assert result["warnings"] == []


def test_comparison_warns_when_a_workbook_has_no_recognized_pin_table():
    result = compare_workbooks(
        _workbook([["项目名称", "说明"], ["项目 A", "无管脚表"]]),
        _workbook([["Connector", "Pin Number", "Pin Definition"], ["J2", "1", "CAN_L"]]),
    )

    assert result["summary"]["reference_pin_count"] == 0
    assert result["warnings"] == ["人工 ICD 未发现可识别的管脚表。"]


def test_comparison_recognizes_bilingual_template_headers():
    reference = _workbook([
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["X1900-1", "I_S_WKUP"],
    ])
    generated = _workbook([
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["X1900-1", "I_S_WKUP"],
    ])

    result = compare_workbooks(reference, generated)

    assert result["summary"]["reference_pin_count"] == 1
    assert result["summary"]["matching_pin_count"] == 1
    assert result["warnings"] == []


def test_comparison_keeps_repeated_pin_numbers_separate_by_location_block():
    rows = [
        ["在控制器上编号：Location Number", "X1900"],
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["1", "CAN0H"],
        ["2", "CAN0L"],
        ["在控制器上编号：Location Number", "X1902"],
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["1", "ETH_P"],
        ["2", "ETH_N"],
    ]

    result = compare_workbooks(_workbook(rows), _workbook(rows))

    assert result["summary"]["reference_pin_count"] == 4
    assert result["summary"]["matching_pin_count"] == 4
    assert {item["key"] for item in result["matched"]} == {
        "x1900:1", "x1900:2", "x1902:1", "x1902:2",
    }


def test_comparison_ignores_template_example_sheets():
    workbook = _workbook_with_sheets(
        ("Pin Definition", [["Pin Number", "Pin Definition"], ["1", "CAN_H"]]),
        ("Example", [["Pin Number", "Pin Definition"], ["X302-1", "OLD"]]),
    )

    result = compare_workbooks(workbook, workbook)

    assert result["summary"]["reference_pin_count"] == 1
    assert result["matched"] == [{"key": "1", "definition": "can_h"}]


def test_comparison_normalizes_location_embedded_in_generated_pin_number():
    reference = _workbook([
        ["在控制器上编号：Location Number", "X1900"],
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["1", "CAN_H"],
    ])
    generated = _workbook([
        ["管脚号 Pin Number", "管脚定义 Pin Definition"],
        ["X1900-1", "CAN_H"],
    ])

    result = compare_workbooks(reference, generated)

    assert result["summary"]["matching_pin_count"] == 1
    assert result["matched"] == [{"key": "x1900:1", "definition": "can_h"}]


def test_comparison_reports_function_and_notice_coverage_separately():
    reference = _workbook([
        ["Pin Number", "Pin Definition", "Function", "Notice"],
        ["1", "CAN_H", "车身 CAN 高", "预留"],
    ])
    generated = _workbook([
        ["Pin Number", "Pin Definition", "Function", "Notice"],
        ["1", "CAN_H", "控制器接入 CAN 通讯", ""],
    ])

    result = compare_workbooks(reference, generated)

    assert result["content_quality"]["function"] == {
        "reference_nonempty_count": 1,
        "generated_nonempty_count": 1,
        "covered_count": 1,
        "exact_match_count": 0,
        "coverage": 1.0,
        "exact_match_rate": 0.0,
    }
    assert result["content_quality"]["notice"] == {
        "reference_nonempty_count": 1,
        "generated_nonempty_count": 0,
        "covered_count": 0,
        "exact_match_count": 0,
        "coverage": 0.0,
        "exact_match_rate": 0.0,
    }
