"""Maintenance CLI: document-authoring compatibility closure readiness."""
import json

import src.settings


def test_compatibility_readiness_reports_blocking_reasons(tmp_path, monkeypatch, capsys):
    from src.maintenance.cli import main

    monkeypatch.setattr(
        src.settings,
        "DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH",
        str(tmp_path / "compatibility.sqlite"),
    )
    monkeypatch.setattr(src.settings, "DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED", False)

    exit_code = main(["compatibility-readiness"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ready"] is False
    assert "two_release_observation_required" in payload["reasons"]
    assert payload["closure_flag_enabled"] is False
    assert payload["release_count"] == 0
    assert payload["legacy_markers"] == 0
    assert payload["event_counts"]["legacy_write"] == 0
    assert set(payload["event_counts"]) == {
        "legacy_write",
        "new_write",
        "legacy_direct_execution",
        "plan_backed_execution",
        "auto_confirmation_attempt",
    }
