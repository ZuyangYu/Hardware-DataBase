"""High-fidelity XLSX cell revisions via targeted OOXML surgery.

Only the worksheet member that owns the edited cell may change; charts,
styles, shared strings and every other zip member must stay byte-identical.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

import src.settings
from src.agents.tools.attachment_tools import make_attachment_edit
from src.agents.tools.runtime import ToolRuntime
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore
from src.attachments.xlsx_revision import (
    XlsxRevisionError,
    replace_cell_text,
    set_cell_value,
)

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _xlsx_bytes(
    *,
    sheet_name: str = "Sheet1",
    rows_xml: str = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" s="1"><v>42</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c></row>'
    ),
    shared: tuple[str, ...] = ("ADAS ICD", "保留内容"),
    merges: str = "",
) -> bytes:
    shared_xml = "".join(
        f"<si><t>{value}</t></si>" for value in shared
    )
    chart_bytes = b"<chart>untouched-chart-bytes</chart>"
    members = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="{_MAIN}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{_MAIN}"><sheetData>{rows_xml}</sheetData>{merges}</worksheet>'
        ),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<sst xmlns="{_MAIN}" count="2" uniqueCount="2">{shared_xml}</sst>'
        ),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<styleSheet xmlns="{_MAIN}">'
            '<fonts count="1"><font/></fonts>'
            '<fills count="1"><fill/></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf/><xf numFmtId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"
        ),
        "docProps/core.xml": "<core>untouched-core</core>",
        "xl/charts/chart1.xml": chart_bytes.decode("utf-8"),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _member_bytes(source: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def _cell_value(source: bytes, *, sheet: str, address: str) -> str:
    from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx

    workbook = parse_xlsx(io.BytesIO(source))
    parsed = next(item for item in workbook.sheets if item.name == sheet)
    for cell in parsed.cells:
        if cell.ref.upper() == address:
            return cell.value
    return ""


def _service(tmp_path, monkeypatch):
    monkeypatch.setattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", True)
    monkeypatch.setattr(
        src.settings, "CHAT_ATTACHMENT_INDEX_DB_PATH", str(tmp_path / "att.db")
    )
    monkeypatch.setattr(
        src.settings, "CHAT_ATTACHMENT_STORAGE_DIR", str(tmp_path / "storage")
    )
    store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
    return AttachmentService(store=store), store


def test_set_cell_rewrites_only_the_target_worksheet_member():
    source = _xlsx_bytes()
    child, audit = set_cell_value(source, sheet="Sheet1", cell="A1", value="ADAS 新标题")

    assert audit["operation"] == "set_cell"
    assert audit["cell"] == "A1"
    assert _cell_value(child, sheet="Sheet1", address="A1") == "ADAS 新标题"
    assert _cell_value(child, sheet="Sheet1", address="B1") == "42"
    assert _cell_value(child, sheet="Sheet1", address="A2") == "保留内容"

    before = _member_bytes(source)
    after = _member_bytes(child)
    assert set(before) == set(after)
    changed = [name for name in before if before[name] != after[name]]
    assert changed == ["xl/worksheets/sheet1.xml"]
    assert after["xl/charts/chart1.xml"] == before["xl/charts/chart1.xml"]
    assert after["xl/sharedStrings.xml"] == before["xl/sharedStrings.xml"]


def test_set_cell_can_create_a_missing_cell():
    source = _xlsx_bytes()
    child, _audit = set_cell_value(source, sheet="Sheet1", cell="C3", value="新增值")

    assert _cell_value(child, sheet="Sheet1", address="C3") == "新增值"
    # Existing cells are untouched.
    assert _cell_value(child, sheet="Sheet1", address="A1") == "ADAS ICD"


def test_replace_cell_text_requires_a_unique_cell():
    source = _xlsx_bytes()
    child, audit = replace_cell_text(
        source, sheet="Sheet1", before="ADAS ICD", after="ADAS Interface Control Document"
    )

    assert audit["cell"] == "A1"
    assert "Interface Control Document" in _cell_value(child, sheet="Sheet1", address="A1")
    assert _cell_value(child, sheet="Sheet1", address="A2") == "保留内容"

    duplicated = _xlsx_bytes(
        rows_xml=(
            '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>0</v></c></row>'
        )
    )
    with pytest.raises(XlsxRevisionError, match="exactly one cell"):
        replace_cell_text(duplicated, sheet="Sheet1", before="ADAS ICD", after="X")
    with pytest.raises(XlsxRevisionError, match="not found"):
        replace_cell_text(source, sheet="Sheet1", before="不存在", after="X")
    with pytest.raises(XlsxRevisionError, match="worksheet not found"):
        replace_cell_text(source, sheet="缺失表", before="ADAS ICD", after="X")


def test_formula_cells_are_rejected_not_overwritten():
    source = _xlsx_bytes(
        rows_xml='<row r="1"><c r="A1"><f>1+1</f><v>2</v></c></row>'
    )
    with pytest.raises(XlsxRevisionError, match="formula"):
        set_cell_value(source, sheet="Sheet1", cell="A1", value="X")


def test_service_creates_an_audited_xlsx_candidate(tmp_path, monkeypatch):
    service, store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="power.xlsx",
        stream=io.BytesIO(_xlsx_bytes()),
    )

    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="set_cell",
        sheet="Sheet1",
        cell="B1",
        after="99",
    )

    assert revision.operation == "set_cell"
    assert revision.details["cell"] == "B1"
    assert revision.details["format"] == "xlsx"
    persisted = store.get_attachment_revision(
        revision.revision_id, session_id=7, user_id=11
    )
    assert persisted is not None and persisted.details["sheet"] == "Sheet1"

    _record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id, session_id=7, user_id=11
    )
    assert _cell_value(child_bytes, sheet="Sheet1", address="B1") == "99"

    # The same request is idempotent, not an error.
    repeated = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="set_cell",
        sheet="Sheet1",
        cell="B1",
        after="99",
    )
    assert repeated.revision_id == revision.revision_id


def _structural_rows() -> str:
    return (
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2" s="1"><v>42</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>0</v></c></row>'
        '<row r="4"><c r="A4"><f>SUM(B2:B3)</f><v>42</v></c></row>'
    )


def _sheet_xml(source: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_insert_rows_shifts_rows_merges_and_formulas():
    from src.attachments.xlsx_revision import insert_rows

    source = _xlsx_bytes(
        rows_xml=_structural_rows(),
        merges='<mergeCells count="1"><mergeCell ref="A5:B6"/></mergeCells>',
    )
    child, audit = insert_rows(source, sheet="Sheet1", after_row=2, count=1)

    assert audit["operation"] == "insert_rows"
    assert audit["updated_merges"] == 1
    assert audit["updated_formulas"] == 1
    sheet_xml = _sheet_xml(child)
    # Row 3 moved to row 4; the SUM range now includes the inserted row.
    assert 'r="A4" t="s"' in sheet_xml
    assert "<f>SUM(B2:B4)</f>" in sheet_xml
    assert 'ref="A6:B7"' in sheet_xml

    from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx

    parsed = parse_xlsx(io.BytesIO(child)).sheets[0]
    assert "A6:B7" in parsed.merged_ranges

    before = _member_bytes(source)
    after = _member_bytes(child)
    assert [name for name in before if before[name] != after[name]] == [
        "xl/worksheets/sheet1.xml"
    ]
    assert after["xl/charts/chart1.xml"] == before["xl/charts/chart1.xml"]


def test_insert_rows_can_write_values():
    from src.attachments.xlsx_revision import insert_rows

    source = _xlsx_bytes(rows_xml=_structural_rows())
    child, audit = insert_rows(
        source, sheet="Sheet1", after_row=2, count=1, values=[["新甲", "新乙"]]
    )

    assert audit["filled_rows"] == 1
    assert _cell_value(child, sheet="Sheet1", address="A3") == "新甲"
    assert _cell_value(child, sheet="Sheet1", address="B3") == "新乙"
    assert _cell_value(child, sheet="Sheet1", address="A4") == "ADAS ICD"


def test_delete_rows_shifts_and_rejects_formula_references():
    from src.attachments.xlsx_revision import delete_rows

    source = _xlsx_bytes(
        rows_xml=_structural_rows(),
        merges='<mergeCells count="1"><mergeCell ref="A5:B6"/></mergeCells>',
    )
    # The SUM(B2:B3) formula references the deleted row 3: reject.
    with pytest.raises(XlsxRevisionError, match="公式引用了被删除的行"):
        delete_rows(source, sheet="Sheet1", row=3, count=1)

    safe = _xlsx_bytes(
        rows_xml=(
            '<row r="1"><c r="A1"><f>SUM(B1:B2)</f><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>0</v></c></row>'
            '<row r="3"><c r="A3" t="s"><v>1</v></c></row>'
        ),
        merges='<mergeCells count="1"><mergeCell ref="A3:B4"/></mergeCells>',
    )
    child, audit = delete_rows(safe, sheet="Sheet1", row=3, count=1)

    assert audit["removed_rows"] == 1
    assert audit["updated_merges"] == 1
    assert "<f>SUM(B1:B2)</f>" in _sheet_xml(child)
    # Rows 3-4 merged; deleting row 3 leaves the surviving row 4 at row 3.
    assert 'ref="A3:B3"' in _sheet_xml(child)
    assert _cell_value(child, sheet="Sheet1", address="A2") == "ADAS ICD"


def test_structural_edits_reject_unsafe_formulas():
    from src.attachments.xlsx_revision import insert_rows

    cross_sheet = _xlsx_bytes(
        rows_xml='<row r="1"><c r="A1"><f>SUM(Data!A1:A2)</f><v>1</v></c></row>'
    )
    with pytest.raises(XlsxRevisionError, match="跨表"):
        insert_rows(cross_sheet, sheet="Sheet1", after_row=0, count=1)

    indirect = _xlsx_bytes(
        rows_xml='<row r="1"><c r="A1"><f>INDIRECT("A2")</f><v>1</v></c></row>'
    )
    with pytest.raises(XlsxRevisionError, match="INDIRECT"):
        insert_rows(indirect, sheet="Sheet1", after_row=0, count=1)


def test_service_row_operations_are_audited(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="power.xlsx",
        stream=io.BytesIO(_xlsx_bytes(rows_xml=_structural_rows())),
    )

    inserted = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        operation="insert_rows",
        sheet="Sheet1",
        after_row=2,
        count=1,
        values=[["插入值"]],
    )
    assert inserted.operation == "insert_rows"
    assert inserted.details["filled_rows"] == 1
    _record, child_bytes = service.read_source_bytes(
        attachment_id=inserted.child_attachment_id, session_id=7, user_id=11
    )
    assert _cell_value(child_bytes, sheet="Sheet1", address="A3") == "插入值"

    # Chained deletion from the candidate keeps the lineage.
    deleted = service.create_attachment_revision(
        parent_attachment_id=inserted.child_attachment_id,
        session_id=7,
        user_id=11,
        operation="delete_rows",
        sheet="Sheet1",
        row=3,
        count=1,
    )
    assert deleted.operation == "delete_rows"
    assert deleted.details["removed_rows"] == 1


def test_tool_exposes_cell_edit_and_details(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="power.xlsx",
        stream=io.BytesIO(_xlsx_bytes()),
    )
    runtime = ToolRuntime(
        kb_name="ADAS",
        ctx=None,
        chat_session_id="7",
        source_scope="attachment_only",
        attachment_refs=service.build_refs([parent]),
        attachment_service=service,
        attachment_user_id=11,
        attachment_session_id=7,
    )

    payload = json.loads(
        make_attachment_edit(runtime)(
            operation="replace_cell_text",
            sheet="Sheet1",
            before="ADAS ICD",
            after="ADAS 接口控制文档",
        )
    )

    assert payload["status"] == "completed"
    assert payload["details"]["cell"] == "A1"
    assert "替换为" in payload["change_summary"]
