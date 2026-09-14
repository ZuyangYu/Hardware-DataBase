"""Auditable, immutable revisions of session attachments."""

from __future__ import annotations

import hashlib
import io
import json

import pytest

import src.settings
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore
from src.agents.tools.attachment_tools import build_attachment_tools, make_attachment_edit
from src.attachments.models import AttachmentRef
from src.agents.tools.runtime import ToolRuntime


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


def test_text_attachment_revision_creates_immutable_candidate_with_audit_lineage(
    tmp_path, monkeypatch
):
    service, store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="design.md",
        stream=io.BytesIO("标题\nADAS ICD\n保留内容\n".encode()),
    )

    assert callable(getattr(service, "create_attachment_revision", None))
    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        before="ADAS ICD",
        after="ADAS Interface Control Document",
        client_request_id="edit-1",
    )

    assert revision.status == "review_candidate"
    assert revision.parent_attachment_id == parent.attachment_id
    assert revision.child_attachment_id
    assert revision.parent_content_hash == parent.sha256
    assert revision.child_content_hash != parent.sha256

    _parent_record, parent_bytes = service.read_source_bytes(
        attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
    )
    child_record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id,
        session_id=7,
        user_id=11,
    )
    assert parent_bytes == "标题\nADAS ICD\n保留内容\n".encode()
    assert child_bytes == "标题\nADAS Interface Control Document\n保留内容\n".encode()
    assert child_record.sha256 == hashlib.sha256(child_bytes).hexdigest()

    persisted = store.get_attachment_revision(
        revision.revision_id,
        session_id=7,
        user_id=11,
    )
    assert persisted is not None
    assert persisted.child_attachment_id == revision.child_attachment_id


def test_text_attachment_revision_rejects_ambiguous_or_unchanged_replacements(
    tmp_path, monkeypatch
):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="notes.txt",
        stream=io.BytesIO(b"same\nsame\n"),
    )

    with pytest.raises(ValueError, match="exactly once"):
        service.create_attachment_revision(
            parent_attachment_id=parent.attachment_id,
            session_id=7,
            user_id=11,
            before="same",
            after="changed",
        )

    with pytest.raises(ValueError, match="must change"):
        service.create_attachment_revision(
            parent_attachment_id=parent.attachment_id,
            session_id=7,
            user_id=11,
            before="same",
            after="same",
        )


def test_attachment_edit_tool_returns_downloadable_candidate_and_rechecks_scope(
    tmp_path, monkeypatch
):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="design.md",
        stream=io.BytesIO(b"old value\nkeep me\n"),
    )
    runtime = ToolRuntime(
        kb_name="",
        ctx=None,
        chat_session_id="7",
        source_scope="attachment_only",
        attachment_refs=service.build_refs([parent]),
        attachment_service=service,
        attachment_user_id=11,
        attachment_session_id=7,
    )

    result = make_attachment_edit(runtime)(
        attachment_id=parent.attachment_id,
        before="old value",
        after="new value",
    )
    payload = json.loads(result)

    assert payload["status"] == "completed"
    assert payload["stage"] == "review_candidate"
    assert payload["parent_attachment_id"] == parent.attachment_id
    assert payload["child_attachment_id"]
    assert payload["download_url"].endswith(
        f"/conversations/7/attachments/{payload['child_attachment_id']}/download"
    )


def test_attachment_candidate_download_route_returns_authorized_child_bytes(
    tmp_path, monkeypatch
):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="design.md",
        stream=io.BytesIO(b"old value\nkeep me\n"),
    )
    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        before="old value",
        after="new value",
    )

    from types import SimpleNamespace
    from src.api.routes import attachments as attachment_routes

    monkeypatch.setattr(attachment_routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(attachment_routes, "_require_session", lambda *_args: None)
    monkeypatch.setattr(attachment_routes, "_attachment_service", lambda: service)

    response = attachment_routes.download_attachment(
        7,
        revision.child_attachment_id,
        SimpleNamespace(id=11),
        None,
    )

    assert response.status_code == 200
    assert response.body == b"new value\nkeep me\n"
    assert response.media_type == "text/markdown"
    assert "attachment" in response.headers["content-disposition"]
    assert "design.revision" in response.headers["content-disposition"]


def test_attachment_edit_is_not_mounted_when_document_writes_are_disabled():
    runtime = ToolRuntime(
        kb_name="",
        ctx=None,
        source_scope="attachment_only",
        attachment_refs=[
            AttachmentRef(
                attachment_id="att-1",
                asset_id="asset-1",
                session_id=7,
                filename="design.md",
                media_type="text/markdown",
                extension=".md",
                size_bytes=1,
                sha256="hash",
            )
        ],
        attachment_edit_enabled=False,
    )

    assert "attachment_edit" not in {
        tool.__name__ for tool in build_attachment_tools(runtime)
    }
