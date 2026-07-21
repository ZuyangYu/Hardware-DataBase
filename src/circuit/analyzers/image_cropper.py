"""Module-region cropping for schematic PDFs.

The cropper materialises ``storage/circuits/{kb}/{id}/module_screenshots/{module_id}.png``
files from PDF pages cached at ``pdf_cache/`` plus the ``module_regions``
attached to the ``CircuitDesign``. It is *best effort* — when PyMuPDF or
Pillow is unavailable, or when no schematic PDF is cached, the cropper writes a
small JSON sidecar describing the requested region so downstream consumers
(UI, multimodal descriptor) can still locate the work that was deferred.

Plan §3.4 ("module_screenshots/" directory) + §8 (multimodal description with
cropped images) drive this layout.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

from src.circuit.image_cache import ImageCache
from src.circuit.models import CircuitDesign, ModuleRegion
from src.circuit.store import CircuitStore


def _try_import_fitz():
    try:
        import fitz  # type: ignore

        return fitz
    except Exception:  # pragma: no cover - optional dep
        return None


class ImageCropper:
    """Crop schematic PDF regions per module into the design's screenshot dir."""

    def __init__(self, store: CircuitStore | None = None, cache: ImageCache | None = None):
        self.store = store or CircuitStore()
        self.cache = cache or ImageCache(self.store)

    def crop_modules(self, design: CircuitDesign, dpi: int = 200) -> dict[str, str]:
        """Render each module's region to PNG (or fall back to a sidecar).

        Returns a mapping ``{module_id: path}``. ``path`` may be the rendered
        PNG, or — when rasterisation is impossible — the JSON sidecar that
        documents the pending region for later retry.
        """
        results: dict[str, str] = {}
        if not design.module_regions:
            return results

        pdf_paths = self.store.list_pdf_cache(design.kb_name, design.design_id)

        fitz = _try_import_fitz()
        documents: dict[str, "fitz.Document"] = {}  # type: ignore[name-defined]

        try:
            for region in design.module_regions:
                if region.module_id in results:
                    continue
                target_png = self.store.module_screenshot_path(
                    design.kb_name, design.design_id, region.module_id, ext="png"
                )

                rendered = False
                if fitz is not None and pdf_paths and region.bbox:
                    rendered = self._render_with_fitz(
                        fitz, pdf_paths, region, target_png, dpi, documents
                    )

                if rendered:
                    results[region.module_id] = target_png
                else:
                    sidecar = self._write_sidecar(design, region, target_png)
                    results[region.module_id] = sidecar
        finally:
            for doc in documents.values():
                try:
                    doc.close()
                except Exception:
                    pass

        return results

    # ── helpers ──────────────────────────────────────────────────────────

    def _render_with_fitz(
        self,
        fitz,
        pdf_paths: list[str],
        region: ModuleRegion,
        target_png: str,
        dpi: int,
        documents: dict,
    ) -> bool:
        try:
            doc_path = pdf_paths[0]  # v1: single cached schematic PDF
            doc = documents.get(doc_path)
            if doc is None:
                doc = fitz.open(doc_path)
                documents[doc_path] = doc

            page_idx = max(0, region.page_number - 1)
            if page_idx >= doc.page_count:
                return False

            page = doc.load_page(page_idx)
            x0, y0, x1, y1 = region.bbox  # type: ignore[misc]
            rect = fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            rect = rect & page.rect
            if rect.is_empty:
                return False

            zoom = dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
            os.makedirs(os.path.dirname(target_png), exist_ok=True)
            pix.save(target_png)
            return True
        except Exception:
            return False

    def _write_sidecar(self, design: CircuitDesign, region: ModuleRegion, target_png: str) -> str:
        sidecar_path = target_png + ".pending.json"
        payload = {
            "design_id": design.design_id,
            "kb_name": design.kb_name,
            "module_id": region.module_id,
            "page_number": region.page_number,
            "bbox": list(region.bbox) if region.bbox else None,
            "strategy": region.strategy,
            "confidence": region.confidence,
            "reason": "PDF cache missing or PyMuPDF unavailable; crop deferred.",
        }
        os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return sidecar_path
