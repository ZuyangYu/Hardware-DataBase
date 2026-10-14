"""Production feedback's transaction, authorization and recovery contracts."""
from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest

import src.settings as settings
from src.core.auth import AuthUser
from src.core.conversation import ConversationService, StaleTurnOwner
from src.quality.phoenix_sync import PhoenixAnnotationClient, PhoenixRejected, PhoenixRetry
from src.quality.schemas import FeedbackRequest, ReviewRequest, ReviewRequestCreate, ShareRequest
from src.quality.service import QualityService
from src.quality.store import QualityConflict, QualityLeaseLost, QualityStore, dumps
from src.quality.worker import process_one

DSN = os.getenv("HDB_TEST_POSTGRES_DSN", "")


@pytest.fixture(name="quality", params=["sqlite", "postgres"])
def quality(request, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "QUALITY_ENABLED", True)
    monkeypatch.setattr(settings, "QUALITY_CAPTURE_CONTENT", True)
    monkeypatch.setattr(settings, "QUALITY_SAMPLE_RATE", 0)
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)
    monkeypatch.setattr(settings, "CONVERSATION_SERIALIZE_SAME_THREAD", True)
    db = str(tmp_path / "chat.db")
    monkeypatch.setattr(settings, "AUTH_DB_PATH", db)
    if request.param == "postgres":
        if not DSN:
            pytest.skip("HDB_TEST_POSTGRES_DSN is not configured")
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        from src.storage.bootstrap import _control_plane
        from src.storage.control_plane.conversation_service import PostgresConversationService

        values = conninfo_to_dict(DSN)
        values["dbname"] = f"hdb_quality_{uuid.uuid4().hex[:12]}"
        dsn = make_conninfo(**values)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{values["dbname"]}"')
        monkeypatch.setattr(settings, "CONTROL_PLANE_BACKEND", "postgres")
        monkeypatch.setattr(settings, "CONTROL_PLANE_DATABASE_URL", dsn)
        _control_plane(dsn)
        store = QualityStore(initialize=True)
        with store.transaction() as conn:
            conn.execute("CREATE TABLE users(id BIGINT PRIMARY KEY, username TEXT, department_id BIGINT)")
        conv = PostgresConversationService(dsn)
    else:
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, department_id INTEGER)")
        conv = ConversationService(db)
        store = QualityStore(db)
    with store.transaction() as conn:
        conn.execute("INSERT INTO users(id, username, department_id) VALUES(1, 'owner', NULL)")
    owner = AuthUser(1, "owner", "employee", True, 10)
    reviewer = AuthUser(2, "reviewer", "employee", True, 10)
    other = AuthUser(3, "other", "employee", True, 20)
    admin = AuthUser(4, "admin", "system_admin", True)
    session = conv.create_session(owner.id, "kb", department_id=10)
    turn = conv.create_turn(owner.id, session.id, "Original question")
    claimed = conv.claim_turn(owner.id, turn.id, "worker")
    turn = conv.complete_turn(owner.id, turn.id, "Original answer", {"evidence": [{"text": "Frozen evidence", "file_name": "private.pdf"}, {"text": "Unselected evidence"}]},
                              worker_id="worker", fencing_token=claimed.fencing_token)
    fixture = SimpleNamespace(store=store, conv=conv, owner=owner, reviewer=reviewer, other=other, admin=admin,
                              session=session, turn=turn, attempt=claimed.attempt_id, token=claimed.fencing_token)
    yield fixture
    if request.param == "postgres":
        conv.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{values["dbname"]}" WITH (FORCE)')


def feedback(q, *, key="click-1", rating="unhelpful", comment="Private feedback", revision=None):
    state = q.store.feedback_state(q.owner.id, q.turn.id)
    request = FeedbackRequest(assistant_message_id=q.turn.assistant_message_id, answer_revision=revision or state["answer_revision"],
                              client_request_id=key, rating=rating, reason_codes=["factual_error"] if rating == "unhelpful" else [],
                              comment=comment if rating == "unhelpful" else "")
    return q.store.submit_feedback(q.owner, q.turn.id, request)


def share(q, case_id, *, fields=None, indices=None):
    row = q.store.get_case(case_id, q.owner)
    return q.store.change_share(q.owner, case_id, ShareRequest(row_version=row["row_version"], reviewer_user_ids=[q.reviewer.id],
                                fields=fields or ["query", "answer"], evidence_indices=indices or [0]))


def test_fixed_snapshot_and_attempt_are_captured_atomically(quality):
    q = quality
    with closing(q.store.connect()) as conn:
        snapshot = conn.execute("SELECT * FROM quality_snapshots WHERE turn_id = ?", (q.turn.id,)).fetchone()
        manifest = json.loads(snapshot["manifest_json"])
        assert manifest["execution_attempt_id"] == q.attempt
        assert manifest["fencing_token"] == q.token
        assert manifest["result_span_id"] == ""  # Parent API context is not the result span.
        assert conn.execute("SELECT COUNT(*) FROM quality_outbox WHERE kind = 'collect'").fetchone()[0] == 1
    case_id = feedback(q)["case_id"]
    q.conv.edit_message(q.owner.id, q.session.id, q.turn.user_message_id, content="Updated question")
    row = q.store.get_case(case_id, q.owner)
    assert row["content"]["query"] == "Original question"
    assert row["content"]["answer"] == "Original answer"
    assert row["content"]["evidence"][0]["text"] == "Frozen evidence"
    assert row["answer_revision"] == q.store.feedback_state(q.owner.id, q.turn.id)["answer_revision"]


def test_duplicate_feedback_retries_are_idempotent_under_concurrency(quality):
    q = quality
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: feedback(q), range(8)))
    assert len({r["event_id"] for r in results}) == 1
    with closing(q.store.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quality_feedback_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quality_outbox WHERE kind = 'phoenix_annotation'").fetchone()[0] == 1
    with pytest.raises(QualityConflict):
        feedback(q, rating="helpful")
    with pytest.raises(QualityConflict):
        feedback(q, key="wrong-revision", revision="0" * 64)
    feedback(q, key="modify", rating="helpful")
    feedback(q, key="withdraw", rating="withdrawn")
    assert q.store.feedback_state(q.owner.id, q.turn.id)["rating"] == "withdrawn"


def test_admin_and_department_metadata_do_not_grant_content(quality):
    q = quality
    case_id = feedback(q)["case_id"]
    for viewer in (q.admin, q.reviewer):
        row = q.store.get_case(case_id, viewer)
        assert row["content"] == {}
        assert row["feedback"]["comment"] == ""
        assert "private.pdf" not in dumps(row)
        assert "Original question" not in dumps(q.store.list_cases(viewer))
        with pytest.raises(PermissionError):
            q.store.submit_review(viewer, case_id, ReviewRequest(row_version=row["row_version"], client_request_id="review", judgment="confirmed"))
    with pytest.raises(KeyError):
        q.store.get_case(case_id, q.other)
    with pytest.raises(KeyError):
        q.store.feedback_state(q.reviewer.id, q.turn.id)
    assert q.store.list_cases(q.other) == []


def test_admin_case_detail_exposes_issue_metadata_without_private_content(quality):
    q = quality
    case_id = feedback(q)["case_id"]
    share(q, case_id)
    row = q.store.get_case(case_id, q.reviewer)
    q.store.submit_review(q.reviewer, case_id, ReviewRequest(
        row_version=row["row_version"], client_request_id="metadata-review",
        judgment="confirmed", correctness="correct", root_cause="presentation",
        reference_answer="Private fixed answer", notes="Private review notes", rubric="Private rubric",
    ))
    feedback(q, key="metadata-follow-up", rating="helpful")

    admin = q.store.get_case(case_id, q.admin)
    assert admin["feedback"]["rating"] == "helpful"
    assert admin["feedback"]["reason_codes"] == []
    assert admin["feedback"]["comment"] == ""
    assert [(event["rating"], event["reason_codes"]) for event in admin["feedback_history"]] == [
        ("unhelpful", ["factual_error"]), ("helpful", []),
    ]
    assert admin["content"] == {}
    assert admin["reviews"][0]["judgment"] == "confirmed"
    assert admin["reviews"][0]["root_cause"] == "presentation"
    assert admin["reviews"][0]["correctness"] == "correct"
    assert admin["reviews"][0]["content"] == {}


def test_admin_review_request_keeps_content_hidden_until_owner_approves(quality):
    q = quality
    case_id = feedback(q)["case_id"]
    admin_case = q.store.get_case(case_id, q.admin)
    created = q.store.create_review_request(q.admin, case_id, ReviewRequestCreate(
        row_version=admin_case["row_version"], client_request_id="request-1", message="Please review this case"))
    assert created["created"] is True
    assert q.store.get_case(case_id, q.admin)["content"] == {}
    assert q.store.feedback_state(q.owner.id, q.turn.id)["review_requests"][0]["status"] == "pending"
    with pytest.raises(QualityConflict):
        q.store.create_review_request(q.admin, case_id, ReviewRequestCreate(
            row_version=admin_case["row_version"], client_request_id="request-1", message="Changed request"))
    owner_case = q.store.get_case(case_id, q.owner)
    assert owner_case["review_requests"][0]["status"] == "pending"
    duplicate = q.store.create_review_request(q.admin, case_id, ReviewRequestCreate(
        row_version=owner_case["row_version"], client_request_id="request-2"))
    assert duplicate["created"] is False
    approved = q.store.approve_review_request(q.owner, case_id, created["request_id"], ShareRequest(
        row_version=owner_case["row_version"], reviewer_user_ids=[q.reviewer.id], fields=["query", "answer"], evidence_indices=[0]))
    assert approved["review_requests"][0]["status"] == "approved"
    assert q.store.get_case(case_id, q.admin)["content"] == {}
    assert q.store.get_case(case_id, q.reviewer)["content"]["answer"] == "Original answer"


def test_selected_copy_review_revision_and_revoke(quality):
    q = quality
    case_id = feedback(q)["case_id"]
    share(q, case_id)
    row = q.store.get_case(case_id, q.reviewer)
    assert row["content"]["answer"] == "Original answer"
    assert len(row["content"]["evidence"]) == 1
    assert row["feedback"]["comment"] == ""
    request = ReviewRequest(row_version=row["row_version"], client_request_id="review-1", judgment="confirmed",
                            root_cause="retrieval", reference_answer="Correct reference", notes="Private review")
    result = q.store.submit_review(q.reviewer, case_id, request)
    assert q.store.submit_review(q.reviewer, case_id, request)["review_id"] == result["review_id"]
    assert q.store.get_case(case_id, q.owner)["status"] == "reviewed"
    with pytest.raises(QualityConflict):
        q.store.submit_review(q.reviewer, case_id, request.model_copy(update={"client_request_id": "stale"}))
    owner_row = q.store.get_case(case_id, q.owner)
    q.store.change_share(q.owner, case_id, row_version=owner_row["row_version"])
    revoked = q.store.get_case(case_id, q.reviewer)
    assert revoked["content"] == {}
    assert revoked["reviews"][0]["content"] == {}
    with pytest.raises(PermissionError):
        q.store.submit_review(q.reviewer, case_id, request)


def test_expired_copy_and_department_move_remove_access(quality):
    q = quality
    case_id = feedback(q)["case_id"]
    share(q, case_id)
    q.reviewer.department_id = 20
    with pytest.raises(KeyError):
        q.store.get_case(case_id, q.reviewer)
    q.reviewer.department_id = 10
    with q.store.transaction() as conn:
        row = conn.execute("SELECT share_json FROM quality_cases WHERE case_id = ?", (case_id,)).fetchone()
        scope = json.loads(row[0])
        scope["expires_at"] = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE quality_cases SET share_json = ? WHERE case_id = ?", (dumps(scope), case_id))
    assert q.store.get_case(case_id, q.reviewer)["content"] == {}


def test_redaction_erases_quality_material_and_delete_cascades(quality):
    q = quality
    case_id = feedback(q)["case_id"]
    share(q, case_id)
    q.conv.edit_message(q.owner.id, q.session.id, q.turn.user_message_id, redact=True)
    for viewer in (q.owner, q.reviewer, q.admin):
        row = q.store.get_case(case_id, viewer)
        assert row["content"] == {}
        assert row["feedback"]["comment"] == ""
        assert not row["source_valid"]
    with closing(q.store.connect()) as conn:
        assert conn.execute("SELECT content_json FROM quality_snapshots").fetchone()[0] == "{}"
        assert conn.execute("SELECT comment FROM quality_feedback_events").fetchone()[0] == ""
    q.conv.delete_session(q.owner.id, q.session.id)
    with closing(q.store.connect()) as conn:
        for table in ("quality_snapshots", "quality_cases", "quality_feedback_events", "quality_outbox"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_terminal_snapshot_failure_rolls_back_answer_and_events(quality, monkeypatch):
    q = quality
    q.conv.release_turn_ownership(q.turn.id, "worker", fencing_token=q.token)
    next_turn = q.conv.create_turn(q.owner.id, q.session.id, "Next question")
    next_claim = q.conv.claim_turn(q.owner.id, next_turn.id, "next-worker")
    import src.quality.store as store_module

    def fail(*_):
        raise RuntimeError("quality insert failed")
    monkeypatch.setattr(store_module, "capture_completed_turn", fail)
    with pytest.raises(RuntimeError, match="quality insert failed"):
        q.conv.complete_turn_result(q.owner.id, next_turn.id, "must rollback", {}, worker_id="next-worker", fencing_token=next_claim.fencing_token,
                                    terminal_events_builder=lambda _: [("done", {"answer": "must rollback"})])
    current = q.conv.get_turn(q.owner.id, next_turn.id)
    assert current.status == "streaming"
    assert current.answer == ""
    assert all(e.event_type != "done" for e in q.conv.list_turn_events(q.owner.id, next_turn.id))
    assert next(m for m in q.conv.list_messages(q.owner.id, q.session.id) if m.id == next_turn.assistant_message_id).content == ""


def test_terminal_cannot_be_rewritten(quality):
    q = quality
    with pytest.raises(StaleTurnOwner):
        q.conv.complete_turn(q.owner.id, q.turn.id, "changed", {})
    assert q.conv.get_turn(q.owner.id, q.turn.id).answer == "Original answer"


def test_job_claim_takeover_and_stale_fencing(quality):
    q = quality
    claim = q.store.claim("old")
    assert claim and q.store.claim("competitor") is None
    with q.store.transaction() as conn:
        conn.execute("UPDATE quality_outbox SET lease_until = 0 WHERE job_id = ?", (claim["job_id"],))
    successor = q.store.claim("new")
    assert successor["fencing_token"] == claim["fencing_token"] + 1
    with pytest.raises(QualityLeaseLost):
        q.store.finish(claim)
    with pytest.raises(QualityLeaseLost):
        q.store.collect(claim)
    q.store.collect(successor)
    assert q.store.claim("third") is None


def test_projection_no_span_and_retry(quality, monkeypatch):
    q = quality
    feedback(q)
    while process_one(q.store, "no-span"):
        pass
    with q.store.transaction() as conn:
        row = conn.execute("SELECT manifest_json FROM quality_snapshots").fetchone()
        manifest = json.loads(row[0])
        manifest.update(result_span_id="0123456789abcdef", sampling_status="sampled")
        conn.execute("UPDATE quality_snapshots SET manifest_json = ?", (dumps(manifest),))
        conn.execute("UPDATE quality_outbox SET status = 'pending' WHERE kind = 'phoenix_annotation'")
    sent = []
    def sync(span_id, payload):
        sent.append(payload)
        if len(sent) == 1:
            raise PhoenixRetry("late_span")
    client = SimpleNamespace(sync=sync)
    monkeypatch.setattr(settings, "QUALITY_PHOENIX_MAX_ATTEMPTS", 20)
    process_one(q.store, "writer", client)
    with q.store.transaction() as conn:
        row = conn.execute("SELECT * FROM quality_outbox WHERE kind = 'phoenix_annotation'").fetchone()
        assert row["status"] == "pending"
        assert row["last_error"] == "late_span"
        conn.execute("UPDATE quality_outbox SET available_at = 0")
    process_one(q.store, "writer", client)
    assert sent[0]["identifier"] == sent[1]["identifier"]
    assert "Private feedback" not in dumps(sent)
    with closing(q.store.connect()) as conn:
        assert conn.execute("SELECT status FROM quality_outbox WHERE kind = 'phoenix_annotation'").fetchone()[0] == "completed"


def test_sampling_daily_cap_is_recorded_and_feedback_cohort_stays_distinct(quality, monkeypatch):
    q = quality
    monkeypatch.setattr(settings, "QUALITY_SAMPLE_RATE", 1)
    monkeypatch.setattr(settings, "QUALITY_SAMPLE_DAILY_LIMIT", 1)
    case_id = feedback(q)["case_id"]
    process_one(q.store, "collector")
    case = q.store.get_case(case_id, q.owner)
    assert set(case["triggers"]) == {"feedback", "random_sample"}
    assert case["sampling"]["probability"] == 1
    assert case["sampling"]["policy"] == "quality-sample-v1"


def test_backfill_is_explicit_and_skips_edited_historical_sources(quality, monkeypatch):
    q = quality
    with q.store.transaction() as conn:
        conn.execute("DELETE FROM quality_snapshots")
    with pytest.raises(KeyError):
        q.store.feedback_state(q.owner.id, q.turn.id)
    assert q.store.backfill() == 1
    assert q.store.backfill() == 0
    with closing(q.store.connect()) as conn:
        manifest = json.loads(conn.execute("SELECT manifest_json FROM quality_snapshots").fetchone()[0])
        assert manifest["capture_origin"] == "terminal_record_backfill"
        assert manifest["execution_attempt_id"] == ""
    q.conv.edit_message(q.owner.id, q.session.id, q.turn.user_message_id, content="Edited")
    with q.store.transaction() as conn:
        conn.execute("DELETE FROM quality_snapshots")
    assert q.store.backfill() == 0


def test_phoenix_sync_uses_fixed_identifier_and_no_private_content():
    requests = []
    annotation = {}
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            annotation.update(json.loads(request.content)["data"][0])
            return httpx.Response(200, json={"data": [{"id": "annotation-1"}]})
        return httpx.Response(200, json={"data": [annotation]})
    client = PhoenixAnnotationClient(endpoint="https://phoenix.test", api_key="secret", project="test", transport=httpx.MockTransport(handler))
    payload = {"identifier": "feedback:event-1", "name": "user_feedback", "annotator_kind": "HUMAN", "label": "unhelpful", "score": 0,
               "comment": "Private feedback", "metadata": {"case_id": "case-1", "query": "Private query"}}
    client.sync("0123456789abcdef", payload)
    assert requests[0].url.params["sync"] == "true"
    assert annotation["identifier"] == "feedback:event-1"
    assert "Private" not in dumps(annotation)
    assert requests[0].headers["Authorization"] == "Bearer secret"


@pytest.mark.parametrize("status,exception", [(404, PhoenixRetry), (503, PhoenixRetry), (401, PhoenixRejected), (403, PhoenixRejected), (422, PhoenixRejected)])
def test_phoenix_retry_vs_permanent_rejection(status, exception):
    client = PhoenixAnnotationClient(endpoint="https://phoenix.test", transport=httpx.MockTransport(lambda _: httpx.Response(status)))
    with pytest.raises(exception):
        client.sync("0123456789abcdef", {"name": "user_feedback", "annotator_kind": "HUMAN", "label": "withdrawn", "identifier": "feedback:1"})


def test_service_rejects_admin_inactive_or_cross_department_reviewers(quality, monkeypatch):
    q = quality
    case_id = feedback(q)["case_id"]
    fake_auth = SimpleNamespace(list_users=lambda: [q.owner, q.reviewer, q.other, q.admin])
    service = QualityService(q.store, fake_auth)
    import src.api.context
    monkeypatch.setattr(src.api.context, "build_context_for_user", lambda *_args, **_kwargs: SimpleNamespace(has_kb_permission=lambda *_: True))
    for actor_id in (q.admin.id, q.other.id):
        with pytest.raises(PermissionError):
            service.share(q.owner, case_id, ShareRequest(row_version=2, reviewer_user_ids=[actor_id], fields=["answer"]))
    q.reviewer.is_active = False
    assert service.reviewers(q.owner) == []
    q.reviewer.is_active = True
    row = q.store.get_case(case_id, q.owner)
    service.share(q.owner, case_id, ShareRequest(row_version=row["row_version"], reviewer_user_ids=[q.reviewer.id], fields=["answer"]))


def test_shared_comment_is_fixed_until_explicitly_reshared(quality):
    q = quality
    case_id = feedback(q, comment="Originally shared comment")["case_id"]
    share(q, case_id, fields=["answer", "comment"])
    feedback(q, key="new-comment", comment="New private comment")
    assert q.store.get_case(case_id, q.reviewer)["feedback"]["comment"] == "Originally shared comment"
    assert q.store.get_case(case_id, q.owner)["feedback"]["comment"] == "New private comment"


def test_source_deletion_revokes_fixed_copies_only_in_its_scope(quality):
    q = quality
    case_id = feedback(q)["case_id"]
    share(q, case_id)
    assert q.store.invalidate_source(kb_name="kb", department_id=20, filename="private.pdf") == 0
    assert q.store.invalidate_source(kb_name="kb", department_id=10, filename="different.pdf") == 0
    assert q.store.invalidate_source(kb_name="kb", department_id=10, filename="private.pdf") == 1
    row = q.store.get_case(case_id, q.reviewer)
    assert row["content"] == {} and not row["source_valid"]


def test_actual_agent_evidence_shape_and_content_disabled(quality, monkeypatch):
    q = quality
    q.conv.release_turn_ownership(q.turn.id, "worker", fencing_token=q.token)
    turn = q.conv.create_turn(q.owner.id, q.session.id, "agent evidence")
    claim = q.conv.claim_turn(q.owner.id, turn.id, "agent")
    q.conv.complete_turn(q.owner.id, turn.id, "Grounded answer", {"status": "partial_failure", "evidence": [
        {"id": "chunk-123", "content": "Real retrieved bytes", "source_name": "actual.pdf", "metadata": {"content_hash": "version-hash", "document_id": "document-123"}},
    ]}, worker_id="agent", fencing_token=claim.fencing_token)
    with closing(q.store.connect()) as conn:
        row = conn.execute("SELECT * FROM quality_snapshots WHERE turn_id = ?", (turn.id,)).fetchone()
        evidence = json.loads(row["content_json"])["evidence"][0]
        assert evidence["text"] == "Real retrieved bytes" and evidence["file_name"] == "actual.pdf"
        assert evidence["content_hash"] == "version-hash" and len(evidence["retrieved_content_sha256"]) == 64
        assert json.loads(row["manifest_json"])["diagnostic_flags"] == ["tool_failure"]
    monkeypatch.setattr(settings, "QUALITY_CAPTURE_CONTENT", False)
    q.conv.release_turn_ownership(turn.id, "agent", fencing_token=claim.fencing_token)
    private_turn = q.conv.create_turn(q.owner.id, q.session.id, "Not collected")
    private_claim = q.conv.claim_turn(q.owner.id, private_turn.id, "private")
    q.conv.complete_turn(q.owner.id, private_turn.id, "Not collected answer", {}, worker_id="private", fencing_token=private_claim.fencing_token)
    assert not q.store.feedback_state(q.owner.id, private_turn.id)["content_available"]
    with closing(q.store.connect()) as conn:
        assert conn.execute("SELECT content_json FROM quality_snapshots WHERE turn_id = ?", (private_turn.id,)).fetchone()[0] == "{}"


def _claim_child(db_path, dsn, owner, queue, ready):
    if dsn:
        settings.CONTROL_PLANE_BACKEND = "postgres"
        settings.CONTROL_PLANE_DATABASE_URL = dsn
        store = QualityStore()
    else:
        store = QualityStore(db_path)
    ready.wait(15)
    queue.put(store.claim(owner))


def test_actual_processes_cannot_double_claim(quality):
    q = quality
    context = multiprocessing.get_context("spawn")
    queue, ready = context.Queue(), context.Event()
    children = [context.Process(target=_claim_child, args=(q.store.db_path, q.store.dsn if q.store.backend == "postgres" else "", owner, queue, ready)) for owner in ("process-1", "process-2")]
    try:
        for child in children:
            child.start()
        ready.set()
        results = [queue.get(timeout=30), queue.get(timeout=30)]
        assert sum(row is not None for row in results) == 1
        for child in children:
            child.join(30)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(10)
        queue.close()


def test_schema_marker_and_postgres_uniqueness_are_fail_closed(quality):
    q = quality
    from src.quality.store import assert_schema

    with q.store.transaction() as conn:
        conn.execute("UPDATE quality_schema SET version = 99")
    with closing(q.store.connect()) as conn, pytest.raises(RuntimeError, match="schema not ready"):
        assert_schema(conn)
    with q.store.transaction() as conn:
        conn.execute("UPDATE quality_schema SET version = 1")
    if q.store.backend == "postgres":
        with q.store.transaction() as conn:
            conn.execute("ALTER TABLE quality_outbox DROP CONSTRAINT quality_outbox_dedup_key_key")
        with closing(q.store.connect()) as conn, pytest.raises(RuntimeError, match="schema not ready"):
            assert_schema(conn)


def test_request_key_reuse_across_concurrent_sessions_is_a_conflict(quality):
    q = quality
    session = q.conv.create_session(q.owner.id, "kb", department_id=10)
    second = q.conv.create_turn(q.owner.id, session.id, "Second session")
    q.conv.complete_turn(q.owner.id, second.id, "Second answer", {})
    requests = []
    for turn in (q.turn, second):
        state = q.store.feedback_state(q.owner.id, turn.id)
        requests.append((turn.id, FeedbackRequest(assistant_message_id=turn.assistant_message_id, answer_revision=state["answer_revision"], client_request_id="same-global-key", rating="helpful")))
    def submit(item):
        try:
            q.store.submit_feedback(q.owner, item[0], item[1])
            return "accepted"
        except QualityConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, requests)) == ["accepted", "conflict"]
    with closing(q.store.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quality_feedback_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quality_cases").fetchone()[0] == 1
