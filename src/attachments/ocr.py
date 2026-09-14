"""Optional CPU OCR for low-text-density PDF pages.

The core attachment parser does not require OCR binaries.  When enabled, the
worker renders only selected PDF pages with ``pdftoppm`` and sends each image
to a local ``tesseract`` process.  Both commands are invoked without a shell
and are bounded by the attachment parse timeout, so a missing binary or a
single bad page degrades that asset instead of taking down the worker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Protocol

import src.settings


class OcrEngine(Protocol):
    """Replaceable page OCR boundary used by ``LocalDocumentParser``."""

    provider: str

    def recognize_page(self, source_path: str, page_number: int) -> str:
        """Return recognized text for one 1-based PDF page."""


class OcrUnavailableError(RuntimeError):
    """Raised when the optional local OCR toolchain is not installed."""


class LocalCpuOcrEngine:
    """Render and OCR one PDF page using local CPU command-line tools."""

    provider = "local_cpu_tesseract"

    def __init__(
        self,
        *,
        renderer_command: str | None = None,
        ocr_command: str | None = None,
        language: str = "eng+chi_sim",
        dpi: int = 200,
    ) -> None:
        self.renderer_command = renderer_command or shutil.which("pdftoppm")
        self.ocr_command = ocr_command or shutil.which("tesseract")
        self.language = str(language or "eng+chi_sim")
        self.dpi = max(72, min(int(dpi), 400))

    def recognize_page(self, source_path: str, page_number: int) -> str:
        if not self.renderer_command:
            raise OcrUnavailableError("pdftoppm is not installed")
        if not self.ocr_command:
            raise OcrUnavailableError("tesseract is not installed")
        page_number = int(page_number)
        if page_number < 1:
            raise ValueError("page_number must be positive")
        if not os.path.isfile(source_path):
            raise OcrUnavailableError("PDF source is unavailable")

        timeout = max(1, int(src.settings.CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS))
        with tempfile.TemporaryDirectory(prefix="hdb-attachment-ocr-") as temp_dir:
            image_prefix = os.path.join(temp_dir, "page")
            render = subprocess.run(
                [
                    self.renderer_command,
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-r",
                    str(self.dpi),
                    "-png",
                    source_path,
                    image_prefix,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            if render.returncode != 0:
                detail = (render.stderr or render.stdout or "").strip()[:300]
                raise RuntimeError(f"PDF page rendering failed{': ' + detail if detail else ''}")
            image_path = f"{image_prefix}.png"
            if not os.path.isfile(image_path):
                raise RuntimeError("PDF renderer produced no page image")

            recognized = subprocess.run(
                [
                    self.ocr_command,
                    image_path,
                    "stdout",
                    "--psm",
                    "3",
                    "-l",
                    self.language,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            if recognized.returncode != 0:
                detail = (recognized.stderr or recognized.stdout or "").strip()[:300]
                raise RuntimeError(f"OCR process failed{': ' + detail if detail else ''}")
            return (recognized.stdout or "").strip()


def default_ocr_engine() -> OcrEngine:
    """Build the optional local implementation lazily at parse time."""

    return LocalCpuOcrEngine()
