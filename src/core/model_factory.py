"""Central chat-model factory built on LangChain's official ``init_chat_model``.

Provider mapping (config/settings.py is the source of truth):
- ``AGENT_LLM_PROVIDER=ollama`` -> ``ollama:{AGENT_OLLAMA_MODEL}`` via langchain-ollama
  (local deployment; Ollama also exposes an OpenAI-compatible endpoint but the
  native integration keeps keep_alive/options available).
- ``AGENT_LLM_PROVIDER=custom`` -> ``openai:{AGENT_CUSTOM_MODEL}`` with a custom
  ``base_url`` (covers OpenRouter / SiliconFlow / DeepSeek / vLLM / Ollama's
  OpenAI-compatible server — any OpenAI-compatible API).
"""

from __future__ import annotations

from functools import lru_cache

import src.settings as settings

from src.core.llm_governor import PRIORITY_INTERACTIVE
from src.core.model_gateway import build_chat_model


@lru_cache(maxsize=8)
def create_chat_model(
    provider: str = "",
    model: str = "",
    priority: str = PRIORITY_INTERACTIVE,
) -> "object":
    """Build and cache a governed LangChain chat model from AGENT_* settings.

    ``priority`` selects the admission class in the process-wide LLM governor
    (``interactive`` keeps reserved capacity; ``batch`` is capped separately).
    Construction and usage accounting live in ``src/core/model_gateway.py``.

    Cached because model construction is cheap but repeated per-request
    construction adds latency to the first token. Settings live-reload
    (PUT /api/v1/config) changes the env, so callers that must observe fresh
    settings pass explicit overrides or call ``create_chat_model.cache_clear()``.
    """
    model = build_chat_model(provider=provider, model=model, priority=priority)
    _apply_model_profile(model)
    return model


def _apply_model_profile(model: "object") -> None:
    """Declare the model's context window so deepagents' SummarizationMiddleware
    computes proactive compaction thresholds (85% trigger / keep 10%).

    OpenAI-compatible relays (OpenRouter/DeepSeek/SiliconFlow) don't expose a
    model profile, so AGENT_MODEL_MAX_INPUT_TOKENS supplies it. A profile the
    provider/registry already declared wins. Fail-soft: a setting or assignment
    failure must never break model construction.
    """
    try:
        max_input = int(settings.AGENT_MODEL_MAX_INPUT_TOKENS or 0)
    except (TypeError, ValueError):
        return
    if max_input <= 0:
        return
    existing = getattr(model, "profile", None)
    if isinstance(existing, dict) and existing.get("max_input_tokens"):
        return
    try:
        model.profile = {"max_input_tokens": max_input}
    except Exception:
        # 老版本 langchain-core 不允许该字段赋值时，压缩退回被动兜底。
        pass
