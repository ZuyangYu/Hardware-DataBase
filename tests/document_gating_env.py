"""Shared env-isolation guard for governed document authoring tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def pin_deterministic_document_gating(monkeypatch):
    """Isolate harness tests from deployment .env toggles.

    Local environments may enable DOCUMENT_AUTO_PUBLISH_VERIFIED so the
    deterministic harness auto-releases instead of stopping at
    ``review_candidate``.  These tests exercise the governed review flow,
    so gating switches are pinned to their safe defaults regardless of the
    developer's environment.
    """
    import src.settings as app_settings

    monkeypatch.setattr(app_settings, "DOCUMENT_AUTO_PUBLISH_VERIFIED", False)
    monkeypatch.setattr(app_settings, "DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES", False)
    monkeypatch.setattr(app_settings, "DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS", False)
