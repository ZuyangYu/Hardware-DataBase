"""Dedicated optional embedding gateway for attachment dense retrieval."""

from __future__ import annotations

from typing import Protocol

import httpx

import src.settings


class EmbeddingGateway(Protocol):
    provider: str
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text in the same order."""


class OpenAICompatibleEmbeddingGateway:
    """Small OpenAI-compatible ``/embeddings`` adapter.

    It intentionally reads only the attachment embedding settings.  In
    particular, it never falls back to ``MEMORY_EMBEDDING_API_KEY``.
    """

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.api_key = str(api_key)
        self.model = str(model)
        self.timeout_seconds = max(
            1.0,
            float(timeout_seconds or src.settings.CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("embedding response length does not match input")
        try:
            indexes = [int(item["index"]) for item in data]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("embedding response contains invalid indexes") from exc
        if sorted(indexes) != list(range(len(texts))):
            raise ValueError("embedding response contains invalid indexes")
        try:
            ordered = [item for _, item in sorted(zip(indexes, data), key=lambda pair: pair[0])]
            vectors = [item["embedding"] for item in ordered]
            if any(not isinstance(vector, list) or not vector for vector in vectors):
                raise ValueError("embedding response contains an empty vector")
            return [[float(value) for value in vector] for vector in vectors]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("embedding response contains invalid vectors") from exc


def default_embedding_gateway() -> EmbeddingGateway | None:
    """Build a configured gateway, or return ``None`` without network I/O."""
    base_url = str(getattr(src.settings, "CHAT_ATTACHMENT_EMBEDDING_BASE_URL", "")).strip()
    api_key = str(getattr(src.settings, "CHAT_ATTACHMENT_EMBEDDING_API_KEY", "")).strip()
    model = str(getattr(src.settings, "CHAT_ATTACHMENT_EMBEDDING_MODEL", "")).strip()
    if not base_url or not api_key or not model:
        return None
    provider = str(
        getattr(src.settings, "CHAT_ATTACHMENT_EMBEDDING_PROVIDER", "openai_compatible")
    ).strip().lower()
    if provider not in {"", "openai_compatible", "openai"}:
        return None
    return OpenAICompatibleEmbeddingGateway(
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
