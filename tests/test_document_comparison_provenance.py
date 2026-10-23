"""Comparison provenance is frozen and immutable."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document

from src.services.document_comparison import (
    ComparisonError,
    canonicalize_archive_record,
    canonicalize_parts,
    compare_documents,
)
from src.storage.document_comparison_store import DocumentComparisonStore


def _record(path: Path, content_hash: str):
    return SimpleNamespace(
        id=9, kb_id=5, document_id="remote-9", document_name=path.name,
        content_hash=content_hash, revision="r3", source_version_id="sv-3",
        processor_kind="ragflow", processor_version="docx-parser-7",
        department_id="47",
    )


def _docx(path: Path) -> None:
    document = Document()
    document.add_heading("1 概述", level=1)
    document.add_paragraph("系统概述。")
    document.add_heading("2 管脚定义", level=1)
    document.add_paragraph("管脚 2 电压为 3.3V。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Pin"
    table.cell(0, 1).text = "Voltage"
    table.cell(1, 0).text = "2"
    table.cell(1, 1).text = "3.3"
    document.save(path)


def test_archive_snapshot_keeps_version_hash_parser_and_locator(tmp_path):
    path = tmp_path / "HSI-v3.docx"
    _docx(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    document = canonicalize_archive_record(
        _record(path, digest), str(path),
        owner={"user_id": 7}, permissions={"source_scope": "attachment_and_knowledge_base"},
    )
    assert document.snapshot.source_id == "kb:5:record:9"
    assert document.snapshot.document_version == "r3"
    assert document.snapshot.content_hash == digest
    assert document.snapshot.parser_version
    assert document.snapshot.owner["user_id"] == 7
    assert document.sections
    assert document.sections[0].locator["heading_path"]
    assert any(section.tables for section in document.sections)


def test_changed_archive_hash_is_rejected_not_compared(tmp_path):
    path = tmp_path / "HSI-v3.docx"
    _docx(path)
    with pytest.raises(ComparisonError) as error:
        canonicalize_archive_record(_record(path, "0" * 64), str(path))
    assert error.value.error_code == "source_changed"


def test_report_store_is_immutable_and_tamper_evident(tmp_path):
    left = canonicalize_parts(
        [SimpleNamespace(
            part_id="p1", ordinal=0, part_type="text", text_content="A",
            locator={"paragraph": 0}, metadata={}, parser_version="v1",
        )],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )
    right = canonicalize_parts(
        [SimpleNamespace(
            part_id="p2", ordinal=0, part_type="text", text_content="B",
            locator={"paragraph": 0}, metadata={}, parser_version="v1",
        )],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="b" * 64,
    )
    report = compare_documents(left, right)
    store = DocumentComparisonStore(str(tmp_path / "comparisons"))
    stored = store.save(report, owner_user_id=7)
    assert stored["immutable_hash"] == report.immutable_hash
    assert store.get(report.report_id, owner_user_id=7)["report_id"] == report.report_id
    with pytest.raises(ComparisonError) as denied:
        store.get(report.report_id, owner_user_id=8)
    assert denied.value.error_code == "comparison_report_denied"

    import json

    path = store.export_path(report.report_id)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["report"]["items"][0]["right_text"] = "tampered"
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ComparisonError) as tampered:
        store.get(report.report_id, owner_user_id=7)
    assert tampered.value.error_code == "comparison_report_tampered"


def test_rerun_with_a_new_created_at_is_idempotent(tmp_path):
    left = canonicalize_parts(
        [SimpleNamespace(
            part_id="p1", ordinal=0, part_type="text", text_content="A",
            locator={"paragraph": 0}, metadata={}, parser_version="v1",
        )],
        source_type="attachment", source_id="attachment:att-1", source_name="a",
        source_content_hash="a" * 64,
    )
    right = canonicalize_parts(
        [SimpleNamespace(
            part_id="p2", ordinal=0, part_type="text", text_content="B",
            locator={"paragraph": 0}, metadata={}, parser_version="v1",
        )],
        source_type="knowledge_base", source_id="kb:5:record:9", source_name="b",
        source_content_hash="b" * 64,
    )
    first = compare_documents(left, right, created_at="2026-10-22T00:00:00+00:00")
    rerun = compare_documents(left, right, created_at="2026-10-22T01:00:00+00:00")
    store = DocumentComparisonStore(str(tmp_path / "comparisons"))
    store.save(first, owner_user_id=7)
    saved = store.save(rerun, owner_user_id=7)
    assert saved["report_id"] == first.report_id
    assert saved["immutable_hash"] == first.immutable_hash
