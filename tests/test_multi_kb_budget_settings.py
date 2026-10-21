"""New chat byte limits inherit the legacy value and never rewrite it."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import src.settings as settings


@pytest.fixture(autouse=True)
def restore_settings():
    yield
    with patch("src.settings.load_dotenv"):
        settings.reload_settings()


def reload_with(monkeypatch, **values):
    for key in ("MULTI_KB_FUSION_MAX_BYTES", "MULTI_KB_TURN_EVIDENCE_MAX_BYTES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MULTI_KB_CONTEXT_MAX_TOKENS", "8000")
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with patch("src.settings.load_dotenv"):
        settings.reload_settings()


def test_new_byte_limits_inherit_legacy_value_when_unset(monkeypatch):
    reload_with(monkeypatch)
    assert settings.MULTI_KB_CONTEXT_MAX_TOKENS == 8000
    assert settings.MULTI_KB_FUSION_MAX_BYTES == 8000
    assert settings.MULTI_KB_TURN_EVIDENCE_MAX_BYTES == 8000


def test_explicit_new_limits_do_not_rewrite_legacy_or_each_other(monkeypatch):
    reload_with(monkeypatch, MULTI_KB_FUSION_MAX_BYTES="16000", MULTI_KB_TURN_EVIDENCE_MAX_BYTES="24000")
    assert settings.MULTI_KB_FUSION_MAX_BYTES == 16000
    assert settings.MULTI_KB_TURN_EVIDENCE_MAX_BYTES == 24000
    assert settings.MULTI_KB_CONTEXT_MAX_TOKENS == 8000


def test_blank_new_limit_returns_to_inheritance(monkeypatch):
    reload_with(monkeypatch, MULTI_KB_FUSION_MAX_BYTES="", MULTI_KB_TURN_EVIDENCE_MAX_BYTES=" ")
    assert settings.MULTI_KB_FUSION_MAX_BYTES == 8000
    assert settings.MULTI_KB_TURN_EVIDENCE_MAX_BYTES == 8000


@pytest.mark.parametrize("key", ["MULTI_KB_FUSION_MAX_BYTES", "MULTI_KB_TURN_EVIDENCE_MAX_BYTES"])
def test_blank_is_valid_but_zero_and_negative_are_rejected(key):
    settings.validate_settings_values({key: ""})
    for value in ("0", "-1"):
        with pytest.raises(ValueError):
            settings.validate_settings_values({key: value})


def test_default_values_keep_inheritance_sentinel_empty():
    assert settings.DEFAULT_VALUES["MULTI_KB_FUSION_MAX_BYTES"] == ""
    assert settings.DEFAULT_VALUES["MULTI_KB_TURN_EVIDENCE_MAX_BYTES"] == ""
    assert "MULTI_KB_FUSION_MAX_BYTES" in settings._INT_SETTING_KEYS


def test_config_surface_returns_blank_for_inherited_limits(monkeypatch):
    from src.api.routes.config import _current_config

    monkeypatch.delenv("MULTI_KB_FUSION_MAX_BYTES", raising=False)
    monkeypatch.delenv("MULTI_KB_TURN_EVIDENCE_MAX_BYTES", raising=False)
    config = _current_config()
    assert config["MULTI_KB_FUSION_MAX_BYTES"] == ""
    assert config["MULTI_KB_TURN_EVIDENCE_MAX_BYTES"] == ""
