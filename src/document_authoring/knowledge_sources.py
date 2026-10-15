"""Server-owned identities for frozen multi-knowledge-base authoring sources."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.services.knowledge_scope_models import AuthorizedKnowledgeSource


def _value(document: Any, metadata: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = metadata.get(name)
        if value not in (None, ""):
            return value
        if isinstance(document, Mapping):
            value = document.get(name)
            if value not in (None, ""):
                return value
        value = getattr(document, name, None)
        if value not in (None, ""):
            return value
    return default


def _record_id(document: Any, metadata: Mapping[str, Any]) -> int | str:
    raw = _value(document, metadata, "store_id", "local_record_id", "record_id", "id")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = str(raw or "").strip()
    return value


def build_frozen_source_entry(
    *,
    tenant_key: str,
    source: AuthorizedKnowledgeSource,
    document: Any,
    strict: bool = False,
) -> dict[str, Any]:
    """Build one immutable source identity from the local document record.

    ``list_file_infos`` returns both dataclass records and backend-specific
    objects.  Metadata is preferred because it carries the source stamps
    emitted by the controlled retrieval backend; named attributes keep older
    adapters compatible during migration.
    """

    metadata = dict(
        (document.get("metadata", {}) if isinstance(document, Mapping) else getattr(document, "metadata", {}))
        or {}
    )
    source_name = str(
        _value(document, metadata, "name", "document_name", "original_file_name")
        or ""
    ).strip()
    entry = {
        "tenant_key": str(tenant_key or "").strip(),
        "department_id": str(source.department_id),
        "kb_id": int(source.kb_id),
        "kb_name_snapshot": str(source.kb_name),
        "role": str(source.role),
        "local_record_id": _record_id(document, metadata),
        "processor_kind": str(
            _value(document, metadata, "processor_kind", default="document_text")
            or "document_text"
        ),
        "source_group": str(_value(document, metadata, "source_group") or ""),
        "source_name": source_name,
        "source_content_hash": str(
            _value(document, metadata, "source_content_hash", "content_hash") or ""
        ),
        "source_version_id": str(_value(document, metadata, "source_version_id") or ""),
        "revision": str(_value(document, metadata, "revision") or ""),
        "approval_status": str(_value(document, metadata, "approval_status") or ""),
        "processing_artifact_id": str(
            _value(document, metadata, "processing_artifact_id") or ""
        ),
        "processor_version": str(_value(document, metadata, "processor_version") or ""),
        "index_revision": int(_value(document, metadata, "index_revision", default=0) or 0),
        "dataset_id": str(_value(document, metadata, "dataset_id") or ""),
        "remote_document_id": str(
            _value(
                document,
                metadata,
                "remote_document_id",
                "ragflow_document_id",
                "table_document_id",
                "document_id",
            )
            or ""
        ),
        "locator": dict(_value(document, metadata, "locator", default={}) or {}),
    }
    if strict:
        missing = [
            key
            for key in (
                "tenant_key",
                "department_id",
                "local_record_id",
                "source_name",
                "source_content_hash",
                "processor_kind",
                "dataset_id",
                "remote_document_id",
            )
            if entry[key] in (None, "")
        ]
        if missing:
            raise ValueError("knowledge_source_snapshot_incomplete:" + ",".join(missing))
    return entry


__all__ = ["build_frozen_source_entry"]
