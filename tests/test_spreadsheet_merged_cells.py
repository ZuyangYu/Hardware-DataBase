import os
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from src.pipelines.spreadsheet.xlsx_parser import ParsedSheet, parse_xlsx
from src.pipelines.spreadsheet.table_store import (
    TableIndexStore,
    _merged_fill_set,
    _sheet_semantic_rows,
)


def _sheet(name: str, rows: list[list[str]], merged_ranges: list[str], row_indices=None) -> ParsedSheet:
    """构建一个已扩展网格的 ParsedSheet(模拟 parser 合并扩展后的输出)。"""
    if row_indices is None:
        row_indices = list(range(1, len(rows) + 1))
    return ParsedSheet(name=name, rows=rows, row_indices=row_indices, merged_ranges=merged_ranges)


class MergedFillSetTests(unittest.TestCase):
    def test_excludes_top_left(self):
        fill = _merged_fill_set(["A2:A4"])
        # A2 是左上(authorished),不应在集合内;A3/A4 应在。
        self.assertNotIn((2, 1), fill)
        self.assertIn((3, 1), fill)
        self.assertIn((4, 1), fill)

    def test_multi_column_range(self):
        fill = _merged_fill_set(["B2:C4"])
        self.assertNotIn((2, 2), fill)  # 左上 B2
        self.assertIn((2, 3), fill)     # C2
        self.assertIn((4, 2), fill)     # B4
        self.assertIn((4, 3), fill)     # C4

    def test_malformed_range_skipped(self):
        self.assertEqual(_merged_fill_set(["badrange", ""]), set())


class SemanticRowsMergedTests(unittest.TestCase):
    def _category_sheet(self) -> ParsedSheet:
        # 表头(行1): Category | Part | Voltage
        # 合并 A2:A4: Category 在行2,3,4 上结构化填充(parser 已展开网格)。
        # 行5: 真空白行(分节),行6: 另一类别,行7 同节但 Voltage 空(应继承行6的 authored 值)。
        rows = [
            ["Category", "Part", "Voltage"],   # 1 header
            ["Power", "A1", "12"],              # 2 (merged top-left)
            ["Power", "A2", "5"],               # 3 (structural fill)
            ["Power", "A3", "3"],               # 4 (structural fill)
            [],                                  # 5 blank
            ["Logic", "B1", "3"],               # 6
            ["Logic", "B2", ""],                # 7 (Voltage genuinely blank -> forward-fill)
        ]
        return _sheet("Sheet1", rows, merged_ranges=["A2:A4"])

    def test_merged_rows_carry_category_as_fact_not_inherited(self):
        sheet = self._category_sheet()
        rows = _sheet_semantic_rows(sheet)

        # 行 2,3,4 三行语义行。
        merged_rows = [r for r in rows if r["row_index"] in (2, 3, 4)]
        self.assertEqual(len(merged_rows), 3)
        for r in merged_rows:
            self.assertEqual(r["values"]["Category"], "Power")
            # Category 不应在 inherited 里(它是结构化事实,不是前向继承)。
            self.assertNotIn("Category", r["inherited"])

        # 行 3,4 是结构化填充(非左上),source.structural_fill 应列出 Category。
        for r in [x for x in merged_rows if x["row_index"] in (3, 4)]:
            self.assertIn("Category", r["source"]["structural_fill"])
            self.assertIn("merged_cell_structural_fill", r["confidence_reasons"])

    def test_merged_category_does_not_leak_past_blank_row(self):
        sheet = self._category_sheet()
        rows = _sheet_semantic_rows(sheet)

        # 行 7 在行 6(Logic)之后的同一节,Voltage 空白应继承行 6 的 authored "3",
        # 但 Category 必须是行 6 的 "Logic"(authored),而不是泄漏的 "Power"。
        row7 = next(r for r in rows if r["row_index"] == 7)
        self.assertEqual(row7["values"]["Category"], "Logic")
        self.assertEqual(row7["values"]["Voltage"], "3")  # forward-fill from row 6
        self.assertIn("Voltage", row7["inherited"])
        # Category 在行 6 是 authored,context_structural 已在行 5 空行处清空,所以行 7 的
        # Category 来自行 6 的 authored 上下文,而非合并泄漏。
        self.assertNotIn("Category", row7["inherited"])


class ParserMergeExpansionTests(unittest.TestCase):
    """端到端:真实 .xlsx 的合并扩展 + 日期样式还原。"""

    def _write_minimal_xlsx(self, path: str):
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        def _el(tag, **attrs):
            e = ET.Element(f"{{{ns}}}{tag}")
            for k, v in attrs.items():
                e.set(k, v)
            return e

        # workbook.xml
        wb = ET.Element(f"{{{ns}}}workbook")
        sheets_el = ET.SubElement(wb, f"{{{ns}}}sheets")
        s = ET.SubElement(sheets_el, f"{{{ns}}}sheet", name="Sheet1", sheetId="1")
        s.set("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "rId1")
        buf = ET.tostring(wb, xml_declaration=True, encoding="UTF-8")

        # sharedStrings
        ss = ET.Element(f"{{{ns}}}sst")
        for value in ["Category", "Part"]:
            si = ET.SubElement(ss, f"{{{ns}}}si")
            ET.SubElement(si, f"{{{ns}}}t").text = value
        ss_buf = ET.tostring(ss, xml_declaration=True, encoding="UTF-8")

        # styles: cellXfs index 0 -> numFmtId 14 (date)
        styles = ET.Element(f"{{{ns}}}styleSheet")
        xfs = ET.SubElement(styles, f"{{{ns}}}cellXfs", count="1")
        ET.SubElement(xfs, f"{{{ns}}}xf", numFmtId="14", xfId="0")
        styles_buf = ET.tostring(styles, xml_declaration=True, encoding="UTF-8")

        # sheet: A2:A4 merged, A2="Category"(string), B2="1234.5"(date serial via style s="0")
        sheet = ET.Element(f"{{{ns}}}worksheet")
        sd = ET.SubElement(sheet, f"{{{ns}}}sheetData")
        r1 = ET.SubElement(sd, f"{{{ns}}}row", r="1")
        c = ET.SubElement(r1, f"{{{ns}}}c", r="A1", t="s"); ET.SubElement(c, f"{{{ns}}}v").text = "0"
        c = ET.SubElement(r1, f"{{{ns}}}c", r="B1", t="s"); ET.SubElement(c, f"{{{ns}}}v").text = "1"
        r2 = ET.SubElement(sd, f"{{{ns}}}row", r="2")
        c = ET.SubElement(r2, f"{{{ns}}}c", r="A2", t="s"); ET.SubElement(c, f"{{{ns}}}v").text = "0"
        c = ET.SubElement(r2, f"{{{ns}}}c", r="B2", s="0"); ET.SubElement(c, f"{{{ns}}}v").text = "45656"
        r3 = ET.SubElement(sd, f"{{{ns}}}row", r="3")
        c = ET.SubElement(r3, f"{{{ns}}}c", r="B3", s="0"); ET.SubElement(c, f"{{{ns}}}v").text = "45657"
        r4 = ET.SubElement(sd, f"{{{ns}}}row", r="4")
        c = ET.SubElement(r4, f"{{{ns}}}c", r="B4", s="0"); ET.SubElement(c, f"{{{ns}}}v").text = "45658"
        merges = ET.SubElement(sheet, f"{{{ns}}}mergeCells", count="1")
        ET.SubElement(merges, f"{{{ns}}}mergeCell", ref="A2:A4")
        sheet_buf = ET.tostring(sheet, xml_declaration=True, encoding="UTF-8")

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/workbook.xml", buf)
            z.writestr("xl/_rels/workbook.xml.rels",
                       '<?xml version="1.0"?>'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                       '</Relationships>')
            z.writestr("xl/sharedStrings.xml", ss_buf)
            z.writestr("xl/styles.xml", styles_buf)
            z.writestr("xl/worksheets/sheet1.xml", sheet_buf)
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0"?>'
                       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                       '<Default Extension="xml" ContentType="application/xml"/>'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                       '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                       '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                       '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                       '</Types>')

    def test_parse_xlsx_expands_merge_and_restores_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "merged.xlsx")
            self._write_minimal_xlsx(path)
            workbook = parse_xlsx(path)

        self.assertEqual(len(workbook.sheets), 1)
        sheet = workbook.sheets[0]
        self.assertEqual(sheet.merged_ranges, ["A2:A4"])

        # A3/A4 原本 XML 中没有,应被结构化扩展为 "Category"。
        # rows[0]=header, rows[1]=row2, rows[2]=row3, rows[3]=row4
        self.assertEqual(sheet.rows[2][0], "Category")  # A3
        self.assertEqual(sheet.rows[3][0], "Category")  # A4

        # B 列日期单元格(value 是 ISO 日期, raw_value 是序列号, number_format 是 m/d/yyyy)。
        # 排除表头 B1(字符串 "Part",无 number_format)。
        date_cells = [c for c in sheet.cells if c.col_index == 2 and c.number_format]
        self.assertEqual({c.row_index for c in date_cells}, {2, 3, 4})
        b2 = next(c for c in date_cells if c.row_index == 2)
        self.assertEqual(b2.value, "2024-12-30")
        self.assertEqual(b2.raw_value, "45656")
        self.assertEqual(b2.number_format, "m/d/yyyy")


if __name__ == "__main__":
    unittest.main()
