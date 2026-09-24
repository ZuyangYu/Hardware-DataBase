"""Attachment Evidence construction (design §11.4).

Attachment and KB evidence share the single ``Evidence`` model; the
``source_type`` metadata is the only discriminator. No parallel evidence
schema is introduced.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.agents.schemas import Evidence


def evidence_from_chunk(
    *,
    ref: Any,
    chunk: Any,
) -> Evidence:
    """Build one Evidence row from a retrieval chunk + attachment ref."""
    locator = dict(chunk.locator or {})
    return Evidence(
        id=f"att-{uuid.uuid4().hex[:12]}",
        content=str(chunk.text_content or ""),
        source_name=str(getattr(ref, "filename", "") or "attachment"),
        content_kind=str(chunk.part_type or "text"),
        processor_kind=f"local_attachment:{getattr(chunk, 'backend', 'lexical')}",
        score=float(getattr(chunk, "score", 0.0) or 0.0),
        locator=locator,
        metadata={
            "source_type": "chat_attachment",
            "attachment_id": str(getattr(ref, "attachment_id", "") or ""),
            "asset_id": str(getattr(ref, "asset_id", "") or ""),
            "session_id": int(getattr(ref, "session_id", 0) or 0),
            "filename": str(getattr(ref, "filename", "") or ""),
            "backend": "local_attachment",
        },
    )


def evidence_from_part(
    *,
    ref: Any,
    part: Any,
    processor_kind: str = "local_attachment",
) -> Evidence:
    """Build Evidence directly from a stored part (attachment_read tool)."""
    return Evidence(
        id=f"att-{uuid.uuid4().hex[:12]}",
        content=str(part.text_content or ""),
        source_name=str(getattr(ref, "filename", "") or "attachment"),
        content_kind=str(part.part_type or "text"),
        processor_kind=processor_kind,
        score=1.0,
        locator=dict(part.locator or {}),
        metadata={
            "source_type": "chat_attachment",
            "attachment_id": str(getattr(ref, "attachment_id", "") or ""),
            "asset_id": str(getattr(ref, "asset_id", "") or ""),
            "session_id": int(getattr(ref, "session_id", 0) or 0),
            "filename": str(getattr(ref, "filename", "") or ""),
            "backend": "local_attachment",
            "ordinal": int(getattr(part, "ordinal", 0) or 0),
        },
    )
