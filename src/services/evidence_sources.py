"""Canonical source identity for formal evidence and retrieval candidates.

Evidence rows flow through several adapters (RAGFlow documents, structured
spreadsheets/circuits, chat attachments, wiki pages and long-term memory).  The
runtime needs one server-owned vocabulary for those origins so an attachment
can never satisfy a knowledge-base obligation by accident.

``metadata["source_type"]`` remains backwards compatible with the historical
``chat_attachment`` value, but every new path writes one of the canonical
values below.  Detection is intentionally conservative: unknown metadata stays
unknown instead of being guessed into a source that could satisfy an
obligation.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

ATTACHMENT = "attachment"
KNOWLEDGE_BASE = "knowledge_base"
KNOWLEDGE_BASE_DOCUMENT = "knowledge_base_document"
KNOWLEDGE_BASE_STRUCTURED = "knowledge_base_structured"
CONVERSATION = "conversation"
WIKI = "wiki"
MEMORY = "memory"
CANONICAL_SOURCE_TYPES = (
    ATTACHMENT,
    KNOWLEDGE_BASE,
    KNOWLEDGE_BASE_DOCUMENT,
    KNOWLEDGE_BASE_STRUCTURED,
    CONVERSATION,
    WIKI,
    MEMORY,
)
KNOWLEDGE_SOURCE_TYPES = frozenset({
    KNOWLEDGE_BASE,
    KNOWLEDGE_BASE_DOCUMENT,
    KNOWLEDGE_BASE_STRUCTURED,
})

_SOURCE_ALIASES = {
    "chat_attachment": ATTACHMENT,
    "attachment_chunk": ATTACHMENT,
    "attachment_part": ATTACHMENT,
    "kb": KNOWLEDGE_BASE,
    "knowledgebase": KNOWLEDGE_BASE,
    "knowledge-base": KNOWLEDGE_BASE,
    "document": KNOWLEDGE_BASE_DOCUMENT,
    "ragflow": KNOWLEDGE_BASE_DOCUMENT,
    "knowledge_base_document": KNOWLEDGE_BASE_DOCUMENT,
    "knowledge-base-document": KNOWLEDGE_BASE_DOCUMENT,
    "structured": KNOWLEDGE_BASE_STRUCTURED,
    "knowledge_base_structured": KNOWLEDGE_BASE_STRUCTURED,
    "spreadsheet": KNOWLEDGE_BASE_STRUCTURED,
    "circuit": KNOWLEDGE_BASE_STRUCTURED,
    "external_conversation": CONVERSATION,
    "conversation_search": CONVERSATION,
    "wiki_search": WIKI,
    "memory_search": MEMORY,
}


def normalize_source_type(value: Any) -> str:
    """Return a canonical source type, or ``""`` for an unknown label."""

    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in CANONICAL_SOURCE_TYPES:
        return text
    return _SOURCE_ALIASES.get(text, "")


def source_type_for_processor(processor_kind: str) -> str:
    """Map a pipeline processor kind to its evidence origin."""

    kind = str(processor_kind or "").strip().lower()
    if kind in {"spreadsheet_table", "circuit_design"}:
        return KNOWLEDGE_BASE_STRUCTURED
    if kind in {"external_conversation", "conversation"}:
        return CONVERSATION
    if kind == "wiki":
        return WIKI
    return KNOWLEDGE_BASE_DOCUMENT


def source_type_for_metadata(metadata: Mapping[str, Any] | None) -> str:
    """Infer the server-owned source type from evidence metadata.

    Explicit ``source_type`` wins.  Legacy attachment metadata is recognized by
    its attachment identity.  Knowledge rows are only classified when they
    carry a knowledge identity (``kb_id`` or ``knowledge_source``); everything
    else stays unknown so it cannot satisfy a restricted obligation.
    """

    data = dict(metadata or {})
    explicit = normalize_source_type(data.get("source_type"))
    if explicit:
        return explicit
    if data.get("attachment_id") or data.get("asset_id"):
        return ATTACHMENT
    if data.get("source_type") == "memory":
        return MEMORY
    processor_kind = str(
        data.get("processor_kind")
        or data.get("content_kind")
        or ""
    )
    if data.get("kb_id") not in (None, "") or isinstance(data.get("knowledge_source"), Mapping):
        return source_type_for_processor(processor_kind)
    return ""


def source_matches(required: str, actual: str) -> bool:
    """Whether ``actual`` can satisfy an obligation for ``required``."""

    required = normalize_source_type(required)
    actual = normalize_source_type(actual)
    if not required:
        return True
    if not actual:
        return False
    if required == KNOWLEDGE_BASE:
        return actual in KNOWLEDGE_SOURCE_TYPES
    if required in KNOWLEDGE_SOURCE_TYPES and actual == KNOWLEDGE_BASE:
        # A generic knowledge source can only be accepted when the requirement
        # itself is generic.  Concrete document/structured obligations must be
        # matched by the concrete evidence type.
        return False
    return required == actual


def source_id_for_metadata(metadata: Mapping[str, Any] | None) -> str:
    """Stable source identity used by candidate pools and evidence fusion."""

    data = dict(metadata or {})
    source_type = source_type_for_metadata(data)
    if source_type == ATTACHMENT:
        return str(data.get("attachment_id") or data.get("asset_id") or "")
    if source_type in KNOWLEDGE_SOURCE_TYPES:
        kb_id = data.get("kb_id")
        record_id = data.get("local_record_id") or data.get("record_id")
        if kb_id not in (None, "") and record_id not in (None, ""):
            return f"kb:{kb_id}:record:{record_id}"
        if kb_id not in (None, ""):
            return f"kb:{kb_id}"
        return str(record_id or "")
    return str(data.get("source_id") or "")


def content_hash(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def with_source_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    source_type: str | None = None,
    source_id: str | None = None,
    content: str | None = None,
    document_version: str | None = None,
    locator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return metadata with canonical identity fields filled in.

    Existing values are preserved so a caller can never overwrite a server
    identity with a model-supplied one.  The helper is used at adapter
    boundaries, not by tools accepting model arguments.
    """

    data = dict(metadata or {})
    resolved_type = normalize_source_type(source_type) or source_type_for_metadata(data)
    if resolved_type:
        data.setdefault("source_type", resolved_type)
    resolved_id = str(source_id or data.get("source_id") or source_id_for_metadata(data))
    if resolved_id:
        data.setdefault("source_id", resolved_id)
    if content is not None:
        data.setdefault("content_hash", content_hash(content))
    if document_version is not None:
        data.setdefault("document_version", str(document_version))
    if locator:
        data.setdefault("locator", dict(locator))
    return data
