from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_user, get_auth_service
from src.api.routes import quality as routes
from tests.quality.test_feedback import quality as quality_fixture  # noqa: F401 - named shared fixture


def test_feedback_api_authorization_and_read_only_queries(quality):
    q = quality
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes._store] = lambda: q.store
    viewer = [q.owner]
    app.dependency_overrides[current_user] = lambda: viewer[0]
    with TestClient(app) as client:
        path = f"/api/v1/chat/turns/{q.turn.id}/feedback"
        state = client.get(path)
        assert state.status_code == 200
        before = state.json()
        with closing(q.store.connect()) as conn:
            initial_jobs = conn.execute("SELECT COUNT(*) FROM quality_outbox").fetchone()[0]
        for _ in range(3):
            assert client.get(path).json() == before
        with closing(q.store.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM quality_cases").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM quality_outbox").fetchone()[0] == initial_jobs
        body = {"assistant_message_id": q.turn.assistant_message_id, "answer_revision": before["answer_revision"],
                "client_request_id": "browser-feedback", "rating": "unhelpful", "reason_codes": ["factual_error"], "comment": "Private feedback"}
        event = client.post(path, json=body)
        assert event.status_code == 200
        assert client.post(path, json=body).json()["deduplicated"]
        assert client.post(path, json={**body, "answer_revision": "0" * 64}).status_code == 409
        assert client.post(path, json={**body, "reason_codes": ["made_up_reason"]}).status_code == 422
        case_path = f"/api/v1/quality/cases/{event.json()['case_id']}"
        viewer[0] = q.admin
        assert client.get(path).status_code == 403
        assert client.get(case_path).json()["content"] == {}
        viewer[0] = q.other
        assert client.get(case_path).status_code == 404
        assert client.get(path).status_code == 404
        assert client.get("/api/v1/quality/cases").json() == []


def test_share_and_review_api_enforce_scope_and_version(quality, monkeypatch):
    q = quality
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    viewer = [q.owner]
    app.dependency_overrides[routes._store] = lambda: q.store
    app.dependency_overrides[current_user] = lambda: viewer[0]
    app.dependency_overrides[get_auth_service] = lambda: SimpleNamespace(list_users=lambda: [q.owner, q.reviewer, q.admin, q.other])
    import src.api.context

    monkeypatch.setattr(src.api.context, "build_context_for_user", lambda *_args, **_kwargs: SimpleNamespace(has_kb_permission=lambda *_: True))
    actions = []
    monkeypatch.setattr(routes, "_audit", lambda action, *_: actions.append(action))
    with TestClient(app) as client:
        feedback_path = f"/api/v1/chat/turns/{q.turn.id}/feedback"
        state = client.get(feedback_path).json()
        event = client.post(feedback_path, json={"assistant_message_id": q.turn.assistant_message_id, "answer_revision": state["answer_revision"], "client_request_id": "feedback", "rating": "unhelpful"}).json()
        path = f"/api/v1/quality/cases/{event['case_id']}"
        version = client.get(path).json()["row_version"]
        body = {"row_version": version, "reviewer_user_ids": [q.reviewer.id], "fields": ["answer"], "evidence_indices": []}
        assert client.post(f"{path}/share", json={**body, "reviewer_user_ids": [q.admin.id]}).status_code == 403
        assert client.post(f"{path}/share", json=body).status_code == 200
        assert client.post(f"{path}/share", json=body).status_code == 409
        viewer[0] = q.reviewer
        shared = client.get(path).json()
        assert set(shared["content"]) == {"answer", "evidence"}
        assert client.post(f"{path}/share/revoke", json={"row_version": shared["row_version"]}).status_code == 403
        review = {"row_version": shared["row_version"], "client_request_id": "review", "judgment": "needs_information", "notes": "Need source"}
        assert client.post(f"{path}/reviews", json=review).status_code == 200
        viewer[0] = q.owner
        updated = client.get(path).json()
        assert client.post(f"{path}/share/revoke", json={"row_version": updated["row_version"]}).status_code == 200
        viewer[0] = q.reviewer
        assert client.get(path).json()["reviews"][0]["content"] == {}
        assert client.post(f"{path}/reviews", json=review).status_code == 403
        assert actions == ["quality.share", "quality.review", "quality.share_revoke"]


def test_admin_review_request_notifies_owner_and_owner_approves_scope(quality, monkeypatch):
    q = quality
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    viewer = [q.owner]
    app.dependency_overrides[routes._store] = lambda: q.store
    app.dependency_overrides[current_user] = lambda: viewer[0]
    app.dependency_overrides[get_auth_service] = lambda: SimpleNamespace(list_users=lambda: [q.owner, q.reviewer, q.admin, q.other])
    import src.api.context
    import src.core.notifications

    monkeypatch.setattr(src.api.context, "build_context_for_user", lambda *_args, **_kwargs: SimpleNamespace(has_kb_permission=lambda *_: True))
    notifications = []
    actions = []
    monkeypatch.setattr(src.core.notifications, "NotificationService", lambda: SimpleNamespace(create=lambda **kwargs: notifications.append(kwargs)))
    monkeypatch.setattr(routes, "_audit", lambda action, *_: actions.append(action))
    with TestClient(app) as client:
        feedback_path = f"/api/v1/chat/turns/{q.turn.id}/feedback"
        state = client.get(feedback_path).json()
        event = client.post(feedback_path, json={"assistant_message_id": q.turn.assistant_message_id, "answer_revision": state["answer_revision"], "client_request_id": "request-feedback", "rating": "unhelpful"}).json()
        case_path = f"/api/v1/quality/cases/{event['case_id']}"
        viewer[0] = q.admin
        admin_case = client.get(case_path).json()
        requested = client.post(f"{case_path}/review-requests", json={"row_version": admin_case["row_version"], "client_request_id": "admin-request", "message": "Please review"})
        assert requested.status_code == 200
        request_id = requested.json()["request_id"]
        assert "owner_user_id" not in requested.json()
        assert client.post(f"{case_path}/review-requests", json={"row_version": admin_case["row_version"], "client_request_id": "admin-request", "message": "Please review"}).json()["created"] is False
        assert len(notifications) == 1
        assert notifications[0]["user_id"] == q.owner.id
        assert client.get(case_path).json()["content"] == {}
        viewer[0] = q.owner
        owner_case = client.get(case_path).json()
        assert owner_case["review_requests"][0]["status"] == "pending"
        approved = client.post(f"{case_path}/review-requests/{request_id}/approve", json={"row_version": owner_case["row_version"], "reviewer_user_ids": [q.reviewer.id], "fields": ["query", "answer"], "evidence_indices": [0], "retention_days": 7})
        assert approved.status_code == 200
        viewer[0] = q.reviewer
        assert client.get(case_path).json()["content"]["answer"] == "Original answer"
        assert notifications[1]["user_id"] == q.reviewer.id
        assert actions == ["quality.review_request", "quality.review_request_approve"]
        viewer[0] = q.admin
        assert client.get(case_path).json()["content"] == {}
