"""Hardware-tuned lexical retrieval over chat attachments (design §8).

Composition (first release, no dense retriever):

    ExactIdentifierRetriever   raw / normalized / prefix + refdes LIKE fallback
    SparseTextRetriever        unicode61 FTS5
    OptionalTrigramRetriever   CJK / substring >= 3 chars
        -> Reciprocal-Rank Fusion -> ContextPacker (token budget) -> Evidence

Retrieval is always scoped to an explicit allow-list of asset ids derived
from ACL-verified attachments; there is no global search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import src.settings
from src.attachments.index import AttachmentIndex, IndexedHit
from src.attachments.query_normalizer import (
    extract_identifiers,
    identifier_variants,
    normalize_identifier,
)


@dataclass
class RetrievedChunk:
    asset_id: str
    part_id: str
    ordinal: int
    part_type: str
    text_content: str
    locator: dict[str, Any]
    metadata: dict[str, Any]
    score: float
    backend: str


@dataclass
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)
    matched_backends: list[str] = field(default_factory=list)
    truncated_by_budget: bool = False


def _chars_for_tokens(tokens: int) -> int:
    return int(max(0, tokens) * 3.5)


def _reciprocal_rank_fusion(
    ranked: list[list[RetrievedChunk]], *, k: int = 60, limit: int
) -> list[RetrievedChunk]:
    """RRF over per-retriever rankings; stable, tuning-light, local."""
    scores: dict[str, float] = {}
    best: dict[str, RetrievedChunk] = {}
    for ranking in ranked:
        for position, chunk in enumerate(ranking):
            key = chunk.part_id or f"{chunk.asset_id}:{chunk.ordinal}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position + 1)
            if key not in best or chunk.score > best[key].score:
                best[key] = chunk
    ordered_keys = sorted(scores, key=lambda key: scores[key], reverse=True)
    fused: list[RetrievedChunk] = []
    for key in ordered_keys[:limit]:
        chunk = best[key]
        chunk.score = round(scores[key], 6)
        fused.append(chunk)
    return fused


def _chars_for_tokens(tokens: int) -> int:
    return int(max(0, tokens) * 3.5)


class AttachmentRetrievalService:
    """Fused lexical retrieval across authorized attachment assets."""

    def __init__(self, index: AttachmentIndex | None = None, dense_retriever=None):
        self.index = index or AttachmentIndex()
        self.dense_retriever = dense_retriever
        self._degraded: list[str] = []

    @property
    def degraded_reasons(self) -> list[str]:
        return list(self._degraded)

    def _note_degraded(self, reason: str) -> None:
        if reason not in self._degraded:
            self._degraded.append(reason)

    def search(
        self,
        query: str,
        *,
        asset_ids: list[str],
        limit: int = 8,
        context_token_budget: int | None = None,
    ) -> RetrievalResult:
        self._degraded = []
        query = str(query or "").strip()
        if not query or not asset_ids:
            return RetrievalResult(query=query)
        budget = context_token_budget
        if budget is None:
            budget = int(src.settings.CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS)
            # Keep attachment evidence bounded relative to the model context
            # even when the caller does not provide a per-turn budget. A
            # caller-supplied budget remains authoritative and is only capped
            # by the hard packing loop below.
            try:
                ratio = float(src.settings.CHAT_ATTACHMENT_CONTEXT_RATIO)
                model_context = int(getattr(src.settings, "AGENT_MODEL_MAX_INPUT_TOKENS", 0))
            except (TypeError, ValueError):
                ratio = 0.0
                model_context = 0
            if ratio > 0 and model_context > 0:
                budget = min(budget, max(1, int(model_context * ratio)))
        budget = max(1, int(budget))

        identifiers = extract_identifiers(query)
        exact_hits: list[RetrievedChunk] = []
        for identifier in identifiers[:6]:
            exact_hits.extend(
                self._exact_lookup(identifier, asset_ids=asset_ids, limit_per=limit)
            )

        sparse_hits = self._sparse_lookup(query, asset_ids=asset_ids, limit=limit)
        dense_hits: list[RetrievedChunk] = []
        retrieval_mode = str(
            getattr(src.settings, "CHAT_ATTACHMENT_RETRIEVAL_MODE", "hybrid")
        ).strip().lower()
        if bool(getattr(src.settings, "CHAT_ATTACHMENT_DENSE_ENABLED", False)) and retrieval_mode != "lexical":
            if self.dense_retriever is None:
                from src.attachments.dense import DenseAttachmentRetriever

                self.dense_retriever = DenseAttachmentRetriever()
            dense_hits = [
                self._to_chunk(hit)
                for hit in self.dense_retriever.search(
                    query, asset_ids=asset_ids, limit=limit
                )
            ]
            for reason in getattr(self.dense_retriever, "degraded_reasons", []):
                self._note_degraded(reason)

        fused = _reciprocal_rank_fusion(
            [ranking for ranking in (exact_hits, sparse_hits, dense_hits) if ranking],
            limit=max(limit, len(identifiers) * 2),
        )

        # Context packing: budget is a hard cap, not a target.
        packed: list[RetrievedChunk] = []
        used = 0
        truncated = False
        for chunk in fused:
            rough_tokens = max(1, len(chunk.text_content) * 2 // 7)
            if used + rough_tokens > budget:
                truncated = True
                break
            packed.append(chunk)
            used += rough_tokens

        backends: list[str] = []
        for chunk in packed:
            if chunk.backend not in backends:
                backends.append(chunk.backend)
        if not self.index.fts_enabled:
            self._note_degraded("fts5_unavailable")
        return RetrievalResult(
            query=query,
            chunks=packed,
            degraded_reasons=list(self._degraded),
            matched_backends=backends,
            truncated_by_budget=truncated,
        )

    # -- retrievers ---------------------------------------------------------

    def _exact_lookup(
        self, identifier: str, *, asset_ids: list[str], limit_per: int
    ) -> list[RetrievedChunk]:
        raw = identifier.strip()
        raw, normalized, prefix = identifier_variants(raw)
        hits: list[IndexedHit] = []
        # Try raw, separator-free, and prefix-normalized forms. The raw form
        # preserves exact refdes/network spelling while normalized matching
        # handles VDD_3V3 vs vdd-3v3 and prefix matching handles revisions.
        for variant in dict.fromkeys((raw, normalized, prefix)):
            if not variant:
                continue
            if len(normalize_identifier(variant)) >= 3:
                hits.extend(
                    self.index.trigram_search(
                        variant, asset_ids=asset_ids, limit=limit_per
                    )
                )
            hits.extend(
                self.index.exact_scan(variant, asset_ids=asset_ids, limit=limit_per)
            )
        if normalized:
            hits.extend(
                self.index.normalized_identifier_scan(
                    normalized,
                    asset_ids=asset_ids,
                    limit=limit_per,
                )
            )
        seen: set[str] = set()
        chunks: list[RetrievedChunk] = []
        for hit in hits:
            if hit.part_id in seen:
                continue
            seen.add(hit.part_id)
            chunks.append(self._to_chunk(hit))
        return chunks[:limit_per]

    def _sparse_lookup(
        self, query: str, *, asset_ids: list[str], limit: int
    ) -> list[RetrievedChunk]:
        if self.index.fts_enabled:
            hits = self.index.search(query, asset_ids=asset_ids, limit=limit)
            return [self._to_chunk(hit) for hit in hits]
        # FTS5 unavailable: bounded lexical scan of a few query terms instead
        # of failing the whole tool (design §8.4).
        chunks: list[RetrievedChunk] = []
        terms = [term for term in re.findall(r"\w{2,}", query)][:4]
        for term in terms:
            chunks.extend(
                self._to_chunk(hit)
                for hit in self.index.exact_scan(term, asset_ids=asset_ids, limit=limit)
            )
        return chunks[:limit]

    @staticmethod
    def _to_chunk(hit: IndexedHit) -> RetrievedChunk:
        return RetrievedChunk(
            asset_id=hit.asset_id,
            part_id=hit.part_id,
            ordinal=hit.ordinal,
            part_type=hit.part_type,
            text_content=hit.text_content,
            locator=dict(hit.locator or {}),
            metadata=dict(hit.metadata or {}),
            score=hit.score,
            backend=hit.backend,
        )
