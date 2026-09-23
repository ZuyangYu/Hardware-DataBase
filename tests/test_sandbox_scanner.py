"""dev-l1 artifact scanner: registered scanner refs are resolved explicitly."""

from __future__ import annotations

import hashlib

import pytest

import src.settings as settings
from src.sandbox.artifact_broker import ArtifactBroker, ArtifactBrokerError
from src.sandbox.artifacts import basic_artifact_scanner, build_registered_scanner


def _broker(tmp_path, monkeypatch, *, scanner_ref: str) -> ArtifactBroker:
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_SCANNER_REF", scanner_ref)
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS", "txt,json")
    monkeypatch.setattr(settings, "SANDBOX_MAX_OUTPUT_BYTES", 1024)
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_STORAGE_DIR", str(tmp_path / "artifacts"))
    return ArtifactBroker(
        str(tmp_path / "artifacts"),
        str(tmp_path / "sandbox.db"),
        scanner=build_registered_scanner(),
    )


def _stage(broker: ArtifactBroker, content: bytes, filename: str):
    return broker.stage_output(
        task_id="task-1",
        owner_id="u1",
        department_id="d1",
        filename=filename,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_registered_builtin_scanner_is_selected(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_SCANNER_REF", "scanner://builtin/basic-v1")
    assert build_registered_scanner() is basic_artifact_scanner
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_SCANNER_REF", "")
    assert build_registered_scanner() is None
    # External scanner refs must be injected by the deployment, not guessed.
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_SCANNER_REF", "scanner://external/av")
    assert build_registered_scanner() is None


def test_builtin_scanner_accepts_allowed_text(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch, scanner_ref="scanner://builtin/basic-v1")
    staged = _stage(broker, b'{"ok": true}', "report.json")
    broker.scan(staged.artifact_id, principal_id="u1", department_id="d1", task_id="task-1")
    committed = broker.commit(staged.artifact_id, department_id="d1", task_id="task-1")
    assert committed.state == "committed"


def test_builtin_scanner_rejects_binary_masquerading_as_text(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch, scanner_ref="scanner://builtin/basic-v1")
    staged = _stage(broker, b"hello\x00world", "report.txt")
    with pytest.raises(ArtifactBrokerError) as exc:
        broker.scan(staged.artifact_id, principal_id="u1", department_id="d1", task_id="task-1")
    assert exc.value.code == "scan_rejected"


def test_registered_external_scanner_still_fails_closed(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch, scanner_ref="scanner://external/av")
    staged = _stage(broker, b"plain text", "notes.txt")
    with pytest.raises(ArtifactBrokerError) as exc:
        broker.scan(staged.artifact_id, principal_id="u1", department_id="d1", task_id="task-1")
    assert exc.value.code == "scanner_unavailable"
