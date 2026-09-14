"""Parser-generation backfill for chat attachments.

Re-parsing only rebuilds derived data (DOCX heading paths, FTS rows); source
bytes are never touched and repeated runs are idempotent while a job is live.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import src.settings
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore


def _service(tmp_path, monkeypatch):
    monkeypatch.setattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", True)
    monkeypatch.setattr(
        src.settings,
        "CHAT_ATTACHMENT_INDEX_DB_PATH",
        str(tmp_path / "attachments.db"),
    )
    monkeypatch.setattr(
        src.settings,
        "CHAT_ATTACHMENT_STORAGE_DIR",
        str(tmp_path / "storage"),
    )
    store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
    return AttachmentService(store=store), store


def _docx_bytes() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("旧版解析文档")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _mark_stale(store, record, version: str = "local_doc_v1:local_document") -> None:
    store.update_asset_status(
        record.asset_id,
        parse_status="ready",
        parser_version=version,
        manifest={},
    )


def _complete_then_stale(service, store, record) -> None:
    """Simulate an asset parsed by the previous generation (no live job)."""

    from src.attachments.worker import AttachmentWorker

    AttachmentWorker(store=store).run_once()
    _mark_stale(store, record)


def test_stale_assets_are_listed_and_requeued_once(tmp_path, monkeypatch):
    service, store = _service(tmp_path, monkeypatch)
    record = service.upload(
        session_id=7, user_id=11, filename="hsi.docx", stream=io.BytesIO(_docx_bytes())
    )
    _complete_then_stale(service, store, record)

    stale = service.list_stale_assets(extensions=[".docx"])
    assert [asset.asset_id for asset in stale] == [record.asset_id]

    first = service.reparse_stale_assets(extensions=[".docx"])
    assert first["scanned"] == 1
    assert first["queued"] == 1
    assert store.get_asset(record.asset_id).parse_status == "queued"

    # A live job suppresses a second enqueue for the same stale asset.
    second = service.reparse_stale_assets(extensions=[".docx"])
    assert second["scanned"] == 1
    assert second["queued"] == 0


def test_current_generation_is_not_stale(tmp_path, monkeypatch):
    from src.attachments.models import PARSER_VERSION

    service, store = _service(tmp_path, monkeypatch)
    record = service.upload(
        session_id=7, user_id=11, filename="hsi.docx", stream=io.BytesIO(_docx_bytes())
    )
    _mark_stale(store, record, version=f"{PARSER_VERSION}:local_document")

    assert service.list_stale_assets() == []


def test_reparse_cli_dry_run_and_enqueue(tmp_path, monkeypatch, capsys):
    from src.maintenance.cli import main

    service, store = _service(tmp_path, monkeypatch)
    record = service.upload(
        session_id=7, user_id=11, filename="hsi.docx", stream=io.BytesIO(_docx_bytes())
    )
    _complete_then_stale(service, store, record)

    assert main(["reparse-attachments", "--stale", "--dry-run"]) == 0
    dry = capsys.readouterr().out
    assert record.asset_id in dry
    assert "local_doc_v1" in dry

    assert main(["reparse-attachments", "--stale", "--limit", "5"]) == 0
    queued = capsys.readouterr().out
    assert '"queued": 1' in queued


def test_reparse_route_skips_kbs_without_write_permission(tmp_path, monkeypatch):
    from src.api.routes import attachments as attachment_routes

    service, store = _service(tmp_path, monkeypatch)
    record = service.upload(
        session_id=7, user_id=11, filename="hsi.docx", stream=io.BytesIO(_docx_bytes())
    )
    _complete_then_stale(service, store, record)

    monkeypatch.setattr(attachment_routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(attachment_routes, "_attachment_service", lambda: service)
    session = SimpleNamespace(kb_name="ADAS")
    monkeypatch.setattr(
        attachment_routes,
        "_conv_service",
        lambda: SimpleNamespace(get_session_unscoped=lambda _sid: session),
    )

    denied = SimpleNamespace(has_kb_permission=lambda *_args: False)
    monkeypatch.setattr(
        attachment_routes, "build_context_for_user", lambda *_args, **_kwargs: denied
    )
    assert attachment_routes.reparse_stale_attachments(
        limit=10, extension=None, actor=SimpleNamespace(id=42), auth=None
    )["queued"] == 0

    granted = SimpleNamespace(has_kb_permission=lambda *_args: True)
    monkeypatch.setattr(
        attachment_routes, "build_context_for_user", lambda *_args, **_kwargs: granted
    )
    result = attachment_routes.reparse_stale_attachments(
        limit=10, extension=None, actor=SimpleNamespace(id=42), auth=None
    )
    assert result["queued"] == 1
    assert record.asset_id in result["queued_asset_ids"]
