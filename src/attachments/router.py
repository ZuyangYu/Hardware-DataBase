"""Extension -> processor routing for chat attachments (design §6).

Deliberately separate from ``src/pipelines/registry.py``: the KB ingestion
router keeps its ``extension -> one PipelineSpec`` invariant untouched, while
attachment routing adds session-scope behaviour (local parsing, no RAGFlow,
no KB archival).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.attachments.models import (
    REJECTED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    AttachmentAsset,
    AttachmentPart,
    AttachmentUnsupportedType,
)


@dataclass
class ParseOutcome:
    """Result of a deterministic local parse."""

    ok: bool
    parse_status: str  # ready | degraded | failed
    parts: list[AttachmentPart]
    manifest: dict[str, Any]
    error_code: str = ""
    error_message: str = ""
    degraded_reasons: list[str] | None = None

    def __post_init__(self) -> None:
        if self.degraded_reasons is None:
            self.degraded_reasons = []


class AttachmentProcessor(Protocol):
    """A deterministic, local parser for one attachment family."""

    kind: str

    def parse(self, asset: AttachmentAsset, source_path: str) -> ParseOutcome:
        ...


class AttachmentProcessorRouter:
    """Resolve which processor handles an attachment extension.

    The registry is frozen at construction: no runtime registration, no
    model-controlled routing. Unknown/rejected extensions fail closed.
    """

    def __init__(self, processors: dict[str, AttachmentProcessor] | None = None):
        # Normalized lowercase extension (with dot) -> processor.
        self._processors: dict[str, AttachmentProcessor] = {
            str(ext).lower(): proc for ext, proc in (processors or {}).items()
        }

    def supports(self, extension: str) -> bool:
        ext = str(extension or "").lower()
        if not ext.startswith("."):
            ext = f".{ext}" if ext else ""
        return ext in self._processors

    def resolve(self, extension: str) -> AttachmentProcessor:
        ext = str(extension or "").lower()
        if not ext.startswith("."):
            ext = f".{ext}" if ext else ""
        if ext in REJECTED_EXTENSIONS:
            raise AttachmentUnsupportedType(
                f".{ext.lstrip('.')} is not supported for chat attachments; "
                "convert to .xlsx first."
            )
        processor = self._processors.get(ext)
        if processor is None:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise AttachmentUnsupportedType(
                f"unsupported attachment type '{ext or 'unknown'}'; supported: {supported}"
            )
        return processor

    @property
    def supported_extensions(self) -> frozenset[str]:
        return frozenset(self._processors)
