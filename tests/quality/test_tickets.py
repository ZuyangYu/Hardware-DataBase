"""Support tickets grant access to submitted material, independently of quality review."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_user, get_auth_service
from src.api.routes import quality as routes
from src.quality.schemas import FeedbackRequest
from src.quality.store import QualityConflict, dumps, sha256
from tests.quality.test_feedback import quality as quality_fixture  # noqa: F401


def submit(q, key="ticket-submit", **changes):
    state = q.store.feedback_state(q.owner.id, q.turn.id)
    body = dict(assistant_message_id=q.turn.assistant_message_id,
                answer_revision=state["answer_revision"], client_request_id=key,
                rating="unhelpful", reason_codes=["factual_error"], comment="电流单位有误",
                share_with_support=True, support_contact="企业微信：owner")
    return q.store.submit_feedback(q.owner, q.turn.id, FeedbackRequest(**{**body, **changes}))


def service(q):
    from src.quality.tickets import TicketService
    return TicketService(q.store)


def update(q, viewer, ticket_id, action, message="", **changes):
    from src.quality.tickets import TicketUpdateRequest
    row = service(q).get(ticket_id, viewer)
    return service(q).update(viewer, ticket_id, TicketUpdateRequest(
        row_version=row["row_version"], client_request_id=f"{action}-{row['row_version']}-{viewer.id}",
        action=action, message=message, **changes))


def test_submission_grants_admin_only_the_submitted_ticket_material(quality):
    q = quality
    result = submit(q)
    assert result["ticket_id"]
    ticket = service(q).get(result["ticket_id"], q.admin)
    assert ticket["status"] == "open"
    assert ticket["owner_user_id"] == 1
    assert ticket["requester_username"] == "owner"
    assert ticket["department_id"] == 10
    assert ticket["content"] == {"query": "Original question", "answer": "Original answer"}
    assert ticket["comment"] == "电流单位有误"
    assert ticket["contact"] == "企业微信：owner"
    assert "private.pdf" not in dumps(ticket)
    # Ticket consent never opens the original review/KB authorization.
    assert q.store.get_case(result["case_id"], q.admin)["content"] == {}
    assert q.store.get_case(result["case_id"], q.owner)["status"] == "new"
    assert q.store.feedback_state(q.owner.id, q.turn.id)["ticket_id"] == result["ticket_id"]


def test_feedback_without_consent_creates_no_support_ticket(quality):
    q = quality
    result = submit(q, share_with_support=False, support_contact="")
    assert result.get("ticket_id") is None
    assert service(q).list(q.admin)["items"] == []


def test_old_feedback_retry_survives_additive_ticket_fields(quality):
    q = quality
    body = FeedbackRequest(assistant_message_id=q.turn.assistant_message_id,
        answer_revision=q.store.feedback_state(q.owner.id, q.turn.id)["answer_revision"],
        client_request_id="legacy-retry", rating="unhelpful", comment="Old feedback")
    result = q.store.submit_feedback(q.owner, q.turn.id, body)
    old_payload = body.model_dump(exclude={"share_with_support", "support_contact"})
    with q.store.transaction() as conn:
        conn.execute("UPDATE quality_feedback_events SET request_sha256=? WHERE event_id=?",
                     (sha256([q.turn.id, old_payload]), result["event_id"]))
    assert q.store.submit_feedback(q.owner, q.turn.id, body)["deduplicated"] is True


def test_ticket_contact_survives_loading_feedback_for_an_existing_answer(quality):
    q = quality
    submit(q)
    assert q.store.feedback_state(q.owner.id, q.turn.id)["support_contact"] == "企业微信：owner"


def test_ticket_records_the_submitters_department_after_a_department_move(quality):
    from dataclasses import replace

    q = quality
    q.owner = replace(q.owner, department_id=20, department_name="新研发部门")
    ticket = service(q).get(submit(q)["ticket_id"], q.admin)
    assert ticket["department_id"] == 20
    assert ticket["department_name"] == "新研发部门"
    assert service(q).list(q.admin, department_id=20)["total"] == 1
    assert service(q).list(q.admin, department_id=10)["total"] == 0


def test_assignment_requires_an_active_administrator_and_preserves_history(quality):
    from types import SimpleNamespace
    from src.core.auth import AuthUser
    from src.quality.tickets import TicketService, TicketUpdateRequest

    q = quality
    ticket_id = submit(q)["ticket_id"]
    second_admin = AuthUser(5, "support-b", "system_admin", True)
    assigned = TicketService(q.store, auth=SimpleNamespace(get_user_by_id=lambda _: second_admin)).update(
        q.admin, ticket_id, TicketUpdateRequest(row_version=1, client_request_id="assign", action="assign", assignee_user_id=5))
    assert assigned["assignee_user_id"] == 5 and assigned["assignee_username"] == "support-b"
    assert "support-b" in assigned["events"][-1]["message"]
    for invalid in (q.owner, AuthUser(6, "disabled", "system_admin", False), None):
        with pytest.raises(ValueError):
            TicketService(q.store, auth=SimpleNamespace(get_user_by_id=lambda _: invalid)).update(
                q.admin, ticket_id, TicketUpdateRequest(row_version=2, client_request_id="bad-assign", action="assign", assignee_user_id=6))


def test_employees_cannot_read_or_modify_colleagues_tickets(quality):
    q = quality
    ticket_id = submit(q)["ticket_id"]
    for viewer in (q.reviewer, q.other):
        assert service(q).list(viewer)["items"] == []
        with pytest.raises(KeyError):
            service(q).get(ticket_id, viewer)
        from src.quality.tickets import TicketUpdateRequest
        with pytest.raises(KeyError):
            service(q).update(viewer, ticket_id, TicketUpdateRequest(
                row_version=1, client_request_id="intruder", action="reply", message="intruder"))


def test_repeated_submission_is_atomic_and_appends_to_one_ticket(quality):
    q = quality
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(q), range(8)))
    assert len({r["ticket_id"] for r in results}) == 1
    ticket_id = results[0]["ticket_id"]
    assert len(service(q).get(ticket_id, q.admin)["events"]) == 1
    submit(q, key="supplement", comment="补充：应该为 2A")
    row = service(q).get(ticket_id, q.admin)
    assert row["comment"] == "补充：应该为 2A"
    assert len(row["events"]) == 2
    assert service(q).list(q.admin)["total"] == 1


def test_ticket_lifecycle_keeps_resolution_separate_from_quality_verification(quality):
    q = quality
    ticket_id = submit(q)["ticket_id"]
    row = update(q, q.admin, ticket_id, "claim")
    assert row["status"] == "in_progress" and row["assignee_user_id"] == q.admin.id
    assert update(q, q.admin, ticket_id, "wait", "请补充型号")["status"] == "waiting"
    assert update(q, q.owner, ticket_id, "reply", "型号为 A")["status"] == "in_progress"
    resolved = update(q, q.admin, ticket_id, "resolve", "已提供正确规格")
    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == "已提供正确规格"
    assert q.store.get_case(resolved["case_id"], q.owner)["status"] == "new"
    assert update(q, q.owner, ticket_id, "reopen", "仍有问题")["status"] == "open"
    assert update(q, q.owner, ticket_id, "close", "已通过其他渠道解决")["status"] == "closed"
    with pytest.raises(QualityConflict):
        update(q, q.owner, ticket_id, "reply", "新信息")
    with pytest.raises(PermissionError):
        update(q, q.owner, ticket_id, "reopen")
    assert update(q, q.admin, ticket_id, "reopen")["status"] == "open"


def test_resolving_requires_an_explanation_and_only_admin_can_resolve(quality):
    q = quality
    from src.quality.tickets import TicketUpdateRequest
    ticket_id = submit(q)["ticket_id"]
    with pytest.raises(ValueError):
        TicketUpdateRequest(row_version=1, client_request_id="empty", action="resolve", message="  ")
    with pytest.raises(PermissionError):
        update(q, q.owner, ticket_id, "resolve", "自己标记管理员已处理")
    with pytest.raises(PermissionError):
        service(q).delete(q.owner, ticket_id, row_version=1)


def test_stale_updates_cannot_overwrite_replies_and_retries_are_idempotent(quality):
    q = quality
    from src.quality.tickets import TicketUpdateRequest
    ticket_id = submit(q)["ticket_id"]
    body = TicketUpdateRequest(row_version=1, client_request_id="reply-once", action="reply", message="已联系员工")
    first = service(q).update(q.admin, ticket_id, body)
    second = service(q).update(q.admin, ticket_id, body)
    assert first["row_version"] == second["row_version"] == 2
    assert len(second["events"]) == 2
    with pytest.raises(QualityConflict):
        service(q).update(q.admin, ticket_id, body.model_copy(update={"message": "different"}))
    with pytest.raises(QualityConflict):
        service(q).update(q.owner, ticket_id, body.model_copy(update={"client_request_id": "stale"}))


def test_delete_hides_ticket_and_purges_support_text_without_deleting_original_answer(quality):
    q = quality
    result = submit(q)
    ticket_id = result["ticket_id"]
    update(q, q.admin, ticket_id, "reply", "私下沟通详情")
    with pytest.raises(QualityConflict):
        service(q).delete(q.admin, ticket_id, row_version=1)
    service(q).delete(q.admin, ticket_id, row_version=2)
    for viewer in (q.admin, q.owner):
        assert service(q).list(viewer)["items"] == []
        with pytest.raises(KeyError):
            service(q).get(ticket_id, viewer)
    assert q.store.get_case(result["case_id"], q.owner)["content"]["answer"] == "Original answer"
    with closing(q.store.connect()) as conn:
        deleted = conn.execute("SELECT comment, contact, resolution FROM quality_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        assert tuple(deleted[field] for field in ("comment", "contact", "resolution")) == ("", "", "")
        assert all(not row[0] for row in conn.execute("SELECT message FROM quality_ticket_events WHERE ticket_id=?", (ticket_id,)))
    assert submit(q)["ticket_id"] is None  # An old HTTP retry cannot resurrect it.
    assert submit(q, key="new-submission")["ticket_id"] != ticket_id


def test_redaction_and_withdrawal_revoke_submitted_material(quality):
    q = quality
    ticket_id = submit(q)["ticket_id"]
    update(q, q.admin, ticket_id, "reply", "private handling notes")
    submit(q, key="withdraw", rating="withdrawn", reason_codes=[], comment="", share_with_support=False, support_contact="")
    row = service(q).get(ticket_id, q.admin)
    assert row["status"] == "closed"
    assert row["content"] == {} and row["comment"] == "" and row["contact"] == ""
    assert "private handling notes" not in dumps(row)
    with pytest.raises(QualityConflict):
        update(q, q.admin, ticket_id, "reopen")
    submit(q, key="new-consent")
    q.conv.edit_message(q.owner.id, q.session.id, q.turn.assistant_message_id, redact=True)
    row = service(q).get(ticket_id, q.admin)
    assert row["content"] == {} and row["comment"] == ""
    assert all(not event["message"] for event in row["events"])


def test_ticket_list_filters_and_pagination_are_scoped(quality):
    q = quality
    ticket_id = submit(q)["ticket_id"]
    assert service(q).list(q.admin, department_id=20)["total"] == 0
    assert service(q).list(q.admin, status="resolved")["total"] == 0
    assert service(q).list(q.admin, search="owner")["items"][0]["ticket_id"] == ticket_id
    assert service(q).list(q.admin, search="missing")["total"] == 0
    assert service(q).list(q.owner, offset=1)["items"] == []
    assert service(q).list(q.owner, offset=1)["total"] == 1


def test_ticket_api_enforces_roles_versions_and_deletion(quality, monkeypatch):
    from types import SimpleNamespace
    import src.settings as settings

    q = quality
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "")
    from src.api.routes import quality_tickets
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.include_router(quality_tickets.router, prefix="/api/v1")
    app.dependency_overrides[routes._store] = lambda: q.store
    viewer = [q.owner]
    app.dependency_overrides[current_user] = lambda: viewer[0]
    app.dependency_overrides[get_auth_service] = lambda: SimpleNamespace(list_users=lambda: [q.owner, q.admin])
    monkeypatch.setattr(quality_tickets, "_notify", lambda *_, **__: None)
    monkeypatch.setattr(quality_tickets, "_audit", lambda *_: None)
    state = q.store.feedback_state(q.owner.id, q.turn.id)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/chat/turns/{q.turn.id}/feedback", json={
            "assistant_message_id": q.turn.assistant_message_id, "answer_revision": state["answer_revision"],
            "client_request_id": "api-submit", "rating": "unhelpful", "comment": "请联系我", "share_with_support": True})
        assert response.status_code == 200
        path = f"/api/v1/quality/tickets/{response.json()['ticket_id']}"
        assert client.get(path).json()["comment"] == "请联系我"
        assert client.delete(path + "?row_version=1").status_code == 403
        viewer[0] = q.reviewer
        assert client.get(path).status_code == 404
        viewer[0] = q.admin
        resolved = client.post(path + "/actions", json={"row_version": 1, "client_request_id": "api-resolve", "action": "resolve", "message": "已提供解决办法"})
        assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
        assert client.delete(path + "?row_version=1").status_code == 409
        assert client.delete(path + "?row_version=2").status_code == 200
        assert client.get(path).status_code == 404
