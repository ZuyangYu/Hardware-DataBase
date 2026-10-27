"""New source-aware settings keep safe defaults and validate byte units."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import src.settings as settings


@pytest.fixture(autouse=True)
def restore_settings():
    yield
    with patch("src.settings.load_dotenv"):
        settings.reload_settings()


def reload_with(monkeypatch, **values):
    for key in (
        "EVIDENCE_SOURCE_AWARE_ENABLED",
        "EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES",
        "EVIDENCE_SYSTEM_PROMPT_RESERVE_BYTES",
        "EVIDENCE_ANSWER_RESERVE_BYTES",
        "EVIDENCE_ATTACHMENT_MAX_BYTES",
        "EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES",
        "EVIDENCE_ATTACHMENT_REQUIRED_RESERVE_BYTES",
        "EVIDENCE_KNOWLEDGE_BASE_REQUIRED_RESERVE_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with patch("src.settings.load_dotenv"):
        settings.reload_settings()


def test_source_aware_defaults_are_safe(monkeypatch):
    reload_with(monkeypatch)
    assert settings.EVIDENCE_SOURCE_AWARE_ENABLED is False
    assert settings.EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES == 32000
    assert settings.EVIDENCE_ATTACHMENT_MAX_BYTES == 12000
    assert settings.EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES == 16000


def test_source_budget_env_can_enable_the_new_path(monkeypatch):
    reload_with(
        monkeypatch,
        EVIDENCE_SOURCE_AWARE_ENABLED="true",
        EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES="30000",
    )
    assert settings.EVIDENCE_SOURCE_AWARE_ENABLED is True
    assert settings.EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES == 30000


@pytest.mark.parametrize("key", [
    "EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES",
    "EVIDENCE_SYSTEM_PROMPT_RESERVE_BYTES",
    "EVIDENCE_ANSWER_RESERVE_BYTES",
    "EVIDENCE_ATTACHMENT_MAX_BYTES",
    "EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES",
])
def test_new_byte_settings_reject_zero_and_negative(key):
    for value in ("0", "-1"):
        with pytest.raises(ValueError):
            settings.validate_settings_values({key: value})


def test_config_surface_exposes_new_keys():
    from src.api.routes.config import _current_config

    config = _current_config()
    assert config["EVIDENCE_SOURCE_AWARE_ENABLED"] is False
    assert "EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES" in config
