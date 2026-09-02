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
from langchain.chat_models import init_chat_model


@lru_cache(maxsize=4)
def create_chat_model(
    provider: str = "",
    model: str = "",
) -> "object":
    """Build and cache a LangChain chat model from AGENT_* settings.

    Cached because model construction is cheap but repeated per-request
    construction adds latency to the first token. Settings live-reload
    (PUT /api/v1/config) changes the env, so callers that must observe fresh
    settings pass explicit overrides or call ``create_chat_model.cache_clear()``.
    """
    provider = (provider or str(settings.AGENT_LLM_PROVIDER)).lower()
    temperature = float(settings.AGENT_TEMPERATURE)
    max_retries = int(settings.AGENT_RATE_LIMIT_MAX_RETRIES)
    timeout = int(settings.AGENT_TIMEOUT_SECONDS)

    if provider == "ollama":
        # ChatOllama has no max_retries/timeout fields (langchain_ollama
        # silently drops them); reach the backend via the httpx client's
        # request timeout instead.
        model = init_chat_model(
            f"ollama:{model or settings.AGENT_OLLAMA_MODEL}",
            base_url=str(settings.AGENT_OLLAMA_BASE_URL),
            temperature=temperature,
            client_kwargs={"timeout": timeout},
        )
        _apply_model_profile(model)
        return model

    model = init_chat_model(
        f"openai:{model or settings.AGENT_CUSTOM_MODEL}",
        base_url=str(settings.AGENT_CUSTOM_BASE_URL) or None,
        api_key=str(settings.AGENT_CUSTOM_API_KEY),
        temperature=temperature,
        max_tokens=int(settings.AGENT_CUSTOM_MAX_TOKENS),
        max_retries=max_retries,
        timeout=timeout,
    )
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
