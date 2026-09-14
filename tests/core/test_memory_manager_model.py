from __future__ import annotations

from types import SimpleNamespace

import src.settings as application_settings
from src.memory import manager as memory_manager


def test_memory_model_uses_memory_profile_for_live_settings(monkeypatch):
    expected = object()
    calls: list[dict] = []

    def fake_factory(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(memory_manager, "create_chat_model", fake_factory)

    assert memory_manager._memory_model(application_settings) is expected
    assert calls == [{"profile": "memory"}]


def test_memory_model_keeps_injected_settings_inside_factory(monkeypatch):
    expected = object()
    injected = SimpleNamespace()
    calls: list[tuple[object, dict]] = []

    def fake_factory(settings, **kwargs):
        calls.append((settings, kwargs))
        return expected

    monkeypatch.setattr(memory_manager, "create_chat_model_for_settings", fake_factory)

    assert memory_manager._memory_model(injected) is expected
    assert calls == [(injected, {"profile": "memory"})]
