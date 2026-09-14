"""Attachment candidate release governance.

A candidate never becomes official in place: a user requests review, an
authorized reviewer approves (creating a separate released copy) or rejects
with a reason, and every transition keeps the candidate downloadable.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

import src.settings
from src.agents.tools.attachment_tools import make_attachment_release
from src.agents.tools.runtime import ToolRuntime
from src.attachments.models import AttachmentNotFound
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
    # Review hooks write notifications into the control-plane DB.
    monkeypatch.setattr(src.settings, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
    return AttachmentService(store=store), store


def _candidate(service, *, session_id=7, user_id=11):
    parent = service.upload(
        session_id=session_id,
        user_id=user_id,
        filename="design.md",
        stream=io.BytesIO("标题\nADAS ICD\n保留内容\n".encode()),
    )
    return service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=session_id,
        user_id=user_id,
        before="ADAS ICD",
        after="ADAS Interface Control Document",
        client_request_id="edit-release-1",
    )


def _runtime(service, attachment_id, *, session_id=7, user_id=11):
    record = service.get_attachment(
        attachment_id=attachment_id, session_id=session_id, user_id=user_id
    )
    return ToolRuntime(
        kb_name="ADAS",
        ctx=None,
        chat_session_id=str(session_id),
        source_scope="attachment_only",
        attachment_refs=service.build_refs([record]),
        attachment_service=service,
        attachment_user_id=user_id,
        attachment_session_id=session_id,
    )


def test_release_request_moves_to_pending_and_is_idempotent(tmp_path, monkeypatch):
    service, store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)

    requested = service.request_attachment_release(
        revision_id=revision.revision_id,
        session_id=7,
        user_id=11,
        comment="请审核发布",
    )

    assert requested.release_state == "pending"
    assert requested.release_requested_by == 11
    assert requested.release_requested_at
    assert requested.release_comment == "请审核发布"

    repeated = service.request_attachment_release(
        revision_id=revision.revision_id,
        session_id=7,
        user_id=11,
        comment="重复申请",
    )
    assert repeated.release_state == "pending"
    assert repeated.release_comment == "请审核发布"

    persisted = store.get_attachment_revision(
        revision.revision_id, session_id=7, user_id=11
    )
    assert persisted.release_state == "pending"


def test_release_request_is_owner_scoped(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)

    with pytest.raises(AttachmentNotFound):
        service.request_attachment_release(
            revision_id=revision.revision_id, session_id=7, user_id=999
        )


def test_approve_creates_a_separate_released_copy(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)
    service.request_attachment_release(
        revision_id=revision.revision_id, session_id=7, user_id=11
    )

    reviewed = service.review_attachment_release(
        revision_id=revision.revision_id,
        session_id=7,
        reviewer_id=42,
        approve=True,
        comment="审核通过",
    )

    assert reviewed.release_state == "approved"
    assert reviewed.reviewed_by == 42
    assert reviewed.review_comment == "审核通过"
    assert reviewed.released_attachment_id
    assert reviewed.released_attachment_id != reviewed.child_attachment_id

    _candidate_record, candidate_bytes = service.read_source_bytes(
        attachment_id=reviewed.child_attachment_id, session_id=7, user_id=11
    )
    released_record, released_bytes = service.read_source_bytes(
        attachment_id=reviewed.released_attachment_id, session_id=7, user_id=11
    )
    # The released copy is a distinct attachment with identical content; the
    # candidate itself is never promoted in place.
    assert released_bytes == candidate_bytes
    assert "released" in released_record.filename
    assert released_record.attachment_id != reviewed.child_attachment_id


def test_reject_keeps_candidate_and_allows_a_new_request(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)
    service.request_attachment_release(
        revision_id=revision.revision_id, session_id=7, user_id=11
    )

    rejected = service.review_attachment_release(
        revision_id=revision.revision_id,
        session_id=7,
        reviewer_id=42,
        approve=False,
        comment="版式不符合规范",
    )

    assert rejected.release_state == "rejected"
    assert rejected.review_comment == "版式不符合规范"
    assert rejected.released_attachment_id is None
    _record, candidate_bytes = service.read_source_bytes(
        attachment_id=rejected.child_attachment_id, session_id=7, user_id=11
    )
    assert "ADAS Interface Control Document" in candidate_bytes.decode("utf-8")

    re_requested = service.request_attachment_release(
        revision_id=revision.revision_id, session_id=7, user_id=11
    )
    assert re_requested.release_state == "pending"
    approved = service.review_attachment_release(
        revision_id=revision.revision_id,
        session_id=7,
        reviewer_id=42,
        approve=True,
        comment="修改后通过",
    )
    assert approved.release_state == "approved"

    with pytest.raises(ValueError, match="not pending"):
        service.review_attachment_release(
            revision_id=revision.revision_id,
            session_id=7,
            reviewer_id=42,
            approve=True,
        )


def test_review_requires_a_pending_request(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)

    with pytest.raises(ValueError, match="not pending"):
        service.review_attachment_release(
            revision_id=revision.revision_id,
            session_id=7,
            reviewer_id=42,
            approve=True,
        )


def test_release_tool_resolves_candidate_and_emits_card(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)
    runtime = _runtime(service, revision.child_attachment_id)
    events: list[dict] = []
    runtime.on_event = events.append

    payload = json.loads(
        make_attachment_release(runtime)(
            attachment_id=revision.child_attachment_id,
            comment="请发布",
        )
    )

    assert payload["status"] == "completed"
    assert payload["revision_id"] == revision.revision_id
    assert payload["release_state"] == "pending"
    assert "等待授权审核人" in payload["message"]

    release_events = [event for event in events if event["type"] == "attachment_release"]
    assert release_events and release_events[0]["payload"]["release_state"] == "pending"
    cards = [event for event in events if event["type"] == "document_card"]
    assert cards and cards[0]["payload"]["card"]["release_state"] == "pending"


def test_attachment_list_surfaces_candidate_release_state(tmp_path, monkeypatch):
    from src.agents.tools.attachment_tools import make_attachment_list

    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)
    runtime = _runtime(service, revision.child_attachment_id)

    before = make_attachment_list(runtime)()
    assert "修订候选，发布状态: 未申请发布" in before

    service.request_attachment_release(
        revision_id=revision.revision_id, session_id=7, user_id=11
    )
    after = make_attachment_list(runtime)()
    assert "发布审核中" in after


def test_release_tool_blocks_when_no_candidate_exists(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = service.upload(
        session_id=7,
        user_id=11,
        filename="plain.md",
        stream=io.BytesIO(b"no revision yet"),
    )
    runtime = _runtime(service, parent.attachment_id)

    payload = json.loads(make_attachment_release(runtime)())

    assert payload["status"] == "blocked"
    assert payload["error_code"] == "attachment_revision_target_unresolved"


def test_pending_release_queue_filters_by_kb_write_permission(tmp_path, monkeypatch):
    from src.api.routes import attachments as attachment_routes

    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)
    service.request_attachment_release(
        revision_id=revision.revision_id, session_id=7, user_id=11
    )
    session = SimpleNamespace(kb_name="ADAS", title="完善 HSI 文档", user_id=11)

    monkeypatch.setattr(attachment_routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(attachment_routes, "_attachment_service", lambda: service)
    monkeypatch.setattr(
        attachment_routes,
        "_conv_service",
        lambda: SimpleNamespace(get_session_unscoped=lambda _sid: session),
    )

    granted = SimpleNamespace(
        has_kb_permission=lambda _kb, _perm: True,
    )
    monkeypatch.setattr(
        attachment_routes, "build_context_for_user", lambda *_args, **_kwargs: granted
    )
    items = attachment_routes.list_pending_attachment_releases(
        user=SimpleNamespace(id=42), auth=None
    )
    assert len(items) == 1
    assert items[0]["revision_id"] == revision.revision_id
    assert items[0]["kb_name"] == "ADAS"
    assert "替换为" in items[0]["change_summary"]
    assert items[0]["candidate_download_url"].endswith("/candidate-download")

    denied = SimpleNamespace(
        has_kb_permission=lambda _kb, _perm: False,
    )
    monkeypatch.setattr(
        attachment_routes, "build_context_for_user", lambda *_args, **_kwargs: denied
    )
    assert attachment_routes.list_pending_attachment_releases(
        user=SimpleNamespace(id=42), auth=None
    ) == []


def test_reviewer_downloads_candidate_with_kb_write_permission(tmp_path, monkeypatch):
    from src.api.routes import attachments as attachment_routes

    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)
    monkeypatch.setattr(attachment_routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(attachment_routes, "_attachment_service", lambda: service)
    monkeypatch.setattr(attachment_routes, "_reviewer_ctx", lambda *_args: None)

    response = attachment_routes.download_attachment_revision_candidate(
        session_id=7,
        revision_id=revision.revision_id,
        user=SimpleNamespace(id=42),
        auth=None,
    )

    assert response.status_code == 200
    assert b"ADAS Interface Control Document" in response.body
    assert "attachment" in response.headers["content-disposition"]


def test_release_routes_request_review_and_permission(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from src.api.routes import attachments as attachment_routes

    service, _store = _service(tmp_path, monkeypatch)
    revision = _candidate(service)
    real_reviewer_ctx = attachment_routes._reviewer_ctx
    monkeypatch.setattr(attachment_routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(attachment_routes, "_require_session", lambda *_args: None)
    monkeypatch.setattr(attachment_routes, "_attachment_service", lambda: service)

    requested = attachment_routes.request_attachment_release(
        session_id=7,
        revision_id=revision.revision_id,
        payload=attachment_routes.AttachmentReleaseRequestIn(comment="走审核"),
        user=SimpleNamespace(id=11),
        auth=None,
    )
    assert requested["release_state"] == "pending"
    assert "替换为" in requested["change_summary"]

    monkeypatch.setattr(attachment_routes, "_reviewer_ctx", lambda *_args: None)
    reviewed = attachment_routes.review_attachment_release(
        session_id=7,
        revision_id=revision.revision_id,
        payload=attachment_routes.AttachmentReleaseReviewIn(
            decision="approve", comment="ok"
        ),
        user=SimpleNamespace(id=42),
        auth=None,
    )
    assert reviewed["release_state"] == "approved"
    assert reviewed["released_attachment_id"]

    # A reviewer without KB write permission is rejected before the service.
    session = SimpleNamespace(kb_name="ADAS")
    monkeypatch.setattr(
        attachment_routes, "_conv_service", lambda: SimpleNamespace(
            get_session_unscoped=lambda _sid: session
        )
    )
    monkeypatch.setattr(
        attachment_routes,
        "build_context_for_user",
        lambda *_args, **_kwargs: SimpleNamespace(
            has_kb_permission=lambda *_args, **_kwargs: False
        ),
    )
    monkeypatch.setattr(
        attachment_routes, "reject_system_admin_kb_access", lambda *_args: None
    )
    with pytest.raises(HTTPException) as excinfo:
        real_reviewer_ctx(SimpleNamespace(id=42), None, session_id=7)
    assert excinfo.value.status_code == 403
