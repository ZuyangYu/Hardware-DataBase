from __future__ import annotations

import re
from pathlib import Path

from src.circuit.models import SchematicLabel, SchematicPage


_REFDES_RE = re.compile(r"\b(?:U|R|C|L|D|Q|J|P|TP|Y|X|F|K|RN|CN)\d+(?:-\d+)?\b", re.IGNORECASE)
_NET_LABEL_RE = re.compile(r"\b[A-Z][A-Z0-9_./+-]{2,}\b")


class PdfSchematicParser:
    """Text-layer schematic PDF parser.

    This is the deterministic Phase 2 path. It extracts page text and labels
    that can later be cross-referenced with EDF instances. Scanned/OCR-only
    files are reported as pages with empty text so OCR can be plugged in later.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.warnings: list[str] = []

    def parse(self) -> list[SchematicPage]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("pypdf is required to parse schematic PDFs.") from exc

        reader = PdfReader(self.file_path)
        pages: list[SchematicPage] = []
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            width = float(page.mediabox.width) if page.mediabox else None
            height = float(page.mediabox.height) if page.mediabox else None
            labels = self._extract_labels(text, index)
            if not text:
                self.warnings.append(f"Page {index} has no text layer: {Path(self.file_path).name}")
            pages.append(
                SchematicPage(
                    page_number=index,
                    width=width,
                    height=height,
                    text=text,
                    labels=labels,
                )
            )
        return pages

    def _extract_labels(self, text: str, page_number: int) -> list[SchematicLabel]:
        labels: dict[tuple[str, str], SchematicLabel] = {}
        for match in _REFDES_RE.finditer(text):
            value = match.group(0).upper()
            labels[(value, "refdes")] = SchematicLabel(text=value, page_number=page_number, kind="refdes")
        for match in _NET_LABEL_RE.finditer(text):
            value = match.group(0).upper()
            if len(value) <= 24 and not value.isdigit():
                labels.setdefault(
                    (value, "net_label"),
                    SchematicLabel(text=value, page_number=page_number, kind="net_label"),
                )
        return sorted(labels.values(), key=lambda label: (label.page_number, label.kind, label.text))
