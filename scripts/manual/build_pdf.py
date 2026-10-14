"""Generate the shipped manual PDF from the same Markdown used by the help page.

Run from the repository root: uv run python scripts/manual/build_pdf.py
Requires ReportLab (an existing application dependency) and a Chinese TTF font.
The completed PDF is committed under frontend/public; Docker does not run this tool.
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / "frontend/src/content/manual"
PUBLIC = ROOT / "frontend/public"
MANUAL = json.loads((CONTENT / "manifest.json").read_text(encoding="utf-8"))
OUTPUT = PUBLIC / MANUAL["pdf"].lstrip("/")
FONT = "ManualChinese"
LATIN_FONT = "ManualLatin"
WIDTH = A4[0] - 40 * mm


def register_font() -> None:
    candidates = [
        Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttf"),
    ]
    font = next((p for p in candidates if p.is_file()), None)
    if font is None:
        raise SystemExit(
            "Install a Chinese TTF font (fonts-droid-fallback) before generating the PDF."
        )
    pdfmetrics.registerFont(TTFont(FONT, str(font)))
    pdfmetrics.registerFontFamily(
        FONT, normal=FONT, bold=FONT, italic=FONT, boldItalic=FONT
    )
    latin = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not latin.is_file():
        raise SystemExit("Install fonts-dejavu-core before generating the PDF.")
    pdfmetrics.registerFont(TTFont(LATIN_FONT, str(latin)))
    pdfmetrics.registerFontFamily(
        LATIN_FONT,
        normal=LATIN_FONT,
        bold=LATIN_FONT,
        italic=LATIN_FONT,
        boldItalic=LATIN_FONT,
    )


def inline(text: str) -> str:
    # DroidSansFallback has Chinese glyphs but no ASCII cmap. Embed both fonts
    # so numbers, version strings and mixed Chinese/English remain readable.
    result = []
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        bold = part.startswith("**") and part.endswith("**")
        code = part.startswith("`") and part.endswith("`")
        value = part[2:-2] if bold else part[1:-1] if code else part
        runs: list[tuple[str, str]] = []
        for char in value:
            name = (
                FONT
                if ord(char) > 127
                and ord(char) in pdfmetrics.getFont(FONT).face.charToGlyph
                else LATIN_FONT
            )
            if runs and runs[-1][0] == name:
                runs[-1] = (name, runs[-1][1] + char)
            else:
                runs.append((name, char))
        markup = "".join(
            f'<font name="{name}">{escape(run)}</font>' for name, run in runs
        )
        if bold:
            markup = f"<b>{markup}</b>"
        if code:
            markup = f'<font color="#0f766e">{markup}</font>'
        result.append(markup)
    return "".join(result)


class ManualDocument(SimpleDocTemplate):
    def afterFlowable(self, flowable):  # ReportLab callback
        chapter = getattr(flowable, "manual_chapter", None)
        if chapter:
            chapter_id, title = chapter
            self.canv.bookmarkPage(chapter_id)
            self.canv.addOutlineEntry(title, chapter_id, level=0)
            self.notify("TOCEntry", (0, inline(title), self.page, chapter_id))


def main() -> None:
    register_font()
    body = ParagraphStyle(
        "Body", fontName=FONT, fontSize=10, leading=17, wordWrap="CJK", spaceAfter=8
    )
    heading = ParagraphStyle(
        "Heading", parent=body, fontSize=17, leading=25, spaceAfter=15
    )
    subheading = ParagraphStyle(
        "Subheading", parent=body, fontSize=12, leading=20, spaceBefore=12, spaceAfter=8
    )
    caption = ParagraphStyle(
        "Caption",
        parent=body,
        fontSize=8,
        leading=13,
        alignment=1,
        textColor=colors.HexColor("#64748b"),
    )
    cell = ParagraphStyle("Cell", parent=body, fontSize=9, leading=15, spaceAfter=0)
    story = [
        Spacer(1, 35 * mm),
        Paragraph(
            inline("Hardware DataBase"),
            ParagraphStyle("Cover", parent=heading, fontSize=26, leading=38),
        ),
        Paragraph("图文用户使用手册", heading),
        Paragraph(inline(f"{MANUAL['version']} · {MANUAL['updatedAt']}"), body),
        Spacer(1, 12 * mm),
        Paragraph(inline("适用角色：员工 / 系统管理员"), body),
        Paragraph(inline(f"适用基线：{MANUAL['applicationBaseline']}"), body),
        Paragraph("桌面端操作说明 · 目录导航 · 图文阅读", body),
        Spacer(1, 10 * mm),
        Paragraph(
            inline(
                "本手册沿用 V2.1 的章节结构，校正现有入口与操作说明。截图为脱敏示例界面，不代表真实业务任务已经成功执行。"
            ),
            body,
        ),
        PageBreak(),
        Paragraph("目录", heading),
    ]
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle(
            "TOC",
            parent=body,
            fontName=LATIN_FONT,
            fontSize=10,
            leading=22,
            leftIndent=0,
            firstLineIndent=0,
            spaceBefore=4,
        )
    ]
    story.extend([toc, PageBreak()])

    for index, chapter in enumerate(MANUAL["chapters"]):
        if index:
            story.append(PageBreak())
        title = Paragraph(inline(chapter["title"]), heading)
        title.manual_chapter = (chapter["id"], chapter["title"])
        story.append(title)
        lines = (CONTENT / chapter["source"]).read_text(encoding="utf-8").splitlines()
        line_index = 0
        while line_index < len(lines):
            line = lines[line_index].strip()
            line_index += 1
            if not line:
                continue
            if line.startswith("|"):
                rows = [line]
                while line_index < len(lines) and lines[line_index].strip().startswith(
                    "|"
                ):
                    rows.append(lines[line_index].strip())
                    line_index += 1
                rows = [r for r in rows if not re.fullmatch(r"[|:\-\s]+", r)]
                cells = [
                    [
                        Paragraph(inline(c.strip()), cell)
                        for c in r.strip("|").split("|")
                    ]
                    for r in rows
                ]
                table = Table(
                    cells,
                    colWidths=[WIDTH / len(cells[0])] * len(cells[0]),
                    repeatRows=1,
                    hAlign="LEFT",
                )
                table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9e0e8")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                            ("TOPPADDING", (0, 0), (-1, -1), 7),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                        ]
                    )
                )
                story.extend([table, Spacer(1, 8)])
                continue
            image_match = re.fullmatch(r"!\[(.*?)\]\((.*?)\)", line)
            if image_match:
                image_path = PUBLIC / image_match[2].lstrip("/")
                if not image_path.is_file():
                    raise SystemExit(f"Missing screenshot: {image_path}")
                image = Image(str(image_path))
                scale = min(WIDTH / image.imageWidth, 95 * mm / image.imageHeight)
                image.drawWidth = image.imageWidth * scale
                image.drawHeight = image.imageHeight * scale
                story.append(
                    KeepTogether(
                        [
                            Spacer(1, 6),
                            image,
                            Spacer(1, 5),
                            Paragraph(inline(image_match[1]), caption),
                        ]
                    )
                )
                continue
            style = subheading if line.startswith("### ") else body
            story.append(Paragraph(inline(line.removeprefix("### ")), style))

    def page_chrome(canvas, doc):
        canvas.saveState()
        canvas.setFont(LATIN_FONT, 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        footer = Paragraph(
            inline(f"Hardware DataBase 使用手册 {MANUAL['version']}"), caption
        )
        footer.wrap(WIDTH, 20)
        footer.drawOn(canvas, 20 * mm, 12 * mm)
        canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, str(doc.page))
        canvas.restoreState()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    document = ManualDocument(
        str(OUTPUT),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=MANUAL["title"] + " " + MANUAL["version"],
        author="Hardware DataBase",
        pageCompression=1,
    )
    document.multiBuild(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    print(f"Generated {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
