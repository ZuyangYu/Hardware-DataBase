"""Evidence providers shared by the document authoring flow.

The document harness consumes one evidence shape regardless of whether a
claim came from the selected knowledge base or from chat attachments.  The
providers in this module are deliberately scope-aware: callers may pass a
broader set of references, but a provider only reads the source class and
frozen references it owns.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, Protocol

from src.agents.schemas import Evidence
from src.attachments.evidence import evidence_from_chunk
from src.attachments.models import SOURCE_SCOPES, scope_allows_attachments
from src.attachments.retrieval import AttachmentRetrievalService
from src.pipelines.document_rag.schemas import RequestContext


class DocumentEvidenceProvider(Protocol):
    """Retrieve evidence for one bounded document-authoring query."""

    def retrieve(
        self,
        *,
        query: str,
        source_scope: str,
        attachment_ids: list[str],
        kb_name: str,
        ctx: RequestContext,
    ) -> list[Evidence]:
        """Return only evidence permitted by ``source_scope``."""


def _validate_source_scope(source_scope: str) -> str:
    normalized = str(source_scope or "auto").strip()
    if normalized not in SOURCE_SCOPES:
        raise ValueError(f"unsupported document evidence source scope: {normalized}")
    return normalized


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_ref_object(ref: Any) -> Any:
    """Adapt a persisted dict snapshot to the ref shape used by adapters."""
    if isinstance(ref, Mapping):
        return SimpleNamespace(**dict(ref))
    return ref


def _stable_evidence_id(raw: Any, *, source_name: str, content: str) -> str:
    raw_id = str(
        _value(raw, "id", "")
        or _value(raw, "chunk_id", "")
        or _value(raw, "document_id", "")
    ).strip()
    if raw_id:
        return raw_id
    digest = hashlib.sha256(f"{source_name}\n{content}".encode("utf-8")).hexdigest()
    return f"kb-{digest[:20]}"


def _knowledge_base_evidence(raw: Any, *, kb_name: str) -> Evidence:
    metadata = dict(_value(raw, "metadata", {}) or {})
    content = str(_value(raw, "content", "") or _value(raw, "text", "") or "")
    source_name = str(
        _value(raw, "source_name", "")
        or _value(raw, "document_name", "")
        or metadata.get("source_name", "")
        or "knowledge-base"
    )
    backend = str(_value(raw, "backend", "") or "knowledge_base")
    retriever = str(_value(raw, "retriever", "") or "")
    metadata.update(
        {
            "source_type": "knowledge_base",
            "knowledge_base_name": kb_name,
            "backend": backend,
            **({"retriever": retriever} if retriever else {}),
        }
    )
    locator = dict(_value(raw, "locator", {}) or {})
    if not locator and isinstance(metadata.get("locator"), Mapping):
        locator = dict(metadata["locator"])
    return Evidence(
        id=_stable_evidence_id(raw, source_name=source_name, content=content),
        content=content,
        source_name=source_name,
        content_kind=str(
            _value(raw, "content_kind", "")
            or metadata.get("content_kind", "")
            or "document"
        ),
        processor_kind=str(
            _value(raw, "processor_kind", "")
            or retriever
            or backend
            or "knowledge_base"
        ),
        score=float(_value(raw, "score", 0.0) or 0.0),
        locator=locator,
        metadata=metadata,
    )


class KnowledgeBaseEvidenceProvider:
    """Scope-checked adapter around the existing RAG backend."""

    def __init__(
        self,
        backend: Any,
        *,
        top_k: int = 8,
        source_names: Sequence[str] | None = None,
        extra_filters: Mapping[str, Any] | None = None,
    ):
        self.backend = backend
        self.top_k = max(1, int(top_k))
        self.source_names = (
            None
            if source_names is None
            else tuple(dict.fromkeys(str(name) for name in source_names if str(name)))
        )
        self.extra_filters = dict(extra_filters or {})

    def retrieve(
        self,
        *,
        query: str,
        source_scope: str,
        attachment_ids: list[str],
        kb_name: str,
        ctx: RequestContext,
    ) -> list[Evidence]:
        scope = _validate_source_scope(source_scope)
        if scope == "attachment_only":
            return []
        if not kb_name:
            return []
        # ``None`` means the standalone adapter has no source restriction;
        # an explicit empty sequence is a frozen empty source set and must
        # never be widened into an unrestricted backend query.
        if self.source_names == ():
            return []
        if ctx is None or not ctx.has_kb_permission(kb_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        filters = {"source_names": list(self.source_names)} if self.source_names is not None else {}
        filters.update(self.extra_filters)
        raw_evidence = self.backend.retrieve(
            kb_name,
            str(query or ""),
            top_k=self.top_k,
            ctx=ctx,
            filters=filters,
        )
        return [
            _knowledge_base_evidence(item, kb_name=kb_name)
            for item in (raw_evidence or [])
        ]


class AttachmentEvidenceProvider:
    """Retrieve only assets named by the frozen attachment reference snapshot."""

    def __init__(
        self,
        retrieval: AttachmentRetrievalService,
        refs_snapshot: Sequence[Any] = (),
        *,
        top_k: int = 8,
        context_token_budget: int | None = None,
    ):
        self.retrieval = retrieval
        self.refs_snapshot = tuple(copy.deepcopy(list(refs_snapshot or ())))
        self.top_k = max(1, int(top_k))
        self.context_token_budget = context_token_budget

    def _selected_refs(self, attachment_ids: Sequence[str]) -> list[Any]:
        refs_by_id: dict[str, Any] = {}
        for ref in self.refs_snapshot:
            attachment_id = str(_value(ref, "attachment_id", "") or "").strip()
            if attachment_id and attachment_id not in refs_by_id:
                refs_by_id[attachment_id] = ref

        requested = [str(item).strip() for item in attachment_ids if str(item).strip()]
        if requested:
            refs = [refs_by_id[item] for item in dict.fromkeys(requested) if item in refs_by_id]
        else:
            refs = list(refs_by_id.values())
        return refs

    def retrieve(
        self,
        *,
        query: str,
        source_scope: str,
        attachment_ids: list[str],
        kb_name: str,
        ctx: RequestContext,
    ) -> list[Evidence]:
        scope = _validate_source_scope(source_scope)
        if scope == "knowledge_base_only":
            return []
        if not scope_allows_attachments(scope) and not (
            scope == "auto" and attachment_ids
        ):
            return []

        refs = self._selected_refs(attachment_ids)
        asset_to_ref: dict[str, Any] = {}
        asset_ids: list[str] = []
        for raw_ref in refs:
            asset_id = str(_value(raw_ref, "asset_id", "") or "").strip()
            if not asset_id or asset_id in asset_to_ref:
                continue
            asset_to_ref[asset_id] = _as_ref_object(raw_ref)
            asset_ids.append(asset_id)
        if not asset_ids:
            return []

        search_kwargs: dict[str, Any] = {
            "asset_ids": asset_ids,
            "limit": self.top_k,
        }
        if self.context_token_budget is not None:
            search_kwargs["context_token_budget"] = self.context_token_budget
        result = self.retrieval.search(str(query or ""), **search_kwargs)
        chunks = _value(result, "chunks", result if isinstance(result, list) else []) or []
        evidence: list[Evidence] = []
        for chunk in chunks:
            ref = asset_to_ref.get(str(_value(chunk, "asset_id", "") or "").strip())
            if ref is None:
                # A retrieval adapter must not be able to smuggle an asset
                # outside the frozen allow-list into the document flow.
                continue
            evidence.append(evidence_from_chunk(ref=ref, chunk=chunk))
        return evidence


class CompositeDocumentEvidenceProvider:
    """Merge scope-aware providers while retaining their declared order."""

    def __init__(self, providers: Sequence[DocumentEvidenceProvider]):
        self.providers = tuple(providers)

    def retrieve(
        self,
        *,
        query: str,
        source_scope: str,
        attachment_ids: list[str],
        kb_name: str,
        ctx: RequestContext,
    ) -> list[Evidence]:
        _validate_source_scope(source_scope)
        evidence: list[Evidence] = []
        for provider in self.providers:
            evidence.extend(
                provider.retrieve(
                    query=query,
                    source_scope=source_scope,
                    attachment_ids=attachment_ids,
                    kb_name=kb_name,
                    ctx=ctx,
                )
            )
        return evidence


__all__ = [
    "AttachmentEvidenceProvider",
    "CompositeDocumentEvidenceProvider",
    "DocumentEvidenceProvider",
    "KnowledgeBaseEvidenceProvider",
]
