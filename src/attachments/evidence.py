"""Attachment Evidence construction (design §11.4).

Attachment and KB evidence share the single ``Evidence`` model; the
``source_type`` metadata is the only discriminator. No parallel evidence
schema is introduced.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.agents.schemas import Evidence
from src.services.evidence_sources import ATTACHMENT, content_hash, with_source_metadata


def evidence_from_chunk(
    *,
    ref: Any,
    chunk: Any,
) -> Evidence:
    """Build one Evidence row from a retrieval chunk + attachment ref."""
    locator = dict(chunk.locator or {})
    metadata = with_source_metadata(
        {
            "source_type": ATTACHMENT,
            "attachment_id": str(getattr(ref, "attachment_id", "") or ""),
            "asset_id": str(getattr(ref, "asset_id", "") or ""),
            "session_id": int(getattr(ref, "session_id", 0) or 0),
            "filename": str(getattr(ref, "filename", "") or ""),
            "backend": "local_attachment",
            "document_version": str(getattr(ref, "sha256", "") or "")[:16],
        },
        content=str(chunk.text_content or ""),
        locator=locator,
    )
    return Evidence(
        id=f"att-{uuid.uuid4().hex[:12]}",
        content=str(chunk.text_content or ""),
        source_name=str(getattr(ref, "filename", "") or "attachment"),
        content_kind=str(chunk.part_type or "text"),
        processor_kind=f"local_attachment:{getattr(chunk, 'backend', 'lexical')}",
        score=float(getattr(chunk, "score", 0.0) or 0.0),
        locator=locator,
        metadata=metadata,
    )


def evidence_from_part(
    *,
    ref: Any,
    part: Any,
    processor_kind: str = "local_attachment",
) -> Evidence:
    """Build Evidence directly from a stored part (attachment_read tool)."""
    locator = dict(part.locator or {})
    metadata = with_source_metadata(
        {
            "source_type": ATTACHMENT,
            "attachment_id": str(getattr(ref, "attachment_id", "") or ""),
            "asset_id": str(getattr(ref, "asset_id", "") or ""),
            "session_id": int(getattr(ref, "session_id", 0) or 0),
            "filename": str(getattr(ref, "filename", "") or ""),
            "backend": "local_attachment",
            "ordinal": int(getattr(part, "ordinal", 0) or 0),
            "document_version": str(getattr(ref, "sha256", "") or "")[:16],
        },
        content=str(part.text_content or ""),
        locator=locator,
    )
    return Evidence(
        id=f"att-{uuid.uuid4().hex[:12]}",
        content=str(part.text_content or ""),
        source_name=str(getattr(ref, "filename", "") or "attachment"),
        content_kind=str(part.part_type or "text"),
        processor_kind=processor_kind,
        score=1.0,
        locator=locator,
        metadata=metadata,
    )


def candidate_preview_from_chunk(*, ref: Any, chunk: Any) -> dict[str, Any]:
    """Return a non-admitted candidate preview with full attachment identity.

    Preview candidates never receive a citation number and are never appended
    to ``ToolRuntime.evidence``.  A later explicit ``attachment_search`` is the
    only path that can promote one to formal evidence.
    """

    locator = dict(getattr(chunk, "locator", None) or {})
    text = str(getattr(chunk, "text_content", "") or "")
    return {
        "candidate_id": f"att-candidate:{getattr(ref, 'attachment_id', '')}:{getattr(chunk, 'part_id', '') or getattr(chunk, 'ordinal', '')}",
        "source_type": ATTACHMENT,
        "source_id": str(getattr(ref, "attachment_id", "") or ""),
        "attachment_id": str(getattr(ref, "attachment_id", "") or ""),
        "asset_id": str(getattr(ref, "asset_id", "") or ""),
        "filename": str(getattr(ref, "filename", "") or ""),
        "content": text,
        "content_hash": str(getattr(chunk, "content_hash", "") or content_hash(text)),
        "locator": locator,
        "score": float(getattr(chunk, "score", 0.0) or 0.0),
        "estimated_render_bytes": len(text.encode("utf-8")) + 160,
        "metadata": {"evidence_role": "candidate", "backend": "local_attachment"},
    }
