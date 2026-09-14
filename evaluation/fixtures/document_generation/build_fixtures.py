"""Build deterministic, offline template fixtures for the document-generation eval.

Every file is written with fixed zip timestamps (ZIP_STORED, 2026-01-01) so the
sha256 in fixture_index.json is reproducible. Run:

    uv run python evaluation/fixtures/document_generation/build_fixtures.py
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

FIXED_DATE = (2026, 1, 1, 0, 0, 0)
FIXTURE_DIR = Path(__file__).resolve().parent

_SHEET_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<sheetData>{rows}</sheetData></worksheet>"
)

_XLSX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ),
    "xl/workbook.xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets></workbook>'
    ),
    "xl/_rels/workbook.xml.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    ),
}

_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_DOCX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rRoot" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cell(ref: str, value: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{_escape(value)}</t></is></c>'


def build_xlsx(rows: dict[str, str]) -> bytes:
    sheet_rows = []
    for index, (ref, value) in enumerate(sorted(rows.items()), start=1):
        sheet_rows.append(f'<row r="{index}">{_cell(ref, value)}</row>')
    parts = dict(_XLSX_PARTS)
    parts["xl/worksheets/sheet1.xml"] = _SHEET_XML.format(rows="".join(sheet_rows))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
            archive.writestr(info, content)
    return buffer.getvalue()


def build_docx(lines: list[str]) -> bytes:
    paragraphs = "".join(
        f"<w:p><w:r><w:t>{_escape(line)}</w:t></w:r></w:p>" for line in lines
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}<w:sectPr/></w:body></w:document>"
    )
    parts = {
        "[Content_Types].xml": _DOCX_CONTENT_TYPES,
        "_rels/.rels": _DOCX_RELS,
        "word/document.xml": document,
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        ),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
            archive.writestr(info, content)
    return buffer.getvalue()


FIXTURES: dict[str, dict] = {
    "rated_current_review.xlsx": {
        "format": "xlsx",
        "schema_id": "fixture-rated-current-schema",
        "schema_version": "1",
        "source_set_snapshot": "fixture-snapshot-v1",
        "expected_artifact_stage": "review_candidate",
        "cells": {
            "A1": "Field", "B1": "Value",
            "A2": "rated_current", "B2": "",
            "A3": "insulation_class", "B3": "",
            "A4": "test_voltage", "B4": "",
        },
        "fields": ["rated_current", "insulation_class", "test_voltage"],
    },
    "interface_review.xlsx": {
        "format": "xlsx",
        "schema_id": "fixture-interface-schema",
        "schema_version": "1",
        "source_set_snapshot": "fixture-snapshot-v1",
        "expected_artifact_stage": "review_candidate",
        "cells": {
            "A1": "Field", "B1": "Value",
            "A2": "supported_modes", "B2": "",
            "A3": "baud_rate", "B3": "",
            "A4": "flow_control", "B4": "",
        },
        "fields": ["supported_modes", "baud_rate", "flow_control"],
    },
    "power_table_review.xlsx": {
        "format": "xlsx",
        "schema_id": "fixture-power-table-schema",
        "schema_version": "1",
        "source_set_snapshot": "fixture-snapshot-v1",
        "expected_artifact_stage": "review_candidate",
        "cells": {
            "A1": "Field", "B1": "Value",
            "A2": "voltage_range", "B2": "",
            "A3": "efficiency_percent", "B3": "",
            "A4": "power_table", "B4": "",
        },
        "fields": ["voltage_range", "efficiency_percent", "power_table"],
    },
    "safety_review.xlsx": {
        "format": "xlsx",
        "schema_id": "fixture-safety-schema",
        "schema_version": "1",
        "source_set_snapshot": "fixture-snapshot-v1",
        "expected_artifact_stage": "review_candidate",
        "cells": {
            "A1": "Field", "B1": "Value",
            "A2": "safety_standard", "B2": "",
            "A3": "ip_rating", "B3": "",
            "A4": "creepage_distance", "B4": "",
        },
        "fields": ["safety_standard", "ip_rating", "creepage_distance"],
    },
    "conflict_review.xlsx": {
        "format": "xlsx",
        "schema_id": "fixture-conflict-schema",
        "schema_version": "1",
        "source_set_snapshot": "fixture-snapshot-v1",
        "expected_artifact_stage": "review_candidate",
        "cells": {
            "A1": "Field", "B1": "Value",
            "A2": "can_baud_rate", "B2": "",
            "A3": "termination_resistor", "B3": "",
        },
        "fields": ["can_baud_rate", "termination_resistor"],
    },
    "scope_violation_review.xlsx": {
        "format": "xlsx",
        "schema_id": "fixture-scope-schema",
        "schema_version": "1",
        "source_set_snapshot": "fixture-snapshot-v1",
        "expected_artifact_stage": "review_candidate",
        "cells": {
            "A1": "Field", "B1": "Value",
            "A2": "restricted_field", "B2": "",
        },
        "fields": ["restricted_field"],
    },
    "controller_review.docx": {
        "format": "docx",
        "schema_id": "fixture-controller-schema",
        "schema_version": "1",
        "source_set_snapshot": "fixture-snapshot-v1",
        "expected_artifact_stage": "review_candidate",
        "lines": [
            "Controller Review",
            "controller_model: ",
            "firmware_version: ",
            "release_date: ",
        ],
        "fields": ["controller_model", "firmware_version", "release_date"],
    },
    "thermal_review.docx": {
        "format": "docx",
        "schema_id": "fixture-thermal-schema",
        "schema_version": "1",
        "source_set_snapshot": "fixture-snapshot-v1",
        "expected_artifact_stage": "review_candidate",
        "lines": [
            "Thermal Review",
            "operating_temp: ",
            "thermal_resistance: ",
            "cooling_method: ",
        ],
        "fields": ["operating_temp", "thermal_resistance", "cooling_method"],
    },
}


def main() -> None:
    index: dict[str, dict] = {}
    for name, spec in FIXTURES.items():
        content = build_xlsx(spec["cells"]) if spec["format"] == "xlsx" else build_docx(spec["lines"])
        (FIXTURE_DIR / name).write_bytes(content)
        index[name] = {
            "format": spec["format"],
            "sha256": hashlib.sha256(content).hexdigest(),
            "template_schema_id": spec["schema_id"],
            "template_schema_version": spec["schema_version"],
            "source_set_snapshot": spec["source_set_snapshot"],
            "expected_artifact_stage": spec["expected_artifact_stage"],
            "fields": spec["fields"],
        }
    index_path = FIXTURE_DIR / "fixture_index.json"
    index_path.write_text(
        json.dumps({"fixtures": index}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(index)} fixtures + {index_path.name}")


if __name__ == "__main__":
    main()
