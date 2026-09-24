import io
import os
import tempfile
import unittest
import zipfile

from src.ingestion.embedded_extractor import (
    EmbeddedObject,
    build_child_name,
    extract_embedded_objects,
    write_embedded_child,
)


def _docx_bytes(*paragraphs: str) -> bytes:
    body = "".join(
        "<w:p><w:r><w:t>"
        + "</w:t></w:r><w:r><w:t>".join(segment for segment in paragraph)
        + "</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _xlsx_bytes(marker: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", f"<workbook>{marker}</workbook>")
    return buffer.getvalue()


class EmbeddedExtractorTests(unittest.TestCase):
    def test_extracts_ooxml_payloads_and_skips_ole_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "架构说明书.xlsx")
            with zipfile.ZipFile(parent, "w") as archive:
                archive.writestr(
                    "xl/embeddings/Microsoft_Word_Document.docx",
                    _docx_bytes(("5.", "3", " ", "摄像头模块")),
                )
                archive.writestr(
                    "xl/embeddings/Microsoft_Word_Document1.docx",
                    _docx_bytes(("电源模块设计方案",)),
                )
                archive.writestr(
                    "xl/embeddings/Microsoft_Excel_Worksheet.xlsx",
                    _xlsx_bytes("sheet"),
                )
                archive.writestr("xl/embeddings/oleObject1.bin", b"\xd0\xcf\x11\xe0binary")

            extraction = extract_embedded_objects(parent)

        self.assertEqual(len(extraction.objects), 3)
        self.assertEqual(extraction.skipped, ["xl/embeddings/oleObject1.bin"])
        by_name = {item.original_name: item for item in extraction.objects}
        self.assertEqual(by_name["Microsoft_Word_Document.docx"].title, "5.3 摄像头模块")
        self.assertEqual(by_name["Microsoft_Word_Document1.docx"].title, "电源模块设计方案")
        self.assertEqual(by_name["Microsoft_Excel_Worksheet.xlsx"].extension, ".xlsx")
        self.assertTrue(all(len(item.content_hash) == 64 for item in extraction.objects))

    def test_returned_payload_bytes_are_routable_ooxml(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "parent.xlsx")
            with zipfile.ZipFile(parent, "w") as archive:
                archive.writestr("word/embeddings/child.docx", _docx_bytes(("标题",)))

            extraction = extract_embedded_objects(parent)

        self.assertTrue(zipfile.is_zipfile(io.BytesIO(extraction.objects[0].data)))

    def test_child_name_uses_parent_and_title_then_hash_on_collision(self):
        first = EmbeddedObject(
            entry_name="xl/embeddings/Microsoft_Word_Document.docx",
            original_name="Microsoft_Word_Document.docx",
            extension=".docx",
            data=b"one",
            content_hash="a" * 64,
            title="电源模块设计方案",
        )
        second = EmbeddedObject(
            entry_name="xl/embeddings/Microsoft_Word_Document1.docx",
            original_name="Microsoft_Word_Document1.docx",
            extension=".docx",
            data=b"two",
            content_hash="b" * 64,
            title="电源模块设计方案",
        )
        used: set[str] = set()

        first_name = build_child_name("架构说明书.xlsx", first, used)
        second_name = build_child_name("架构说明书.xlsx", second, used)

        self.assertEqual(first_name, "架构说明书 [嵌入] 电源模块设计方案.docx")
        self.assertEqual(
            second_name,
            "架构说明书 [嵌入] 电源模块设计方案 #bbbbbb.docx",
        )

    def test_child_name_falls_back_to_original_name_when_title_missing(self):
        embedded = EmbeddedObject(
            entry_name="xl/embeddings/Microsoft_Excel_Worksheet.xlsx",
            original_name="Microsoft_Excel_Worksheet.xlsx",
            extension=".xlsx",
            data=b"data",
            content_hash="c" * 64,
            title="",
        )

        name = build_child_name("架构说明书.xlsx", embedded, set())

        self.assertEqual(name, "架构说明书 [嵌入] Microsoft_Excel_Worksheet.xlsx")

    def test_missing_file_returns_empty_extraction(self):
        extraction = extract_embedded_objects("/nonexistent/path.xlsx")
        self.assertEqual(extraction.objects, [])
        self.assertEqual(extraction.skipped, [])

    def test_write_embedded_child_persists_payload_bytes(self):
        embedded = EmbeddedObject(
            entry_name="xl/embeddings/child.docx",
            original_name="child.docx",
            extension=".docx",
            data=b"payload-bytes",
            content_hash="d" * 64,
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_path = write_embedded_child(embedded, "父 [嵌入] 子.docx", tmp)
            with open(out_path, "rb") as handle:
                written = handle.read()

        self.assertTrue(out_path.endswith("父 [嵌入] 子.docx"))
        self.assertEqual(written, b"payload-bytes")


if __name__ == "__main__":
    unittest.main()
