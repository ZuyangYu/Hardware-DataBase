"""Optional multimodal inference boundary used by attachment visual analysis.

The gateway owns provider-specific HTTP details.  Callers pass a single
already-rendered page image, never a PDF path or a complete source file.
Configuration is deliberately independent from the memory embedding
capability; an unset ARK key means that no remote client is constructed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

import src.settings


@dataclass(frozen=True)
class VisualAnalysis:
    """Normalized result returned by a multimodal provider."""

    text: str
    provider: str
    model: str
    request_id: str = ""


class MultimodalModelGateway(Protocol):
    provider: str
    model: str

    def analyze(
        self,
        *,
        question: str,
        image_bytes: bytes,
        text_context: str = "",
    ) -> VisualAnalysis:
        """Analyze one selected page image."""


class VolcengineArkGateway:
    """Volcengine Ark Responses API adapter.

    The request uses the OpenAI-compatible Responses content shape accepted by
    Ark: a short text prompt plus one data URL for the selected PNG page.
    """

    provider = "volcengine_ark"

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
            float(
                timeout_seconds
                or getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS", 60)
            ),
        )

    def analyze(
        self,
        *,
        question: str,
        image_bytes: bytes,
        text_context: str = "",
    ) -> VisualAnalysis:
        if not image_bytes:
            raise ValueError("visual gateway requires a page image")
        prompt = str(question or "").strip() or "请描述该页面中的硬件结构、器件和网络连接。"
        context = str(text_context or "").strip()
        if context:
            prompt = f"{prompt}\n\n页面可提取文本（仅作辅助，不可替代图像）：\n{context[:2400]}"
        image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        response = httpx.post(
            f"{self.base_url}/responses",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": image_url},
                        ],
                    }
                ],
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        text = _response_text(payload)
        if not text:
            raise ValueError("visual response contains no output text")
        request_id = ""
        if isinstance(payload, dict):
            request_id = str(payload.get("id") or payload.get("request_id") or "")
        return VisualAnalysis(
            text=text,
            provider=self.provider,
            model=self.model,
            request_id=request_id,
        )


def _response_text(payload: Any) -> str:
    """Extract text from the common Ark/OpenAI Responses output shapes."""

    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    fragments: list[str] = []
    for item in output:
        if isinstance(item, str):
            fragments.append(item)
            continue
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            fragments.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, str):
                fragments.append(block)
            elif isinstance(block, dict):
                value = block.get("text")
                if isinstance(value, str):
                    fragments.append(value)
    return "\n".join(fragment.strip() for fragment in fragments if fragment.strip()).strip()


def default_multimodal_gateway() -> MultimodalModelGateway | None:
    """Build the optional Ark gateway only when both policy flags and creds exist."""

    if not bool(getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_ENABLED", False)):
        return None
    if not bool(getattr(src.settings, "CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED", False)):
        return None
    provider = str(
        getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_PROVIDER", "volcengine_ark")
    ).strip().lower()
    base_url = str(getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_BASE_URL", "")).strip()
    model = str(getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_MODEL", "")).strip()
    api_key = str(getattr(src.settings, "ARK_API_KEY", "")).strip()
    if provider not in {"", "volcengine_ark", "ark"} or not base_url or not model or not api_key:
        return None
    return VolcengineArkGateway(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=getattr(src.settings, "CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS", 60),
    )
