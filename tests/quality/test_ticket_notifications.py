"""New support requests reach active administrators without duplicate intake alerts."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.settings as settings
from src.api.deps import current_user, get_auth_service
from src.api.routes import quality as routes
from src.core.auth import AuthUser
from src.core.notifications import NotificationService
from tests.quality.test_feedback import quality as quality_fixture  # noqa: F401


def setup_api(q, monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "")
    second = AuthUser(5, "support-b", "system_admin", True)
    inactive = AuthUser(6, "disabled-admin", "system_admin", False)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes._store] = lambda: q.store
    app.dependency_overrides[current_user] = lambda: q.owner
    app.dependency_overrides[get_auth_service] = lambda: SimpleNamespace(
        list_users=lambda: [q.owner, q.reviewer, q.other, q.admin, second, inactive])
    state = q.store.feedback_state(q.owner.id, q.turn.id)
    body = {"assistant_message_id": q.turn.assistant_message_id, "answer_revision": state["answer_revision"],
            "client_request_id": "intake-alert", "rating": "unhelpful", "comment": "Private issue details",
            "share_with_support": True, "support_contact": "private-contact@example.test"}
    return app, f"/api/v1/chat/turns/{q.turn.id}/feedback", body


def test_new_ticket_notifies_each_active_admin_once_and_no_employees(quality, monkeypatch):
    q = quality
    app, path, body = setup_api(q, monkeypatch)
    notifications = NotificationService()
    with TestClient(app) as client:
        first = client.post(path, json=body)
        assert first.status_code == 200
        ticket_id = first.json()["ticket_id"]
        for admin_id in (4, 5):
            rows = notifications.list_for_user(user_id=admin_id)
            assert len(rows) == 1
            assert rows[0].kind == "quality_ticket_created"
            assert rows[0].target_type == "quality_ticket" and rows[0].target_id == ticket_id
            assert "Private issue details" not in rows[0].body
            assert "private-contact" not in rows[0].body
        for user_id in (1, 2, 3, 6):
            assert notifications.list_for_user(user_id=user_id) == []
        assert client.post(path, json=body).json()["deduplicated"] is True
        assert client.post(path, json={**body, "client_request_id": "supplement", "comment": "More details"}).json()["ticket_id"] == ticket_id
        assert notifications.unread_count(user_id=4) == notifications.unread_count(user_id=5) == 1


def test_one_recipient_failure_does_not_block_other_admins_or_ticket_creation(quality, monkeypatch):
    q = quality
    app, path, body = setup_api(q, monkeypatch)
    create = NotificationService.create

    def fail_one(self, **values):
        if values["user_id"] == 4:
            raise OSError("simulated notification storage outage")
        return create(self, **values)

    monkeypatch.setattr(NotificationService, "create", fail_one)
    with TestClient(app) as client:
        result = client.post(path, json=body)
        assert result.status_code == 200
        assert NotificationService().unread_count(user_id=5) == 1
        assert q.store.feedback_state(q.owner.id, q.turn.id)["ticket_id"] == result.json()["ticket_id"]


def test_assignment_notifies_the_new_handler_and_the_requester(quality, monkeypatch):
    from src.api.routes.quality_tickets import _notify

    q = quality
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "")
    _notify(q.admin, {"ticket_id": "ticket-assigned", "owner_user_id": 1, "assignee_user_id": 5}, action="assign")
    notifications = NotificationService()
    assert notifications.unread_count(user_id=1) == 1
    assert notifications.unread_count(user_id=5) == 1
    assert notifications.unread_count(user_id=4) == 0
    assert notifications.list_for_user(user_id=5)[0].target_id == "ticket-assigned"
