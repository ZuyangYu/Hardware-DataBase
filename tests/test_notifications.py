"""In-app notifications for release reviews and their read state."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.settings
from src.core.notifications import NotificationService


def _service(tmp_path, monkeypatch) -> NotificationService:
    monkeypatch.setattr(src.settings, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    return NotificationService(db_path=str(tmp_path / "auth.db"))


def test_create_list_and_unread_count(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    first = service.create(
        user_id=11,
        kind="attachment_release_approved",
        title="附件候选已通过正式发布",
        body="已生成正式文件",
        target_type="attachment_revision",
        target_id="attachment-revision-1",
        payload={"session_id": 7},
    )
    service.create(user_id=12, kind="other", title="其他用户")

    assert first is not None and first.to_dict()["read"] is False
    mine = service.list_for_user(user_id=11)
    assert [item.title for item in mine] == ["附件候选已通过正式发布"]
    assert service.unread_count(user_id=11) == 1

    assert service.mark_read(user_id=11, notification_id=first.id) is True
    assert service.mark_read(user_id=11, notification_id=first.id) is False
    assert service.unread_count(user_id=11) == 0
    # Another user's notification is never touched.
    assert service.mark_read(user_id=99, notification_id=first.id) is False


def test_mark_all_read_is_user_scoped(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.create(user_id=11, kind="a", title="一")
    service.create(user_id=11, kind="b", title="二")
    service.create(user_id=12, kind="a", title="三")

    assert service.mark_all_read(user_id=11) == 2
    assert service.unread_count(user_id=11) == 0
    assert service.unread_count(user_id=12) == 1


def test_release_review_notifies_the_requester(tmp_path, monkeypatch):
    import io

    from src.attachments.service import AttachmentService
    from src.attachments.store import AttachmentStore

    monkeypatch.setattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", True)
    monkeypatch.setattr(
        src.settings, "CHAT_ATTACHMENT_INDEX_DB_PATH", str(tmp_path / "att.db")
    )
    monkeypatch.setattr(
        src.settings, "CHAT_ATTACHMENT_STORAGE_DIR", str(tmp_path / "storage")
    )
    monkeypatch.setattr(src.settings, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
    service = AttachmentService(store=store)
    parent = service.upload(
        session_id=7, user_id=11, filename="design.md", stream=io.BytesIO(b"old\nkeep\n")
    )
    revision = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=7,
        user_id=11,
        before="old",
        after="new",
        client_request_id="n1",
    )
    service.request_attachment_release(
        revision_id=revision.revision_id, session_id=7, user_id=11
    )

    service.review_attachment_release(
        revision_id=revision.revision_id,
        session_id=7,
        reviewer_id=42,
        approve=False,
        comment="版式不符",
    )

    notifications = NotificationService(
        db_path=str(tmp_path / "auth.db")
    ).list_for_user(user_id=11)
    assert len(notifications) == 1
    assert notifications[0].kind == "attachment_release_rejected"
    assert "版式不符" in notifications[0].body
    assert notifications[0].target_id == revision.revision_id


def test_webhook_dispatch_is_optional_and_fail_soft(tmp_path, monkeypatch):
    from src.core import notifications as notifications_mod

    calls: list[tuple] = []
    monkeypatch.setattr(
        notifications_mod,
        "_post_webhook",
        lambda url, payload, token="", timeout=2.0: calls.append((url, payload, token)),
    )
    service = _service(tmp_path, monkeypatch)

    # Unconfigured: no outbound call.
    monkeypatch.setattr(src.settings, "NOTIFY_WEBHOOK_URL", "")
    service.create(user_id=11, kind="k", title="站内")
    assert calls == []

    # Configured: one outbound call carrying the audit payload.
    monkeypatch.setattr(src.settings, "NOTIFY_WEBHOOK_URL", "https://hook.test/x")
    monkeypatch.setattr(src.settings, "NOTIFY_WEBHOOK_TOKEN", "tok")
    service.create(user_id=11, kind="k2", title="外发", body="b")
    assert len(calls) == 1
    assert calls[0][0] == "https://hook.test/x"
    assert calls[0][2] == "tok"
    assert calls[0][1]["title"] == "外发"

    # Delivery failure must never break the in-app notification.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("webhook down")

    monkeypatch.setattr(notifications_mod, "_post_webhook", _boom)
    created = service.create(user_id=11, kind="k3", title="仍然入库")
    assert created is not None
    assert service.unread_count(user_id=11) == 3


def test_notification_routes_are_user_scoped(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from src.api.routes import notifications as notification_routes

    service = _service(tmp_path, monkeypatch)
    created = service.create(user_id=11, kind="a", title="通知")
    monkeypatch.setattr(notification_routes, "_service", lambda: service)

    listed = notification_routes.list_notifications(
        unread_only=True, limit=10, user=SimpleNamespace(id=11)
    )
    assert listed["unread_count"] == 1
    assert listed["items"][0]["notification_id"] == created.id

    notification_routes.mark_notification_read(
        notification_id=created.id, user=SimpleNamespace(id=11)
    )
    assert service.unread_count(user_id=11) == 0

    with pytest.raises(HTTPException) as excinfo:
        notification_routes.mark_notification_read(
            notification_id=created.id, user=SimpleNamespace(id=11)
        )
    assert excinfo.value.status_code == 404
