"""Optional dense retrieval over ACL-scoped attachment parts."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import src.settings
from src.attachments.embedding import EmbeddingGateway, default_embedding_gateway
from src.attachments.index import IndexedHit
from src.attachments.store import AttachmentStore


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator


@dataclass
class DenseRetrievalState:
    degraded_reasons: list[str]


class DenseAttachmentRetriever:
    """Cache part vectors and rank only the explicitly supplied asset ids."""

    def __init__(
        self,
        *,
        store: AttachmentStore | None = None,
        gateway: EmbeddingGateway | None = None,
    ) -> None:
        self.store = store or AttachmentStore()
        self.gateway = gateway
        self._state = DenseRetrievalState(degraded_reasons=[])

    @property
    def degraded_reasons(self) -> list[str]:
        return list(self._state.degraded_reasons)

    def _note_degraded(self, reason: str) -> None:
        if reason not in self._state.degraded_reasons:
            self._state.degraded_reasons.append(reason)

    @staticmethod
    def _record_metric(status: str) -> None:
        try:
            from src.observability.metrics import record_attachment

            record_attachment("dense", status=status)
        except Exception:
            pass

    def search(self, query: str, *, asset_ids: list[str], limit: int = 8) -> list[IndexedHit]:
        self._state.degraded_reasons = []
        if not bool(getattr(src.settings, "CHAT_ATTACHMENT_DENSE_ENABLED", False)):
            self._record_metric("disabled")
            return []
        query = str(query or "").strip()
        normalized_assets = list(dict.fromkeys(str(item) for item in asset_ids if str(item)))
        if not query or not normalized_assets:
            self._record_metric("empty")
            return []
        gateway = self.gateway or default_embedding_gateway()
        if gateway is None:
            self._note_degraded("dense_unavailable")
            self._record_metric("unavailable")
            return []

        try:
            max_parts = max(
                1,
                min(int(getattr(src.settings, "CHAT_ATTACHMENT_DENSE_MAX_PARTS", 4000)), 10000),
            )
            parts = []
            for asset_id in normalized_assets:
                parts.extend(self.store.list_parts(asset_id))
                if len(parts) >= max_parts:
                    parts = parts[:max_parts]
                    break
            parts = [part for part in parts if str(part.text_content or "").strip()]
            if not parts:
                self._record_metric("empty")
                return []

            provider = str(getattr(gateway, "provider", "openai_compatible"))
            model = str(getattr(gateway, "model", ""))
            cached = self.store.list_embeddings(
                asset_ids=normalized_assets,
                provider=provider,
                model=model,
            )
            query_vectors = gateway.embed([query])
            if len(query_vectors) != 1:
                raise ValueError("embedding gateway returned no query vector")
            query_vector = [float(value) for value in query_vectors[0]]
            if not query_vector:
                raise ValueError("embedding gateway returned an empty query vector")

            missing = []
            vectors: dict[str, list[float]] = {}
            for part in parts:
                expected_hash = part.content_hash or _content_hash(part.text_content)
                cached_value = cached.get(part.part_id)
                if (
                    cached_value is not None
                    and cached_value[0] == expected_hash
                    and cached_value[1]
                ):
                    vectors[part.part_id] = cached_value[1]
                else:
                    missing.append((part, expected_hash))
            if missing:
                part_vectors = gateway.embed([part.text_content for part, _ in missing])
                if len(part_vectors) != len(missing):
                    raise ValueError("embedding gateway returned an invalid part vector count")
                to_save = []
                for (part, content_hash), vector in zip(missing, part_vectors, strict=True):
                    normalized_vector = [float(value) for value in vector]
                    if not normalized_vector:
                        raise ValueError("embedding gateway returned an empty part vector")
                    vectors[part.part_id] = normalized_vector
                    to_save.append((part, content_hash, normalized_vector))
                for asset_id in normalized_assets:
                    asset_embeddings = [
                        (part.part_id, content_hash, vector)
                        for part, content_hash, vector in to_save
                        if part.asset_id == asset_id
                    ]
                    if asset_embeddings:
                        self.store.save_embeddings(
                            asset_id=asset_id,
                            provider=provider,
                            model=model,
                            embeddings=asset_embeddings,
                        )

            hits = []
            for part in parts:
                vector = vectors.get(part.part_id)
                if vector is None:
                    continue
                hits.append(
                    IndexedHit(
                        asset_id=part.asset_id,
                        part_id=part.part_id,
                        ordinal=part.ordinal,
                        part_type=part.part_type,
                        text_content=part.text_content,
                        locator=dict(part.locator or {}),
                        metadata=dict(part.metadata or {}),
                        score=_cosine(query_vector, vector),
                        backend="dense",
                    )
                )
            hits.sort(key=lambda hit: (-hit.score, hit.ordinal, hit.part_id))
            self._record_metric("ok" if hits else "empty")
            return hits[: max(1, min(int(limit), 50))]
        except Exception:
            self._note_degraded("dense_failed")
            self._record_metric("failed")
            return []
