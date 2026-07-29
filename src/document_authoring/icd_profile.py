"""Data-driven classification of formal ICD workbook templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Literal
import zipfile

from src.document_authoring.icd_generation import (
    connector_refdes_from_front_view_template,
    is_example_sheet_name,
)
from src.pipelines.spreadsheet.xlsx_parser import ParsedSheet, parse_xlsx


_PIN_LABELS = ("pin number", "管脚号", "引脚号", "针脚号")
_DEFINITION_LABELS = ("pin definition", "管脚定义", "引脚定义", "signal definition")
_LOCATION_LABELS = ("location number", "控制器上编号", "位置编号", "接插件位置")
_BOARD_MODEL_LABELS = ("board connector", "pcb connector", "板端接插件", "板端型号")


@dataclass(frozen=True)
class IcdConnectorBlock:
    """Template-owned identity and pin-table contract for one connector."""

    sheet_name: str
    location_number: str
    location_cell: str
    board_connector_model: str
    board_connector_model_cell: str
    pin_header_row: int


@dataclass(frozen=True)
class IcdTemplateProfile:
    kind: Literal["generic", "icd", "icd_sample"]
    reasons: list[str]
    connector_blocks: list[IcdConnectorBlock]
    issues: list[dict[str, str]]
    front_view_refdes: list[str] = field(default_factory=list)

    @property
    def connector_refdes(self) -> list[str]:
        """Return template-declared connector identities without project inference."""

        return list(dict.fromkeys([
            *(block.location_number for block in self.connector_blocks),
            *self.front_view_refdes,
        ]))


def classify_icd_template(content: bytes, target_format: str) -> IcdTemplateProfile:
    """Classify only from template labels and geometry; never from project data."""

    if str(target_format).casefold() not in {"xlsx", "xlsm"}:
        return _generic_profile("unsupported_template_format")
    if not isinstance(content, bytes) or not content:
        return _generic_profile("empty_template")
    try:
        workbook = parse_xlsx(BytesIO(content))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return _generic_profile("unreadable_workbook")

    formal_sheets = [
        sheet for sheet in workbook.sheets if not is_example_sheet_name(sheet.name)
    ]
    connector_blocks = [
        block
        for sheet in formal_sheets
        for header_row in _pin_table_header_rows(sheet)
        for block in [_connector_block(sheet, header_row)]
        if block is not None
    ]
    has_icd_labels = any(_has_pin_table(sheet) for sheet in workbook.sheets)
    front_view_refdes = connector_refdes_from_front_view_template(content)
    if connector_blocks:
        return IcdTemplateProfile(
            kind="icd",
            reasons=["formal_connector_block_detected"],
            connector_blocks=connector_blocks,
            issues=[],
            front_view_refdes=front_view_refdes,
        )
    if has_icd_labels:
        return IcdTemplateProfile(
            kind="icd_sample",
            reasons=["icd_labels_without_formal_connector_block"],
            connector_blocks=[],
            issues=[{
                "code": "formal_connector_block_missing",
                "severity": "blocking",
                "message": "正式 ICD 模板需要非示例页中的连接器编号、板端型号和管脚定义表。",
            }],
            front_view_refdes=front_view_refdes,
        )
    return _generic_profile("icd_labels_not_detected", front_view_refdes)


def _generic_profile(
    reason: str,
    front_view_refdes: list[str] | None = None,
) -> IcdTemplateProfile:
    return IcdTemplateProfile(
        kind="generic",
        reasons=[reason],
        connector_blocks=[],
        issues=[],
        front_view_refdes=front_view_refdes or [],
    )


def _connector_block(sheet: ParsedSheet, header_row: int) -> IcdConnectorBlock | None:
    location = _nearest_labeled_value(sheet, header_row, _LOCATION_LABELS)
    board_model = _nearest_labeled_value(sheet, header_row, _BOARD_MODEL_LABELS)
    if location is None or board_model is None:
        return None
    return IcdConnectorBlock(
        sheet_name=sheet.name,
        location_number=location[0],
        location_cell=location[1],
        board_connector_model=board_model[0],
        board_connector_model_cell=board_model[1],
        pin_header_row=header_row + 1,
    )


def _has_pin_table(sheet: ParsedSheet) -> bool:
    return bool(_pin_table_header_rows(sheet))


def _pin_table_header_rows(sheet: ParsedSheet) -> list[int]:
    return [
        row_index
        for row_index, row in enumerate(sheet.rows)
        if _row_has_labels(row, _PIN_LABELS, _DEFINITION_LABELS)
    ]


def _row_has_labels(row: list[str], *label_groups: tuple[str, ...]) -> bool:
    labels = [_normalize(value) for value in row if str(value).strip()]
    return all(any(_contains(label, group) for label in labels) for group in label_groups)


def _nearest_labeled_value(
    sheet: ParsedSheet,
    header_row: int,
    labels: tuple[str, ...],
) -> tuple[str, str] | None:
    candidates: list[tuple[int, int, str, str]] = []
    for row_index, row in enumerate(sheet.rows):
        for column_index, raw_label in enumerate(row):
            if not _contains(_normalize(raw_label), labels):
                continue
            value = _next_value(row, column_index)
            if value is None:
                continue
            text, value_index = value
            candidates.append((
                abs(row_index - header_row),
                column_index,
                text,
                _cell_ref(value_index + 1, row_index + 1),
            ))
    if not candidates:
        return None
    _distance, _column, value, cell = min(candidates)
    return value, cell


def _next_value(row: list[str], label_index: int) -> tuple[str, int] | None:
    for value_index, value in enumerate(row[label_index + 1:], start=label_index + 1):
        text = str(value).strip()
        if text:
            return text, value_index
    return None


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _contains(value: str, candidates: tuple[str, ...]) -> bool:
    return any(candidate in value for candidate in candidates)


def _cell_ref(column_number: int, row_number: int) -> str:
    result = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        result = chr(65 + remainder) + result
    return f"{result}{row_number}"
