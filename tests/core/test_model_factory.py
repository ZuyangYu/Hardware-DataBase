from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.settings as settings
from src.core import model_factory


@pytest.fixture(autouse=True)
def clear_model_factory_cache():
    model_factory.invalidate_chat_model_cache()
    yield
    model_factory.invalidate_chat_model_cache()


def _configure_agent(monkeypatch, *, provider=settings.Provider.OLLAMA):
    monkeypatch.setattr(settings, "AGENT_LLM_PROVIDER", provider)
    monkeypatch.setattr(settings, "AGENT_OLLAMA_BASE_URL", "http://ollama.test")
    monkeypatch.setattr(settings, "AGENT_OLLAMA_MODEL", "qwen-test")
    monkeypatch.setattr(settings, "AGENT_CUSTOM_BASE_URL", "https://api.test/v1")
    monkeypatch.setattr(settings, "AGENT_CUSTOM_API_KEY", "agent-key")
    monkeypatch.setattr(settings, "AGENT_CUSTOM_MODEL", "custom-test")
    monkeypatch.setattr(settings, "AGENT_CUSTOM_MAX_TOKENS", 2048)
    monkeypatch.setattr(settings, "AGENT_TEMPERATURE", 0.2)
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_MAX_RETRIES", 4)


def _recording_init(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def init(model_name: str, **kwargs):
        calls.append((model_name, kwargs))
        return SimpleNamespace(model=model_name.split(":", 1)[1], provider=model_name.split(":", 1)[0])

    monkeypatch.setattr(model_factory, "init_chat_model", init)
    return calls


def test_enum_default_provider_builds_ollama_model(monkeypatch):
    _configure_agent(monkeypatch, provider=settings.Provider.OLLAMA)
    calls = _recording_init(monkeypatch)

    model_factory.create_chat_model()

    assert calls == [(
        "ollama:qwen-test",
        {
            "base_url": "http://ollama.test",
            "temperature": 0.2,
            "num_predict": 2048,
            "client_kwargs": {"timeout": 120},
        },
    )]


@pytest.mark.parametrize("provider", ["ollama", "custom"])
def test_explicit_provider_strings_are_normalized(monkeypatch, provider):
    _configure_agent(monkeypatch, provider=settings.Provider.OLLAMA)
    calls = _recording_init(monkeypatch)

    model_factory.create_chat_model(provider=provider)

    assert calls[0][0].startswith(f"{provider}:") if provider == "ollama" else calls[0][0].startswith("openai:")


def test_profiles_preserve_timeout_and_retry_contract(monkeypatch):
    _configure_agent(monkeypatch, provider=settings.Provider.CUSTOM)
    calls = _recording_init(monkeypatch)

    model_factory.create_chat_model(profile="authoring_auxiliary")
    model_factory.create_chat_model(profile="template_suggestion")
    model_factory.create_chat_model(profile="external_conversation")

    assert calls[0][1]["timeout"] == 20
    assert calls[0][1]["max_retries"] == 0
    assert calls[1][1]["timeout"] == 60
    assert calls[1][1]["max_retries"] == 4
    assert calls[2][1]["timeout"] < 60
    assert calls[2][1]["max_retries"] == 0


def test_memory_profile_prefers_memory_overrides(monkeypatch):
    _configure_agent(monkeypatch, provider=settings.Provider.OLLAMA)
    monkeypatch.setattr(settings, "MEMORY_MODEL_PROVIDER", "custom")
    monkeypatch.setattr(settings, "MEMORY_MODEL", "memory-test")
    monkeypatch.setattr(settings, "MEMORY_MODEL_BASE_URL", "https://memory.test/v1")
    monkeypatch.setattr(settings, "MEMORY_MODEL_API_KEY", "memory-key")
    monkeypatch.setattr(settings, "MEMORY_REFLECTION_TIMEOUT_SECONDS", 37)
    calls = _recording_init(monkeypatch)

    model_factory.create_chat_model(profile="memory")

    assert calls == [(
        "openai:memory-test",
        {
            "base_url": "https://memory.test/v1",
            "api_key": "memory-key",
            "temperature": 0.2,
            "max_tokens": 2048,
            "max_retries": 4,
            "timeout": 37,
        },
    )]


def test_memory_profile_falls_back_to_agent_settings(monkeypatch):
    _configure_agent(monkeypatch, provider=settings.Provider.OLLAMA)
    monkeypatch.setattr(settings, "MEMORY_MODEL_PROVIDER", "")
    monkeypatch.setattr(settings, "MEMORY_MODEL", "")
    monkeypatch.setattr(settings, "MEMORY_MODEL_BASE_URL", "")
    monkeypatch.setattr(settings, "MEMORY_MODEL_API_KEY", "")
    monkeypatch.setattr(settings, "MEMORY_REFLECTION_TIMEOUT_SECONDS", 41)
    calls = _recording_init(monkeypatch)

    config = model_factory.resolve_chat_model_config(profile="memory")
    model_factory.create_chat_model(profile="memory")

    assert config.provider == "ollama"
    assert config.model == "qwen-test"
    assert config.timeout == 41
    assert config.max_retries == 0
    assert calls[0][0] == "ollama:qwen-test"
    assert calls[0][1]["client_kwargs"] == {"timeout": 41}


def test_memory_profile_normalizes_enum_override(monkeypatch):
    _configure_agent(monkeypatch, provider=settings.Provider.OLLAMA)
    monkeypatch.setattr(settings, "MEMORY_MODEL_PROVIDER", settings.Provider.CUSTOM)
    monkeypatch.setattr(settings, "MEMORY_MODEL", "memory-custom")
    monkeypatch.setattr(settings, "AGENT_CUSTOM_BASE_URL", "https://custom.test/v1")
    monkeypatch.setattr(settings, "AGENT_CUSTOM_API_KEY", "key")
    calls = _recording_init(monkeypatch)

    model_factory.create_chat_model(profile="memory")

    assert calls[0][0] == "openai:memory-custom"


def test_settings_generation_rebuilds_model_after_reload(monkeypatch):
    _configure_agent(monkeypatch, provider=settings.Provider.OLLAMA)
    calls = _recording_init(monkeypatch)

    first = model_factory.create_chat_model()
    monkeypatch.setattr(settings, "AGENT_OLLAMA_MODEL", "qwen-reloaded")
    model_factory.notify_settings_reloaded()
    second = model_factory.create_chat_model()

    assert first is not second
    assert [call[0] for call in calls] == ["ollama:qwen-test", "ollama:qwen-reloaded"]


def test_memory_profile_can_use_an_injected_settings_source(monkeypatch):
    injected = SimpleNamespace(
        MEMORY_MODEL_PROVIDER="custom",
        MEMORY_MODEL="memory-test",
        MEMORY_MODEL_BASE_URL="https://memory.test/v1",
        MEMORY_MODEL_API_KEY="memory-key",
        MEMORY_REFLECTION_TIMEOUT_SECONDS=31,
        MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS=True,
        AGENT_LLM_PROVIDER="ollama",
        AGENT_OLLAMA_MODEL="fallback-ollama",
        AGENT_OLLAMA_BASE_URL="http://ollama.test",
        AGENT_CUSTOM_MODEL="fallback-custom",
        AGENT_CUSTOM_BASE_URL="https://api.test/v1",
        AGENT_CUSTOM_API_KEY="fallback-key",
        AGENT_CUSTOM_MAX_TOKENS=1024,
        AGENT_TEMPERATURE=0.1,
        AGENT_RATE_LIMIT_MAX_RETRIES=3,
    )
    calls = _recording_init(monkeypatch)

    model_factory.create_chat_model_for_settings(injected, profile="memory")

    assert calls == [(
        "openai:memory-test",
        {
            "base_url": "https://memory.test/v1",
            "api_key": "memory-key",
            "temperature": 0.1,
            "max_tokens": 1024,
            "max_retries": 3,
            "timeout": 31,
        },
    )]
