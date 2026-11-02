"""Central, profile-aware chat-model factory.

Provider construction is intentionally kept here. Application modules may
select a bounded profile, but cannot construct provider clients or override
transport policy ad hoc.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import src.settings as settings

from src.core.llm_governor import PRIORITY_INTERACTIVE
from src.core.llm_headers import opencode_extra_headers
from src.core.model_context import resolve_model_context_window
from src.core.model_gateway import build_governed_model

_LOGGER = logging.getLogger(__name__)

_SETTINGS_GENERATION = 0
_PROFILE_NAMES = frozenset({
    "default",
    "external_conversation",
    "memory",
})


@dataclass(frozen=True)
class ChatModelConfig:
    profile: str
    provider: str
    model: str
    endpoint: str
    api_key: str
    max_tokens: int
    timeout: int
    temperature: float
    max_retries: int
    require_structured_tools: bool = False
    max_input_tokens: int = 0
    priority: str = PRIORITY_INTERACTIVE


def settings_generation() -> int:
    return _SETTINGS_GENERATION


def notify_settings_reloaded() -> None:
    global _SETTINGS_GENERATION
    _SETTINGS_GENERATION += 1
    create_chat_model.cache_clear()
    _build_cached_chat_model.cache_clear()


def invalidate_chat_model_cache() -> None:
    create_chat_model.cache_clear()
    _build_cached_chat_model.cache_clear()


def normalize_provider(value: Any) -> str:
    """Normalize settings.Enum and explicit string provider values."""

    raw = getattr(value, "value", value)
    provider = str(raw or "").strip().casefold()
    if provider == "openai":
        return "custom"
    if provider not in {"ollama", "custom"}:
        raise ValueError(f"Unsupported chat model provider: {provider or '<empty>'}")
    return provider


def resolve_chat_model_config(
    *,
    profile: str = "default",
    provider: Any = "",
    model: str = "",
    settings_module: Any | None = None,
    priority: str = PRIORITY_INTERACTIVE,
) -> ChatModelConfig:
    """Resolve one allowlisted profile from the application settings."""

    settings_source = settings_module or settings

    profile_name = str(profile or "default").strip().casefold()
    if profile_name not in _PROFILE_NAMES:
        raise ValueError(
            f"Unknown chat model profile: {profile_name!r}; expected one of {sorted(_PROFILE_NAMES)}"
        )

    memory_provider = getattr(settings_source, "MEMORY_MODEL_PROVIDER", "") or ""
    if profile_name == "memory" and not provider:
        provider_value = memory_provider or getattr(settings_source, "AGENT_LLM_PROVIDER", "ollama")
    else:
        provider_value = provider or getattr(settings_source, "AGENT_LLM_PROVIDER", "ollama")
    normalized_provider = normalize_provider(provider_value)

    if profile_name == "memory":
        configured_model = str(getattr(settings_source, "MEMORY_MODEL", "") or "").strip()
        configured_endpoint = str(getattr(settings_source, "MEMORY_MODEL_BASE_URL", "") or "").strip()
        configured_api_key = str(getattr(settings_source, "MEMORY_MODEL_API_KEY", "") or "").strip()
        timeout = int(getattr(settings_source, "MEMORY_REFLECTION_TIMEOUT_SECONDS", 120))
        require_structured_tools = bool(
            getattr(settings_source, "MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS", True)
        )
    else:
        configured_model = ""
        configured_endpoint = ""
        configured_api_key = ""
        timeout = int(getattr(settings_source, "AGENT_TIMEOUT_SECONDS", 120))
        require_structured_tools = False

    if normalized_provider == "ollama":
        resolved_model = str(
            model or configured_model or getattr(settings_source, "AGENT_OLLAMA_MODEL", "")
        ).strip()
        endpoint = str(
            configured_endpoint or getattr(settings_source, "AGENT_OLLAMA_BASE_URL", "")
        ).strip()
        api_key = ""
        default_retries = 0
    else:
        resolved_model = str(
            model or configured_model or getattr(settings_source, "AGENT_CUSTOM_MODEL", "")
        ).strip()
        endpoint = str(
            configured_endpoint or getattr(settings_source, "AGENT_CUSTOM_BASE_URL", "")
        ).strip()
        api_key = str(
            configured_api_key or getattr(settings_source, "AGENT_CUSTOM_API_KEY", "")
        ).strip()
        default_retries = int(getattr(settings_source, "AGENT_RATE_LIMIT_MAX_RETRIES", 4))

    if profile_name == "external_conversation":
        outer_timeout = int(
            getattr(settings_source, "EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS", 60)
        )
        timeout = max(1, outer_timeout - 5)
        default_retries = 0

    if not resolved_model:
        raise ValueError(f"Chat model is not configured for provider {normalized_provider}")

    configured_window = max(
        0, int(getattr(settings_source, "AGENT_MODEL_MAX_INPUT_TOKENS", 0) or 0)
    )
    context = resolve_model_context_window(
        normalized_provider,
        resolved_model,
        configured_window,
    )
    if context.capped:
        _LOGGER.warning(
            "Configured AGENT_MODEL_MAX_INPUT_TOKENS=%s exceeds the known %s window "
            "for %s/%s; using %s to keep compaction proactive",
            configured_window,
            context.catalog_window,
            normalized_provider,
            resolved_model,
            context.window,
        )

    return ChatModelConfig(
        profile=profile_name,
        provider=normalized_provider,
        model=resolved_model,
        endpoint=endpoint,
        api_key=api_key,
        max_tokens=int(getattr(settings_source, "AGENT_CUSTOM_MAX_TOKENS", 4096)),
        timeout=max(1, int(timeout)),
        temperature=float(getattr(settings_source, "AGENT_TEMPERATURE", 0.2)),
        max_retries=max(0, int(default_retries)),
        require_structured_tools=require_structured_tools,
        max_input_tokens=context.window,
        priority=str(priority or PRIORITY_INTERACTIVE),
    )


@lru_cache(maxsize=32)
def create_chat_model(
    provider: Any = "",
    model: str = "",
    profile: str = "default",
    priority: str = PRIORITY_INTERACTIVE,
) -> object:
    """Build and cache a LangChain chat model for an allowlisted profile.

    ``priority`` selects the governor admission class (interactive keeps
    reserved capacity; batch is capped separately) and is part of the cache key.
    """

    config = resolve_chat_model_config(
        profile=profile,
        provider=provider,
        model=model,
        priority=priority,
    )
    return _build_cached_chat_model(_SETTINGS_GENERATION, config)


def create_chat_model_for_settings(
    settings_module: Any,
    provider: Any = "",
    model: str = "",
    profile: str = "default",
    priority: str = PRIORITY_INTERACTIVE,
) -> object:
    """Build a model from an injected settings object.

    Production code uses :func:`create_chat_model`; this narrow variant keeps
    isolated worker/test settings injectable without moving provider
    construction back into a domain module.
    """
    config = resolve_chat_model_config(
        profile=profile,
        provider=provider,
        model=model,
        settings_module=settings_module,
        priority=priority,
    )
    return _build_cached_chat_model(_SETTINGS_GENERATION, config)


@lru_cache(maxsize=32)
def _build_cached_chat_model(generation: int, config: ChatModelConfig) -> object:
    del generation  # generation is intentionally part of the cache key.
    if config.provider == "ollama":
        # ChatOllama has no max_retries/timeout fields. The sync/async httpx
        # clients enforce the bounded request timeout; transport retry is
        # intentionally disabled for Ollama by the profile contract.
        model = build_governed_model(
            provider="ollama",
            model=config.model,
            base_url=config.endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
            priority=config.priority,
        )
        _apply_model_profile(model, config.max_input_tokens)
        return model

    model = build_governed_model(
        provider="custom",
        model=config.model,
        base_url=config.endpoint,
        api_key=config.api_key,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        max_retries=config.max_retries,
        timeout=config.timeout,
        default_headers=opencode_extra_headers(config.endpoint) or None,
        priority=config.priority,
    )
    _apply_model_profile(model, config.max_input_tokens)

    return model


def _apply_model_profile(model: "object", max_input_tokens: int | None = None) -> None:
    """Declare the model context window for proactive summarization.

    OpenAI-compatible relays do not expose a model profile, so the configured
    ``AGENT_MODEL_MAX_INPUT_TOKENS`` value supplies it. An existing provider
    declaration wins. Passing ``None`` reads the live application settings;
    callers using an injected settings source pass the resolved value instead.
    Assignment is fail-soft for older langchain-core versions.
    """
    if max_input_tokens is None:
        max_input_tokens = getattr(settings, "AGENT_MODEL_MAX_INPUT_TOKENS", 0)
    try:
        max_input = int(max_input_tokens or 0)
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
