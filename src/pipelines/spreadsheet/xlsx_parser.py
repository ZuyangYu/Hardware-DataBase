import os
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


@dataclass
class ParsedCell:
    row_index: int
    col_index: int
    ref: str
    value: str
    # raw_value: 格式化前的序列化原值(如日期序列号 45658)。与 table_semantic_rows
    # 的 raw_values_json(非前向填充的表头值)是不同概念,勿混淆。
    raw_value: str = ""
    # number_format: 已解析的 formatCode(如 "m/d/yyyy");"" 表示 General/无样式。
    number_format: str = ""


@dataclass
class ParsedRow:
    row_index: int
    values: list[str] = field(default_factory=list)


@dataclass
class ParsedSheet:
    name: str
    rows: list[list[str]] = field(default_factory=list)
    row_indices: list[int] = field(default_factory=list)
    cells: list[ParsedCell] = field(default_factory=list)
    merged_ranges: list[str] = field(default_factory=list)


@dataclass
class ParsedWorkbook:
    file_name: str
    sheets: list[ParsedSheet] = field(default_factory=list)
    embedded_object_count: int = 0
    media_object_count: int = 0
    drawing_object_count: int = 0


# --- Number format resolution & value display ---------------------------------
# Excel 内置 numFmtId -> formatCode(ECMA-376 子集)。
_BUILTIN_FORMATS: dict[int, str] = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00",
    9: "0%", 10: "0.00%", 11: "0.00E+00", 12: "# ?/?",
    14: "m/d/yyyy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss",
    22: "m/d/yyyy h:mm",
    37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)",
    5: '"¤"#,##0_);("¤"#,##0)', 6: '"¤"#,##0_);[Red]("¤"#,##0)',
    7: '"¤"#,##0.00_);("¤"#,##0.00)', 8: '"¤"#,##0.00_);[Red]("¤"#,##0.00)',
    44: '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)',
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0", 48: "##0.0E+0", 49: "@",
}

# Excel 日期序列号 epoch(配合 1900 幻影日修正,与 openpyxl 一致):
# 1900-02-29 是 Excel 的虚构日;serial>60 时整体减 1 吸收它。
_EXCEL_EPOCH = datetime(1899, 12, 31)


@dataclass
class _NumberFormatResolver:
    """将 cell 的 style index 解析为 formatCode,并把序列化原值还原为可读显示值。

    None/损坏的 styles.xml -> 空解析器,所有值原样返回(鲁棒降级)。
    """

    cellxfs_numfmt_ids: list[int]
    custom_formats: dict[int, str]

    @classmethod
    def empty(cls) -> "_NumberFormatResolver":
        return cls(cellxfs_numfmt_ids=[], custom_formats={})

    @classmethod
    def from_styles_xml(cls, styles_bytes: bytes | None) -> "_NumberFormatResolver":
        if not styles_bytes:
            return cls.empty()
        try:
            root = ET.fromstring(styles_bytes)
        except ET.ParseError:
            return cls.empty()

        custom_formats: dict[int, str] = {}
        numfmts_node = root.find("main:numFmts", NS)
        if numfmts_node is not None:
            for node in numfmts_node.findall("main:numFmt", NS):
                try:
                    numfmt_id = int(node.attrib.get("numFmtId", ""))
                except ValueError:
                    continue
                code = node.attrib.get("formatCode", "")
                if code:
                    custom_formats[numfmt_id] = code

        cellxfs_numfmt_ids: list[int] = []
        cellxfs_node = root.find("main:cellXfs", NS)
        if cellxfs_node is not None:
            for xf in cellxfs_node.findall("main:xf", NS):
                raw = xf.attrib.get("numFmtId", "0")
                try:
                    cellxfs_numfmt_ids.append(int(raw))
                except ValueError:
                    cellxfs_numfmt_ids.append(0)
        return cls(cellxfs_numfmt_ids=cellxfs_numfmt_ids, custom_formats=custom_formats)

    def format_code_for(self, style_index: int | None) -> str:
        if style_index is None or style_index < 0 or style_index >= len(self.cellxfs_numfmt_ids):
            return ""
        numfmt_id = self.cellxfs_numfmt_ids[style_index]
        return self.custom_formats.get(numfmt_id) or _BUILTIN_FORMATS.get(numfmt_id, "")

    def resolve(self, raw_value: str, style_index: int | None) -> tuple[str, str]:
        """返回 (raw_value, display_value)。无法分类时显示值等于原值。"""
        fmt = self.format_code_for(style_index)
        if not fmt or fmt == "General":
            return raw_value, raw_value
        return raw_value, _format_value(raw_value, fmt)


def _classify_format(format_code: str) -> str:
    """把 formatCode 分类为 date/datetime/time/percentage/currency/text/number/general。

    只取首个 ';' 段(正数段),剥离引号字面量/转义/颜色/条件,但保留 [$...] 货币标记。
    靠 y/d/h/s token 判定日期/时间,避开裸 'm' 的月/分歧义。
    """
    if not format_code:
        return "general"
    section = format_code.split(";", 1)[0]
    section = re.sub(r'"[^"]*"', "", section)        # 去掉引号字面量
    section = re.sub(r"\\.", "", section)              # 去掉转义字符
    # 去掉颜色/条件段(如 [Red]、[<=1000]),但保留 [$...] 货币标记。
    section = re.sub(r"\[(?![$])[^\]]*\]", "", section, flags=re.IGNORECASE)
    if section.strip() == "@":
        return "text"
    if "%" in section:
        return "percentage"
    has_date = bool(re.search(r"yy|mmm+|d{1,4}", section, re.IGNORECASE))
    has_time = bool(re.search(r"[hs]", section, re.IGNORECASE))
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "time"
    if re.search(r"\[\$", section) or re.search(r"[$€£¥￥]", section):
        return "currency"
    if section.strip().lower() == "general" or section.strip() == "":
        return "general"
    return "number"


def _serial_to_datetime_display(serial: float, has_date: bool, has_time: bool) -> str:
    """把 Excel 日期序列号还原成 ISO 风格字符串。

    serial<0(负,1900 前)直接返回原值字符串。应用 1900 幻影日修正。
    """
    if serial < 0 or (not has_date and not has_time):
        return str(serial)
    int_part = int(serial)
    frac = serial - int_part

    date_str = ""
    if has_date:
        if int_part == 60:
            date_str = "1900-02-29"
            int_part = 0
        else:
            adjusted = int_part - 1 if int_part > 60 else int_part
            date_obj = _EXCEL_EPOCH + timedelta(days=adjusted)
            date_str = date_obj.strftime("%Y-%m-%d")

    time_str = ""
    if has_time:
        total_seconds = int(round(frac * 86400))
        if total_seconds >= 86400:
            total_seconds = 86399
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    if has_date and has_time:
        return f"{date_str} {time_str}"
    if has_date:
        return date_str
    return time_str


def _percent_decimals(format_code: str) -> int:
    """数 '%' 所在段 '.' 后的零的个数('0.00%' -> 2,'0%' -> 0)。"""
    section = format_code.split(";", 1)[0]
    match = re.search(r"\.([0#?]+)%", section)
    return len(match.group(1)) if match else 0


def _extract_currency_symbol(format_code: str) -> str:
    """从 [$xxx-yyy] 提取货币符号(如 '[$$-409]' -> '$')。无法识别返回 ''."""
    section = format_code.split(";", 1)[0]
    match = re.search(r"\[\$([^\]\-]+)", section)
    if not match:
        return ""
    symbol = match.group(1)
    if not symbol or symbol == "-":
        return ""
    return symbol


def _format_value(raw: str, format_code: str) -> str:
    """按 formatCode 把序列化原值渲染成可读显示值。

    float(raw) 失败(本就是字符串)直接回退 raw。只渲染 date/time/percentage/currency,
    其余(科学计数/分数/纯数字)回退原值。
    """
    try:
        numeric = float(raw)
    except ValueError:
        return raw

    kind = _classify_format(format_code)
    if kind in ("date", "datetime", "time"):
        return _serial_to_datetime_display(numeric, has_date=kind in ("date", "datetime"), has_time=kind in ("datetime", "time"))
    if kind == "percentage":
        decimals = _percent_decimals(format_code)
        return f"{numeric * 100:.{decimals}f}%"
    if kind == "currency":
        symbol = _extract_currency_symbol(format_code)
        if symbol:
            return f"{symbol}{numeric}"
        return raw  # 货币符号不可识别时保留原值
    # number / general / text -> 原值
    return raw


def parse_xlsx(file_path: str) -> ParsedWorkbook:
    if not zipfile.is_zipfile(file_path):
        raise ValueError("Not a valid .xlsx package")

    with zipfile.ZipFile(file_path) as archive:
        names = archive.namelist()
        shared_strings = _read_shared_strings(archive)
        sheet_refs = _read_sheet_refs(archive)
        styles_bytes = archive.read("xl/styles.xml") if "xl/styles.xml" in names else None
        resolver = _NumberFormatResolver.from_styles_xml(styles_bytes)
        sheets = []
        for sheet_name, sheet_path in sheet_refs:
            if sheet_path not in names:
                continue
            rows, row_indices, cells, merged_ranges = _read_sheet_rows(archive, sheet_path, shared_strings, resolver)
            sheets.append(
                ParsedSheet(
                    name=sheet_name,
                    rows=rows,
                    row_indices=row_indices,
                    cells=cells,
                    merged_ranges=merged_ranges,
                )
            )
    return ParsedWorkbook(
        file_name=os.path.basename(file_path),
        sheets=sheets,
        embedded_object_count=sum(name.startswith("xl/embeddings/") for name in names),
        media_object_count=sum(name.startswith("xl/media/") for name in names),
        drawing_object_count=sum(name.startswith("xl/drawings/") for name in names),
    )


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("main:si", NS):
        texts = [node.text or "" for node in item.findall(".//main:t", NS)]
        values.append("".join(texts))
    return values


def _read_sheet_refs(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
    rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        rel.attrib.get("Id"): rel.attrib.get("Target", "")
        for rel in rels_root.findall("pkgrel:Relationship", NS)
    }
    refs = []
    for sheet in workbook_root.findall(".//main:sheet", NS):
        name = sheet.attrib.get("name", "Sheet")
        rel_id = sheet.attrib.get(f"{{{NS['rel']}}}id")
        target = rel_targets.get(rel_id, "")
        if not target:
            continue
        refs.append((name, _normalize_sheet_path(target)))
    return refs


def _normalize_sheet_path(target: str) -> str:
    target = target.lstrip("/")
    if target.startswith("xl/"):
        return target
    return f"xl/{target}"


def _read_sheet_rows(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
    resolver: _NumberFormatResolver,
) -> tuple[list[list[str]], list[int], list[ParsedCell], list[str]]:
    root = ET.fromstring(archive.read(sheet_path))
    merged_ranges = [
        node.attrib.get("ref", "")
        for node in root.findall(".//main:mergeCell", NS)
        if node.attrib.get("ref")
    ]

    # Pass 1: 收集稀疏 cell(保留真实 row_index),value 用可读显示值。
    # 结构: row_index -> {col_index(0-based): (raw, display, number_format)}
    row_by_index: dict[int, dict[int, tuple[str, str, str]]] = {}
    row_order: list[int] = []
    for row in root.findall(".//main:sheetData/main:row", NS):
        row_index = _row_index(row.attrib.get("r", "")) or len(row_order) + 1
        row_cells: dict[int, tuple[str, str, str]] = {}
        for cell in row.findall("main:c", NS):
            ref = cell.attrib.get("r", "")
            col_index = _column_index(ref)
            if col_index is None:
                continue
            raw_value, display_value, number_format = _cell_value(cell, shared_strings, resolver)
            row_cells[col_index] = (raw_value, display_value, number_format)
        row_by_index[row_index] = row_cells
        row_order.append(row_index)

    # Pass 2(合并扩展,修复核心): 对每个合并区,把左上格的值结构化填充到
    # XML 中缺失的格子(不覆盖 authored cell)。被 sheetData 省略的空行跳过。
    for merged_ref in merged_ranges:
        top_left, bottom_right = _parse_merge_range(merged_ref)
        if top_left is None or bottom_right is None:
            continue
        top_row, top_col = top_left
        bottom_row, bottom_col = bottom_right
        top_left_cells = row_by_index.get(top_row)
        if not top_left_cells or top_col not in top_left_cells:
            continue
        fill_value = top_left_cells[top_col]
        for row_index in range(top_row, bottom_row + 1):
            target_cells = row_by_index.get(row_index)
            if target_cells is None:
                continue
            for col_index in range(top_col, bottom_col + 1):
                if col_index not in target_cells:
                    target_cells[col_index] = fill_value

    # Pass 3: 物化网格(parsed_rows 用 display 值)与 parsed_cells。
    max_col_seen = -1
    for cells in row_by_index.values():
        if cells:
            max_col_seen = max(max_col_seen, max(cells))
    parsed_rows: list[list[str]] = []
    parsed_row_indices: list[int] = []
    parsed_cells: list[ParsedCell] = []
    for row_index in row_order:
        cells = row_by_index[row_index]
        parsed_row_indices.append(row_index)
        row_max = max(cells) if cells else -1
        row_max = max(row_max, -1)
        if not cells:
            parsed_rows.append([])
            continue
        parsed_rows.append([cells.get(index, ("", "", ""))[1] for index in range(row_max + 1)])
        for col_index, (raw_value, display_value, number_format) in cells.items():
            if display_value:
                parsed_cells.append(
                    ParsedCell(
                        row_index=row_index,
                        col_index=col_index + 1,
                        ref=f"{_column_letter(col_index)}{row_index}",
                        value=display_value,
                        raw_value=raw_value,
                        number_format=number_format,
                    )
                )
    return parsed_rows, parsed_row_indices, parsed_cells, merged_ranges


def _parse_merge_range(ref: str) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """解析 'A2:A4' -> ((2, 0), (4, 0))(行, 列0-based)。失败返回 (None, None)。"""
    parts = ref.split(":")
    if len(parts) != 2:
        return None, None
    top_left = _row_col_from_ref(parts[0])
    bottom_right = _row_col_from_ref(parts[1])
    return top_left, bottom_right


def _row_col_from_ref(ref: str) -> tuple[int, int] | None:
    match = re.match(r"([A-Z]+)(\d+)", ref.upper().strip())
    if not match:
        return None
    col_letters, row_str = match.group(1), match.group(2)
    col = 0
    for char in col_letters:
        col = col * 26 + (ord(char) - ord("A") + 1)
    return int(row_str), col - 1


def _column_letter(col_index_0based: int) -> str:
    """0-based 列索引 -> 列字母(0 -> 'A', 26 -> 'AA')。"""
    if col_index_0based < 0:
        return ""
    letters = ""
    index = col_index_0based
    while True:
        index, remainder = divmod(index, 26)
        letters = chr(ord("A") + remainder) + letters
        if index == 0:
            break
        index -= 1
    return letters


def _row_index(cell_ref: str) -> int | None:
    match = re.search(r"(\d+)", cell_ref)
    if not match:
        return None
    return int(match.group(1))


def _column_index(cell_ref: str) -> int | None:
    match = re.match(r"([A-Z]+)", cell_ref.upper())
    if not match:
        return None
    value = 0
    for char in match.group(1):
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def _cell_value(
    cell: ET.Element,
    shared_strings: list[str],
    resolver: _NumberFormatResolver,
) -> tuple[str, str, str]:
    """返回 (raw_value, display_value, number_format)。

    shared string / inlineStr / boolean / formula string / error 各 passthrough;
    数字路径(t in ('', 'n')) 取 cell 的 's'(style index) 经 resolver 还原显示值。
    """
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        texts = [node.text or "" for node in cell.findall(".//main:t", NS)]
        value = "".join(texts).strip()
        return value, value, ""

    value_node = cell.find("main:v", NS)
    if value_node is None or value_node.text is None:
        return "", "", ""
    raw = value_node.text.strip()

    if cell_type == "s":
        try:
            value = shared_strings[int(raw)].strip()
        except (ValueError, IndexError):
            value = raw
        return value, value, ""
    if cell_type == "b":
        value = "TRUE" if raw == "1" else "FALSE"
        return value, value, ""
    if cell_type in ("str", "e"):
        return raw, raw, ""

    # 数字路径:应用数字/日期格式还原。
    style_attr = cell.attrib.get("s")
    style_index = int(style_attr) if style_attr and style_attr.isdigit() else None
    number_format = resolver.format_code_for(style_index)
    _, display_value = resolver.resolve(raw, style_index)
    return raw, display_value, number_format
