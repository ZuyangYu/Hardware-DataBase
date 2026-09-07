"""Selected-page remote visual analysis for PDF chat attachments."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import src.settings
from src.agents.schemas import Evidence
from src.attachments.models import PART_TYPE_VISUAL_EVIDENCE
from src.attachments.storage import resolve_storage_key
from src.attachments.store import AttachmentStore
from src.core.multimodal_gateway import (
    MultimodalModelGateway,
    VisualAnalysis,
    default_multimodal_gateway,
)


class PageRenderer(Protocol):
    def render_page(self, source_path: str, page_number: int) -> bytes:
        """Render one 1-based PDF page as PNG bytes."""


class PdfPageRenderer:
    """Render one PDF page with the optional local ``pdftoppm`` binary."""

    def __init__(self, *, renderer_command: str | None = None, dpi: int = 160) -> None:
        self.renderer_command = renderer_command or shutil.which("pdftoppm")
        self.dpi = max(72, min(int(dpi), 300))

    def render_page(self, source_path: str, page_number: int) -> bytes:
        if not self.renderer_command:
            raise RuntimeError("pdftoppm is not installed")
        page_number = int(page_number)
        if page_number < 1:
            raise ValueError("page_number must be positive")
        if not os.path.isfile(source_path):
            raise FileNotFoundError("PDF source is unavailable")
        timeout = max(
            1,
            int(getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS", 60)),
        )
        with tempfile.TemporaryDirectory(prefix="hdb-attachment-visual-") as temp_dir:
            prefix = os.path.join(temp_dir, "page")
            rendered = subprocess.run(
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
                    prefix,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            if rendered.returncode != 0:
                detail = (rendered.stderr or rendered.stdout or "").strip()[:300]
                raise RuntimeError(
                    f"PDF page rendering failed{': ' + detail if detail else ''}"
                )
            image_path = f"{prefix}.png"
            with open(image_path, "rb") as image_file:
                return image_file.read()


@dataclass
class VisualAnalysisResult:
    evidence: list[Evidence] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)


class AttachmentVisualAnalyzer:
    """Analyze only ACL-resolved PDF pages and return shared Evidence rows."""

    def __init__(
        self,
        *,
        store: AttachmentStore | None = None,
        gateway: MultimodalModelGateway | None = None,
        renderer: PageRenderer | None = None,
    ) -> None:
        self.store = store or AttachmentStore()
        self.gateway = gateway
        self.renderer = renderer or PdfPageRenderer()

    def analyze(
        self,
        *,
        refs: list[Any],
        question: str,
        page_numbers: list[int] | None = None,
    ) -> VisualAnalysisResult:
        result = VisualAnalysisResult()
        if not bool(getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_ENABLED", False)):
            return result
        if not bool(getattr(src.settings, "CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED", False)):
            result.degraded_reasons.append("visual_remote_disabled")
            return result
        gateway = self.gateway or default_multimodal_gateway()
        if gateway is None:
            result.degraded_reasons.append("visual_unavailable")
            return result

        requested = _normalize_pages(page_numbers)
        if not requested:
            requested = [1]
        max_pages = max(
            1,
            min(
                int(getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN", 3)),
                20,
            ),
        )
        max_image_bytes = max(
            1,
            int(getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES", 8 * 1024 * 1024)),
        )
        provider = str(getattr(gateway, "provider", "visual") or "visual")
        model = str(getattr(gateway, "model", "") or "")
        question_hash = hashlib.sha256(
            str(question or "").strip().encode("utf-8", "replace")
        ).hexdigest()
        attempted_pages = 0
        for ref in refs:
            if attempted_pages >= max_pages:
                self._note(result, "visual_page_limit")
                break
            if str(getattr(ref, "extension", "")).lower() != ".pdf":
                self._note(result, "visual_unsupported_type")
                continue
            asset = self.store.get_asset(str(getattr(ref, "asset_id", "") or ""))
            if asset is None:
                self._note(result, "visual_source_unavailable")
                continue
            page_count = _page_count(asset.manifest, asset.storage_key)
            if page_count <= 0:
                self._note(result, "visual_source_unavailable")
                continue
            try:
                source_path = resolve_storage_key(asset.storage_key)
            except Exception:
                self._note(result, "visual_source_unavailable")
                continue
            for page_number in requested:
                if attempted_pages >= max_pages:
                    self._note(result, "visual_page_limit")
                    break
                if page_number < 1 or page_number > page_count:
                    self._note(result, "visual_page_out_of_bounds")
                    continue
                # The limit applies to page attempts, including pages that
                # fail to render or fail at the provider.  A broken page must
                # not let one turn consume an unbounded number of requests.
                attempted_pages += 1
                try:
                    cached = self.store.get_visual_cache(
                        asset_id=asset.asset_id,
                        page_number=page_number,
                        provider=provider,
                        model=model,
                        question_hash=question_hash,
                    )
                    if cached and str(cached.get("content") or "").strip():
                        result.evidence.append(
                            self._evidence(
                                ref=ref,
                                page_number=page_number,
                                analysis=VisualAnalysis(
                                    text=str(cached["content"]),
                                    provider=provider,
                                    model=model,
                                    request_id=str(cached.get("request_id") or ""),
                                ),
                                content=str(cached["content"]),
                            )
                        )
                        continue
                    image_bytes = self.renderer.render_page(source_path, page_number)
                    if len(image_bytes) > max_image_bytes:
                        self._note(result, "visual_image_too_large")
                        continue
                    analysis = gateway.analyze(
                        question=str(question or ""),
                        image_bytes=image_bytes,
                        text_context=self._page_text(asset.asset_id, page_number),
                    )
                    text = str(getattr(analysis, "text", "") or "").strip()
                    if not text:
                        self._note(result, "visual_failed")
                        continue
                    result.evidence.append(
                        self._evidence(
                            ref=ref,
                            page_number=page_number,
                            analysis=analysis,
                            content=text,
                        )
                    )
                    try:
                        self.store.save_visual_cache(
                            asset_id=asset.asset_id,
                            page_number=page_number,
                            provider=provider,
                            model=model,
                            question_hash=question_hash,
                            content=text,
                            request_id=str(getattr(analysis, "request_id", "") or ""),
                        )
                    except Exception:
                        # A cache write must never turn a successful provider
                        # response into a degraded visual result.
                        pass
                except Exception:
                    # A failed page must not prevent other selected pages or
                    # the local lexical path from producing an answer.
                    self._note(result, "visual_failed")
        return result

    def _page_text(self, asset_id: str, page_number: int) -> str:
        chunks: list[str] = []
        for part in self.store.list_parts(asset_id):
            try:
                page = int((part.locator or {}).get("page") or 0)
            except (TypeError, ValueError):
                page = 0
            if page == page_number and str(part.text_content or "").strip():
                chunks.append(str(part.text_content).strip())
        return "\n".join(chunks)[:2400]

    @staticmethod
    def _note(result: VisualAnalysisResult, reason: str) -> None:
        if reason not in result.degraded_reasons:
            result.degraded_reasons.append(reason)

    @staticmethod
    def _evidence(*, ref: Any, page_number: int, analysis: VisualAnalysis, content: str) -> Evidence:
        provider = str(getattr(analysis, "provider", "") or "visual")
        model = str(getattr(analysis, "model", "") or "")
        return Evidence(
            id=f"att-visual-{uuid.uuid4().hex[:12]}",
            content=content,
            source_name=str(getattr(ref, "filename", "") or "attachment"),
            content_kind=PART_TYPE_VISUAL_EVIDENCE,
            processor_kind=f"local_attachment:visual:{provider}",
            score=1.0,
            locator={"page": page_number},
            metadata={
                "source_type": "chat_attachment",
                "attachment_id": str(getattr(ref, "attachment_id", "") or ""),
                "asset_id": str(getattr(ref, "asset_id", "") or ""),
                "session_id": int(getattr(ref, "session_id", 0) or 0),
                "filename": str(getattr(ref, "filename", "") or ""),
                "provider": provider,
                "model": model,
                "request_id": str(getattr(analysis, "request_id", "") or ""),
                "backend": "remote_visual",
            },
        )


def _normalize_pages(page_numbers: list[int] | None) -> list[int]:
    pages: list[int] = []
    for value in page_numbers or []:
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page not in pages:
            pages.append(page)
    return pages


def _page_count(manifest: dict[str, Any], storage_key: str) -> int:
    try:
        count = int((manifest or {}).get("page_count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        return count
    try:
        from pypdf import PdfReader

        return len(PdfReader(resolve_storage_key(storage_key)).pages)
    except Exception:
        return 0
