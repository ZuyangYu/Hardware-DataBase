import unittest

from src.pipelines.spreadsheet.xlsx_parser import (
    _NumberFormatResolver,
    _classify_format,
    _format_value,
    _serial_to_datetime_display,
)


# 2024-12-30 的 Excel 序列号(epoch=1899-12-31 + 1900 幻影日修正)。
SERIAL_2024_12_30 = 45656


class NumberFormatResolverTests(unittest.TestCase):
    def _resolver_from_xml(self, styles_xml: str) -> _NumberFormatResolver:
        return _NumberFormatResolver.from_styles_xml(styles_xml.encode("utf-8"))

    def test_empty_resolver_passes_through(self):
        resolver = _NumberFormatResolver.empty()
        self.assertEqual(resolver.resolve("45656", 0), ("45656", "45656"))
        self.assertEqual(resolver.format_code_for(0), "")
        self.assertEqual(resolver.format_code_for(None), "")

    def test_malformed_styles_yields_empty_resolver(self):
        resolver = _NumberFormatResolver.from_styles_xml(b"<not xml")
        self.assertEqual(resolver.resolve("1", 0), ("1", "1"))

    def test_builtin_format_resolved_by_style_index(self):
        # cellXfs: index 0 -> numFmtId 14 (m/d/yyyy), index 1 -> numFmtId 10 (0.00%)
        styles = """<?xml version="1.0"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<cellXfs count="2">
  <xf numFmtId="14"/>
  <xf numFmtId="10"/>
</cellXfs>
</styleSheet>"""
        resolver = self._resolver_from_xml(styles)
        self.assertEqual(resolver.format_code_for(0), "m/d/yyyy")
        self.assertEqual(resolver.format_code_for(1), "0.00%")
        self.assertEqual(resolver.resolve(str(SERIAL_2024_12_30), 0)[1], "2024-12-30")

    def test_custom_format_overrides_builtin_id(self):
        # numFmtId 164 is custom (>=164); resolver must use its formatCode.
        styles = """<?xml version="1.0"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy/mm/dd"/></numFmts>
<cellXfs count="1"><xf numFmtId="164"/></cellXfs>
</styleSheet>"""
        resolver = self._resolver_from_xml(styles)
        self.assertEqual(resolver.format_code_for(0), "yyyy/mm/dd")

    def test_style_index_out_of_range_returns_empty(self):
        resolver = _NumberFormatResolver(cellxfs_numfmt_ids=[14], custom_formats={})
        self.assertEqual(resolver.format_code_for(5), "")


class FormatValueTests(unittest.TestCase):
    def test_date_serial_to_iso(self):
        self.assertEqual(_format_value(str(SERIAL_2024_12_30), "m/d/yyyy"), "2024-12-30")

    def test_datetime_serial(self):
        # 45656.5 -> 2024-12-30 12:00:00
        self.assertEqual(_format_value("45656.5", "m/d/yyyy h:mm"), "2024-12-30 12:00:00")

    def test_time_only_serial(self):
        self.assertEqual(_format_value("0.5", "h:mm"), "12:00:00")

    def test_percentage_with_decimals(self):
        self.assertEqual(_format_value("0.5", "0.00%"), "50.00%")

    def test_percentage_no_decimals(self):
        self.assertEqual(_format_value("0.5", "0%"), "50%")

    def test_currency_with_symbol(self):
        self.assertEqual(_format_value("1234.5", "[$$-409]#,##0.00"), "$1234.5")

    def test_currency_symbol_undetectable_keeps_raw(self):
        # Built-in id 5 uses a placeholder currency char; no [$...] marker -> keep raw.
        self.assertEqual(_format_value("1234.5", '"¤"#,##0.00'), "1234.5")

    def test_scientific_passthrough(self):
        self.assertEqual(_format_value("1.5", "0.00E+00"), "1.5")

    def test_non_numeric_with_date_style_keeps_raw(self):
        self.assertEqual(_format_value("hello", "m/d/yyyy"), "hello")

    def test_general_returns_raw(self):
        self.assertEqual(_format_value("42", "General"), "42")


class SerialPhantomDayTests(unittest.TestCase):
    """1900 幻影日修正:serial 60 -> 1900-02-29(Excel 虚构日),61 -> 1900-03-01。"""

    def test_phantom_day(self):
        self.assertEqual(_serial_to_datetime_display(60, True, False), "1900-02-29")

    def test_day_after_phantom(self):
        self.assertEqual(_serial_to_datetime_display(61, True, False), "1900-03-01")

    def test_first_serial(self):
        self.assertEqual(_serial_to_datetime_display(1, True, False), "1900-01-01")


class ClassifyFormatTests(unittest.TestCase):
    def test_text_marker(self):
        self.assertEqual(_classify_format("@"), "text")

    def test_percentage(self):
        self.assertEqual(_classify_format("0.00%"), "percentage")

    def test_date(self):
        self.assertEqual(_classify_format("m/d/yyyy"), "date")

    def test_datetime(self):
        self.assertEqual(_classify_format("m/d/yyyy h:mm"), "datetime")

    def test_time_only(self):
        self.assertEqual(_classify_format("mm:ss"), "time")

    def test_currency_bracketed(self):
        self.assertEqual(_classify_format("[$$-409]#,##0.00"), "currency")

    def test_section_split_takes_first(self):
        # Negative section has % but positive section is a number; classifier
        # should look at the first (positive) section.
        self.assertEqual(_classify_format("0.00;[Red]-0.00%"), "number")

    def test_scientific_is_number(self):
        self.assertEqual(_classify_format("0.00E+00"), "number")


if __name__ == "__main__":
    unittest.main()
