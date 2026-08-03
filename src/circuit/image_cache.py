"""Schematic-page / module-screenshot cache.

Plan §3.4 and §4.2 require two per-design artifact directories:

* ``module_screenshots/{module_id}.png``
* ``pdf_cache/<original_pdf>``

This module provides thin helpers around those locations.  It is intentionally
free of Pillow / PyMuPDF imports — the heavy lifting (cropping, rasterising)
happens in :mod:`src.circuit.analyzers.image_cropper` which can degrade
gracefully when the optional CV stack is unavailable.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile

from src.circuit.store import CircuitStore


class ImageCache:
    """Cache for schematic PDFs and module screenshots.

    The cache is keyed by ``(kb_name, design_id)``; every artefact lives under
    the design directory and follows the store's path helpers so the on-disk
    layout stays consistent.
    """

    def __init__(self, store: CircuitStore | None = None):
        self.store = store or CircuitStore()

    # ── PDF cache ─────────────────────────────────────────────────────────

    def cache_pdf(self, kb_name: str, design_id: str, source_path: str) -> str:
        """Copy ``source_path`` into the design's ``pdf_cache/`` directory.

        Re-uses the source filename. If a copy already exists with identical
        bytes the function is a no-op and returns the cached path; otherwise a
        short content-hash suffix is appended to avoid clobbering.
        """
        if not os.path.exists(source_path):
            raise FileNotFoundError(source_path)
        cache_dir = self.store.pdf_cache_dir(kb_name, design_id, create=True)
        filename = os.path.basename(source_path)
        target = os.path.join(cache_dir, filename)

        if os.path.exists(target) and _same_file_contents(source_path, target):
            return target

        if os.path.exists(target):
            digest = _content_digest(source_path)[:10]
            stem, ext = os.path.splitext(filename)
            target = os.path.join(cache_dir, f"{stem}.{digest}{ext}")

        shutil.copy2(source_path, target)
        return target

    def replace_pdf(self, kb_name: str, design_id: str, source_path: str) -> str:
        """Replace the active PDF generation without ever reusing an old file."""
        if not os.path.isfile(source_path):
            raise FileNotFoundError(source_path)
        design_dir = self.store.design_dir(kb_name, design_id, create=True)
        staging_dir = tempfile.mkdtemp(prefix=".pdf-cache-stage-", dir=design_dir)
        staged_source = os.path.join(staging_dir, os.path.basename(source_path))
        cache_dir = self.store.pdf_cache_dir(kb_name, design_id)
        try:
            shutil.copy2(source_path, staged_source)
            if os.path.isdir(cache_dir):
                shutil.rmtree(cache_dir)
            return self.cache_pdf(kb_name, design_id, staged_source)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def get_cached_pdf(self, kb_name: str, design_id: str, filename: str) -> str | None:
        candidate = os.path.join(self.store.pdf_cache_dir(kb_name, design_id), filename)
        return candidate if os.path.exists(candidate) else None

    # ── module screenshots ────────────────────────────────────────────────

    def screenshot_path(self, kb_name: str, design_id: str, module_id: str, ext: str = "png") -> str:
        return self.store.module_screenshot_path(kb_name, design_id, module_id, ext=ext)

    def write_screenshot(self, kb_name: str, design_id: str, module_id: str, image_bytes: bytes, ext: str = "png") -> str:
        target = self.screenshot_path(kb_name, design_id, module_id, ext=ext)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(image_bytes)
        return target

    def list_screenshots(self, kb_name: str, design_id: str) -> list[str]:
        return self.store.list_module_screenshots(kb_name, design_id)


def _same_file_contents(a: str, b: str) -> bool:
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    return _content_digest(a) == _content_digest(b)


def _content_digest(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
