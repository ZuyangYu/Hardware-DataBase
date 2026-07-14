from __future__ import annotations

from collections import Counter, defaultdict

from src.circuit.models import CircuitModule, ModuleRegion, SchematicPage


def estimate_regions_from_pages(pages: list[SchematicPage]) -> list[ModuleRegion]:
    """Estimate coarse module/page regions for PDF-only mode.

    Without vector coordinates or OCR boxes we cannot draw precise regions yet,
    so each populated page becomes a coarse full-page region. The structure is
    intentionally compatible with later PyMuPDF/pdfplumber coordinate output.
    """

    regions = []
    for page in pages:
        if not page.labels and not page.text:
            continue
        bbox = None
        if page.width and page.height:
            bbox = (0.0, 0.0, page.width, page.height)
        regions.append(
            ModuleRegion(
                module_id=f"pdf_page_{page.page_number}",
                page_number=page.page_number,
                bbox=bbox,
                confidence=0.35 if page.labels else 0.15,
                strategy="pdf_page",
            )
        )
    return regions


def estimate_regions_from_modules(
    modules: list[CircuitModule],
    pages: list[SchematicPage],
) -> list[ModuleRegion]:
    """Map EDF modules to schematic pages using refdes label votes."""

    labels_by_page: dict[int, set[str]] = defaultdict(set)
    for page in pages:
        for label in page.labels:
            if label.kind == "refdes":
                labels_by_page[page.page_number].add(label.text.upper())

    regions = []
    for module in modules:
        votes: Counter[int] = Counter()
        module_refs = {ref.upper() for ref in module.instances}
        for page_number, labels in labels_by_page.items():
            hits = module_refs & labels
            if hits:
                votes[page_number] = len(hits)
        if not votes:
            continue
        page_number, hit_count = votes.most_common(1)[0]
        page = next((candidate for candidate in pages if candidate.page_number == page_number), None)
        bbox = (0.0, 0.0, page.width, page.height) if page and page.width and page.height else None
        regions.append(
            ModuleRegion(
                module_id=module.module_id,
                page_number=page_number,
                bbox=bbox,
                confidence=min(0.95, hit_count / max(1, len(module.instances))),
                strategy="edf_refdes_votes",
            )
        )
    return regions
