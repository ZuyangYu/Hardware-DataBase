"""Owner consent must preserve the existing sharing and privacy boundaries."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src.quality.schemas import ReviewRequestCreate, ReviewRequestReject, ShareRequest
from src.quality.service import QualityService
from src.quality.store import QualityConflict, dumps
from tests.quality.test_feedback import feedback, quality as quality_fixture  # noqa: F401


def create(q):
    case_id = feedback(q)["case_id"]
    request = ReviewRequestCreate(row_version=q.store.get_case(case_id, q.admin)["row_version"], client_request_id="request", message="Please review")
    result = q.store.create_review_request(q.admin, case_id, request)
    return case_id, request, result["request_id"]


def approval(q, case_id):
    return ShareRequest(row_version=q.store.get_case(case_id, q.owner)["row_version"], reviewer_user_ids=[q.reviewer.id], fields=["answer"], evidence_indices=[0])


def test_creation_is_idempotent_and_concurrent(quality):
    q = quality
    case_id, request, request_id = create(q)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: q.store.create_review_request(q.admin, case_id, request), range(8)))
    assert all(row["request_id"] == request_id and not row["created"] for row in results)
    assert len(q.store.get_case(case_id, q.owner)["review_requests"]) == 1
    assert q.store.get_case(case_id, q.reviewer)["review_requests"] == []
    with pytest.raises(PermissionError):
        q.store.create_review_request(q.owner, case_id, request)
    with pytest.raises(QualityConflict):
        q.store.create_review_request(q.admin, case_id, request.model_copy(update={"message": "different"}))


def test_only_owner_can_decide_and_rejection_does_not_share(quality):
    q = quality
    case_id, request, request_id = create(q)
    body = approval(q, case_id)
    with pytest.raises(PermissionError):
        q.store.approve_review_request(q.reviewer, case_id, request_id, body)
    with pytest.raises(PermissionError):
        q.store.approve_review_request(q.admin, case_id, request_id, body)
    with pytest.raises(PermissionError):
        q.store.reject_review_request(q.reviewer, case_id, request_id, ReviewRequestReject(row_version=body.row_version))
    rejected = q.store.reject_review_request(q.owner, case_id, request_id, ReviewRequestReject(row_version=body.row_version, decision_note="Private owner note"))
    assert rejected["review_requests"][0]["status"] == "rejected"
    assert q.store.get_case(case_id, q.reviewer)["content"] == {}
    assert "Private owner note" not in dumps(q.store.get_case(case_id, q.admin))
    assert q.store.create_review_request(q.admin, case_id, request)["status"] == "rejected"
    with pytest.raises(QualityConflict):
        q.store.approve_review_request(q.owner, case_id, request_id, body)


def test_approval_checks_version_and_frozen_material(quality):
    q = quality
    case_id, _, request_id = create(q)
    body = approval(q, case_id)
    with pytest.raises(QualityConflict):
        q.store.approve_review_request(q.owner, case_id, request_id, body.model_copy(update={"row_version": body.row_version - 1}))
    with pytest.raises(ValueError):
        q.store.approve_review_request(q.owner, case_id, request_id, body.model_copy(update={"evidence_indices": [99]}))
    with pytest.raises(ValueError):
        q.store.approve_review_request(q.owner, case_id, request_id, body.model_copy(update={"fields": ["answer", "answer"]}))
    with q.store.transaction() as conn:
        row = conn.execute("SELECT * FROM quality_snapshots WHERE turn_id = ?", (q.turn.id,)).fetchone()
        manifest = json.loads(row["manifest_json"])
        manifest["has_attachments"] = True
        conn.execute("UPDATE quality_snapshots SET manifest_json = ? WHERE snapshot_id = ?", (dumps(manifest), row["snapshot_id"]))
    with pytest.raises(PermissionError, match="attachment"):
        q.store.approve_review_request(q.owner, case_id, request_id, body)
    assert q.store.get_case(case_id, q.owner)["review_requests"][0]["status"] == "pending"


def test_approval_preserves_department_kb_and_reviewer_guards(quality, monkeypatch):
    import src.api.context

    q = quality
    case_id, _, request_id = create(q)
    body = approval(q, case_id)
    service = QualityService(q.store, SimpleNamespace(list_users=lambda: [q.owner, q.reviewer, q.other, q.admin]))
    monkeypatch.setattr(src.api.context, "build_context_for_user", lambda *_args, **_kwargs: SimpleNamespace(has_kb_permission=lambda *_: True))
    for user_id in (q.admin.id, q.other.id, q.owner.id):
        with pytest.raises(PermissionError):
            service.approve_review_request(q.owner, case_id, request_id, body.model_copy(update={"reviewer_user_ids": [user_id]}))
    q.reviewer.is_active = False
    with pytest.raises(PermissionError):
        service.approve_review_request(q.owner, case_id, request_id, body)
    q.reviewer.is_active = True
    q.owner.department_id = 20
    with pytest.raises(PermissionError, match="department"):
        service.approve_review_request(q.owner, case_id, request_id, body)
    q.owner.department_id = 10
    monkeypatch.setattr(src.api.context, "build_context_for_user", lambda *_args, **_kwargs: SimpleNamespace(has_kb_permission=lambda *_: False))
    with pytest.raises(PermissionError, match="KB read"):
        service.approve_review_request(q.owner, case_id, request_id, body)
    assert q.store.get_case(case_id, q.owner)["review_requests"][0]["status"] == "pending"


def test_redaction_closes_pending_requests_and_delete_cascades(quality):
    q = quality
    case_id, _, request_id = create(q)
    body = approval(q, case_id)
    q.conv.edit_message(q.owner.id, q.session.id, q.turn.user_message_id, redact=True)
    assert q.store.get_case(case_id, q.owner)["review_requests"][0]["status"] == "rejected"
    with pytest.raises(QualityConflict):
        q.store.approve_review_request(q.owner, case_id, request_id, body)
    q.conv.delete_session(q.owner.id, q.session.id)
    with q.store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quality_review_requests").fetchone()[0] == 0
