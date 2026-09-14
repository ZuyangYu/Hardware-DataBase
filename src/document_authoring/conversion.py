"""Deterministic conversion of filled template artifacts.

Template renderers intentionally preserve OOXML package parts and therefore do
not pretend that a Markdown export is the finished template.  This module is
the second, explicit stage in that workflow: it reads a validated native
artifact, renders its semantic paragraphs/tables into PDF or PowerPoint, and
validates the resulting package before it can be exposed as an artifact.

The converter is deliberately bounded and fail-closed.  It is not a promise of
pixel-perfect Office layout (that requires a licensed/headless Office engine),
but it does preserve the document's headings, paragraphs, worksheets and table
cells instead of placing raw Markdown in a PDF.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from zipfile import BadZipFile, ZipFile

from src.document_authoring.artifact_preview import preview_artifact


class TemplateConversionError(ValueError):
    """A deterministic conversion or output-validation failure."""


class TemplateConversionUnavailable(RuntimeError):
    """Optional conversion dependencies are unavailable."""


@dataclass(frozen=True)
class TemplateConversionResult:
    content: bytes
    source_format: str
    target_format: str
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class TemplateArtifactConversionService:
    """Convert a native filled artifact into a validated PDF or PPTX."""

    converter_version = "template-semantic-converter/1"
    supported_sources = frozenset({"docx", "xlsx", "xlsm", "markdown", "md"})
    supported_targets = frozenset({"pdf", "pptx"})
    max_package_entries = 2_000
    max_uncompressed_bytes = 100 * 1024 * 1024
    max_rows = 500
    max_columns = 30
    max_paragraphs = 1_000
    max_slides = 100

    def convert(
        self,
        content: bytes,
        *,
        source_format: str,
        target_format: str,
        security_approved: bool = False,
    ) -> TemplateConversionResult:
        source = self._normalize_format(source_format)
        target = self._normalize_format(target_format)
        if source not in self.supported_sources:
            raise TemplateConversionError(f"unsupported source format: {source or 'unknown'}")
        if target not in self.supported_targets:
            raise TemplateConversionError(f"unsupported target format: {target or 'unknown'}")
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise TemplateConversionError("source artifact content is empty")
        source_bytes = bytes(content)
        if source in {"xlsx", "xlsm", "docx"}:
            self._validate_office_source(source_bytes, source, security_approved=security_approved)
        semantic = self._extract_semantic(source_bytes, source)
        if target == "pdf":
            output, metadata, warnings = self._render_pdf(semantic)
            self._validate_pdf(output)
        else:
            output, metadata, warnings = self._render_pptx(semantic)
            self._validate_pptx(output)
        metadata = {
            "converter_version": self.converter_version,
            "source_format": source,
            "target_format": target,
            "source_content_hash": hashlib.sha256(source_bytes).hexdigest(),
            "content_hash": hashlib.sha256(output).hexdigest(),
            **metadata,
        }
        return TemplateConversionResult(
            content=output,
            source_format=source,
            target_format=target,
            metadata=metadata,
            warnings=warnings,
        )

    @staticmethod
    def _normalize_format(value: str) -> str:
        return str(value or "").strip().lower().lstrip(".")

    def _validate_office_source(self, content: bytes, source: str, *, security_approved: bool) -> None:
        try:
            with ZipFile(BytesIO(content)) as package:
                infos = package.infolist()
                if len(infos) > self.max_package_entries:
                    raise TemplateConversionError("source Office package has too many entries")
                if sum(max(0, int(info.file_size)) for info in infos) > self.max_uncompressed_bytes:
                    raise TemplateConversionError("source Office package exceeds the conversion size limit")
                if package.testzip() is not None:
                    raise TemplateConversionError("source Office package contains a corrupt member")
                names = {info.filename.lower() for info in infos}
        except BadZipFile as exc:
            raise TemplateConversionError("source Office package is not a valid OOXML zip") from exc
        active = sorted(
            name for name in names
            if "vbaproject.bin" in name
            or "/activex/" in name
            or "/embeddings/" in name
            or "externallinks" in name
        )
        if active and not security_approved:
            raise TemplateConversionError(
                "source artifact contains active content; conversion requires security approval"
            )
        # A package with no main document part is malformed even if the
        # preview parser would otherwise return an empty warning payload.
        required = "word/document.xml" if source == "docx" else "xl/workbook.xml"
        if required not in names:
            raise TemplateConversionError(f"source {source.upper()} package is missing {required}")

    def _extract_semantic(self, content: bytes, source: str) -> dict[str, Any]:
        if source in {"xlsx", "xlsm", "docx"}:
            preview = preview_artifact(
                content,
                source,
                max_sheets=20,
                max_rows=self.max_rows,
                max_columns=self.max_columns,
                max_paragraphs=self.max_paragraphs,
            )
            warnings = list(preview.get("warnings") or [])
            if warnings:
                raise TemplateConversionError("source artifact could not be parsed: " + "; ".join(warnings[:3]))
            return {
                "format": source,
                "paragraphs": list(preview.get("paragraphs") or []),
                "tables": list(preview.get("tables") or []),
                "sheets": list(preview.get("sheets") or []),
                "truncated": bool(preview.get("truncated")),
            }
        return self._markdown_semantic(content)

    def _markdown_semantic(self, content: bytes) -> dict[str, Any]:
        text = content.decode("utf-8", errors="replace")
        paragraphs: list[str] = []
        tables: list[list[list[str]]] = []
        lines = text.splitlines()
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue
            if line.startswith("|") and "|" in line[1:]:
                rows: list[list[str]] = []
                while index < len(lines) and lines[index].strip().startswith("|"):
                    cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                    if not cells or all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
                        index += 1
                        continue
                    rows.append(cells[: self.max_columns])
                    index += 1
                    if len(rows) >= self.max_rows:
                        break
                if rows:
                    tables.append(rows)
                continue
            paragraphs.append(line)
            index += 1
            if len(paragraphs) >= self.max_paragraphs:
                break
        return {
            "format": "markdown",
            "paragraphs": paragraphs,
            "tables": tables,
            "sheets": [],
            "truncated": len(lines) > self.max_paragraphs,
        }

    def _render_pdf(self, semantic: dict[str, Any]) -> tuple[bytes, dict[str, Any], list[str]]:
        try:
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_LEFT
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.platypus import (
                BaseDocTemplate,
                Frame,
                PageTemplate,
                Paragraph,
                Spacer,
                Table,
                TableStyle,
            )
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise TemplateConversionUnavailable("reportlab is not installed") from exc

        font_name = "Helvetica"
        try:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            font_name = "STSong-Light"
        except Exception:
            # Helvetica still produces a readable artifact for Latin-only
            # templates; the warning makes CJK fallback visible to operators.
            pass
        page_size = A4
        if semantic.get("sheets") and any(len(sheet.get("rows") or []) > 0 for sheet in semantic["sheets"]):
            page_size = landscape(A4)
        output = BytesIO()
        margin = 15 * mm
        frame = Frame(margin, margin, page_size[0] - 2 * margin, page_size[1] - 2 * margin, id="normal")
        document = BaseDocTemplate(output, pagesize=page_size, leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin)
        document.addPageTemplates([PageTemplate(id="template", frames=[frame])])
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("conversion-title", parent=styles["Title"], fontName=font_name, alignment=TA_LEFT, fontSize=18, leading=23, spaceAfter=10)
        body_style = ParagraphStyle("conversion-body", parent=styles["BodyText"], fontName=font_name, fontSize=9.5, leading=14, spaceAfter=5)
        heading_style = ParagraphStyle("conversion-heading", parent=styles["Heading2"], fontName=font_name, fontSize=13, leading=17, spaceBefore=9, spaceAfter=6)
        story: list[Any] = []
        title = "模板成品（语义转换）"
        if semantic.get("paragraphs"):
            title = str(semantic["paragraphs"][0])[:200]
        story.append(Paragraph(self._escape(title), title_style))
        story.append(Paragraph("本文件由已校验的模板成品转换生成，表格和段落按结构化内容排版。", body_style))
        for paragraph in semantic.get("paragraphs", [])[1:]:
            story.append(Paragraph(self._escape(str(paragraph)), body_style))
        tables = list(semantic.get("tables") or [])
        for table in tables:
            story.extend(self._pdf_table(table, font_name, body_style, colors, Paragraph, Table, TableStyle, Spacer, mm))
        for sheet in semantic.get("sheets", []):
            story.append(Paragraph(self._escape(str(sheet.get("name") or "工作表")), heading_style))
            story.extend(self._pdf_table(sheet.get("rows") or [], font_name, body_style, colors, Paragraph, Table, TableStyle, Spacer, mm))
        if not tables and not semantic.get("sheets") and not semantic.get("paragraphs"):
            story.append(Paragraph("（模板没有可转换的正文或表格内容）", body_style))
        document.build(story)
        warnings = ["源模板内容超过转换预览上限，输出已截断。"] if semantic.get("truncated") else []
        return output.getvalue(), {
            "page_count": self._pdf_page_count(output.getvalue()),
            "table_count": len(tables),
            "sheet_count": len(semantic.get("sheets") or []),
        }, warnings

    @staticmethod
    def _pdf_table(table_data, font_name, body_style, colors, Paragraph, Table, TableStyle, Spacer, mm):
        rows = [list(map(str, row)) for row in (table_data or []) if isinstance(row, (list, tuple))]
        if not rows:
            return []
        max_columns = max(len(row) for row in rows)
        normalized = [row + [""] * (max_columns - len(row)) for row in rows]
        wrapped = [[Paragraph(TemplateArtifactConversionService._escape(cell), body_style) for cell in row] for row in normalized]
        table = Table(wrapped, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#132238")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#9aa8b8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return [Spacer(1, 4 * mm), table, Spacer(1, 5 * mm)]

    def _render_pptx(self, semantic: dict[str, Any]) -> tuple[bytes, dict[str, Any], list[str]]:
        try:
            from pptx import Presentation
            from pptx.util import Inches, Pt
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise TemplateConversionUnavailable("python-pptx is not installed") from exc

        presentation = Presentation()
        # Use a widescreen canvas because the table renderer intentionally
        # allows up to twelve columns.  The python-pptx default is the older
        # 10 x 7.5 inch canvas, which would clip the right side of a converted
        # ICD table even though the package itself is valid.
        presentation.slide_width = Inches(13.333)
        presentation.slide_height = Inches(7.5)
        title_layout = presentation.slide_layouts[0]
        body_layout = presentation.slide_layouts[5] if len(presentation.slide_layouts) > 5 else presentation.slide_layouts[1]
        paragraphs = [str(value) for value in semantic.get("paragraphs") or []]
        slides = 0
        first = presentation.slides.add_slide(title_layout)
        slides += 1
        first.shapes.title.text = (paragraphs[0] if paragraphs else "模板成品")[:200]
        subtitle = getattr(first, "placeholders", [])
        if len(subtitle) > 1:
            subtitle[1].text = "结构化模板转换结果"
        for paragraph in paragraphs[1:]:
            slide = presentation.slides.add_slide(body_layout)
            slides += 1
            self._set_slide_title(slide, "正文")
            box = slide.shapes.add_textbox(Inches(0.7), Inches(1.35), Inches(11.8), Inches(5.3))
            frame = box.text_frame
            frame.word_wrap = True
            frame.text = paragraph[:2_000]
            for run in frame.paragraphs[0].runs:
                run.font.size = Pt(20)
        for sheet in semantic.get("sheets", []):
            slides += self._pptx_table_slides(presentation, sheet.get("name") or "工作表", sheet.get("rows") or [], body_layout, Inches, Pt)
        for index, table in enumerate(semantic.get("tables") or [], start=1):
            slides += self._pptx_table_slides(presentation, f"表格 {index}", table, body_layout, Inches, Pt)
        if slides > self.max_slides:
            raise TemplateConversionError("converted presentation exceeds the slide limit")
        output = BytesIO()
        presentation.save(output)
        warnings = ["源模板内容超过转换预览上限，输出已截断。"] if semantic.get("truncated") else []
        return output.getvalue(), {"slide_count": slides, "table_count": len(semantic.get("tables") or [])}, warnings

    @staticmethod
    def _set_slide_title(slide, title: str) -> None:
        if slide.shapes.title is not None:
            slide.shapes.title.text = title

    @staticmethod
    def _pptx_table_slides(presentation, title, rows, body_layout, Inches, Pt) -> int:
        rows = [list(map(str, row)) for row in (rows or []) if isinstance(row, (list, tuple))]
        if not rows:
            return 0
        # Split very large tables into bounded slides while keeping the header.
        chunk_size = 20
        count = 0
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            if start and rows:
                chunk = [rows[0], *chunk]
            slide = presentation.slides.add_slide(body_layout)
            count += 1
            TemplateArtifactConversionService._set_slide_title(slide, str(title)[:100])
            columns = max(1, min(12, max(len(row) for row in chunk)))
            shape = slide.shapes.add_table(len(chunk), columns, Inches(0.4), Inches(1.25), Inches(12.5), Inches(5.6))
            table = shape.table
            for row_index, row in enumerate(chunk):
                for column_index in range(columns):
                    cell = table.cell(row_index, column_index)
                    cell.text = row[column_index][:500] if column_index < len(row) else ""
                    for paragraph in cell.text_frame.paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(10 if row_index else 11)
        return count

    @staticmethod
    def _escape(value: str) -> str:
        return (
            str(value or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )

    @staticmethod
    def _pdf_page_count(content: bytes) -> int:
        try:
            from pypdf import PdfReader

            return len(PdfReader(BytesIO(content), strict=False).pages)
        except Exception as exc:
            raise TemplateConversionError("converted PDF failed validation") from exc

    def _validate_pdf(self, content: bytes) -> None:
        if not content.startswith(b"%PDF"):
            raise TemplateConversionError("converted PDF failed magic-header validation")
        pages = self._pdf_page_count(content)
        if pages < 1 or pages > self.max_slides * 10:
            raise TemplateConversionError("converted PDF page count is outside the safe range")

    def _validate_pptx(self, content: bytes) -> None:
        if content[:2] != b"PK":
            raise TemplateConversionError("converted PPTX failed OOXML validation")
        try:
            with ZipFile(BytesIO(content)) as package:
                names = set(package.namelist())
                if "[Content_Types].xml" not in names or "ppt/presentation.xml" not in names:
                    raise TemplateConversionError("converted PPTX is missing required package parts")
                slides = [name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
                if not slides or len(slides) > self.max_slides:
                    raise TemplateConversionError("converted PPTX slide count is outside the safe range")
                if any("vba" in name.lower() or "/embeddings/" in name.lower() for name in names):
                    raise TemplateConversionError("converted PPTX contains active content")
        except BadZipFile as exc:
            raise TemplateConversionError("converted PPTX is not a valid OOXML package") from exc


__all__ = [
    "TemplateArtifactConversionService",
    "TemplateConversionError",
    "TemplateConversionResult",
    "TemplateConversionUnavailable",
]
