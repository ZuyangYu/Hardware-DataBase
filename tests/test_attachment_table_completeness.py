"""Regression tests for bounded, complete DOCX table attachment parts."""

from __future__ import annotations

import json

from docx import Document

import src.settings
from src.attachments.local_document_parser import LocalDocumentParser
from src.attachments.models import (
    AttachmentAsset,
    PARSE_STATUS_DEGRADED,
    PARSE_STATUS_READY,
    PART_TYPE_TABLE,
)


def _asset() -> AttachmentAsset:
    return AttachmentAsset(
        asset_id="asset-docx-table",
        session_id=1,
        user_id=1,
        tenant_id="tenant",
        sha256="a" * 64,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        extension=".docx",
        size_bytes=1,
        storage_key="unused",
        parse_status="running",
    )


def _save_table(path, rows: list[list[str]]) -> None:
    document = Document()
    table = document.add_table(rows=0, cols=len(rows[0]))
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values):
            cell.text = value
    document.save(path)


def _table_parts(outcome):
    return [part for part in outcome.parts if part.part_type == PART_TYPE_TABLE]


def test_docx_table_parts_are_valid_json_and_keep_last_row_with_source_metadata(tmp_path):
    rows = [["Part", "Description"]]
    rows.extend([[f"P-{index}", f"component description {index}"] for index in range(400)])
    rows[-1][-1] = "LAST_ROW_SENTINEL"
    source = tmp_path / "long-table.docx"
    _save_table(source, rows)

    outcome = LocalDocumentParser().parse(_asset(), str(source))

    assert outcome.parse_status == PARSE_STATUS_READY
    parts = _table_parts(outcome)
    assert len(parts) > 1
    decoded = [json.loads(part.text_content) for part in parts]
    assert all(isinstance(payload, list) for payload in decoded)
    assert any("LAST_ROW_SENTINEL" in json.dumps(payload) for payload in decoded)
    assert {part.locator["table"] for part in parts} == {0}
    assert {part.metadata["table_index"] for part in parts} == {0}
    assert all(part.metadata["row_start"] <= part.metadata["row_end"] for part in parts)
    assert all(part.metadata["header_context"] == rows[0] for part in parts)


def test_docx_table_row_and_cell_caps_degrade_instead_of_silently_dropping_data(
    tmp_path, monkeypatch
):
    source = tmp_path / "capped-table.docx"
    _save_table(
        source,
        [
            ["Part", "Description", "Value"],
            ["P-1", "first", "1"],
            ["P-2", "second", "2"],
            ["P-3", "third", "3"],
        ],
    )
    monkeypatch.setattr(src.settings, "CHAT_ATTACHMENT_MAX_ROWS", 2)
    monkeypatch.setattr(src.settings, "CHAT_ATTACHMENT_MAX_CELLS", 4)

    outcome = LocalDocumentParser().parse(_asset(), str(source))

    assert outcome.parse_status == PARSE_STATUS_DEGRADED
    assert outcome.degraded_reasons
    assert any("row" in reason.lower() or "cell" in reason.lower() for reason in outcome.degraded_reasons)
    parts = _table_parts(outcome)
    assert parts
    assert all(isinstance(json.loads(part.text_content), list) for part in parts)
    assert not any("P-3" in part.text_content for part in parts)


def test_docx_table_long_cell_is_split_without_losing_text(tmp_path):
    source = tmp_path / "long-cell.docx"
    long_value = "CELL_START_" + ("long cell data " * 1000) + "_CELL_END"
    _save_table(source, [["Column"], [long_value]])

    outcome = LocalDocumentParser().parse(_asset(), str(source))

    parts = _table_parts(outcome)
    assert len(parts) > 1
    values = []
    for part in parts:
        payload = json.loads(part.text_content)
        values.extend(cell for row in payload for cell in row)
    combined = "".join(value for value in values)
    assert "CELL_START_" in combined
    assert "_CELL_END" in combined
    assert len(combined) >= len(long_value)
