"""Version binding and revision history for attachment candidates.

The final acceptance is "the user's request is truly reflected in the final
file and its audit chain".  When several candidates exist, editing without an
explicit id must bind to the newest candidate, and the history view must show
each version's change summary and release state.
"""

from __future__ import annotations

import io
import json

import src.settings
from src.agents.tools.attachment_tools import (
    make_attachment_edit,
    make_attachment_revision_history,
)
from src.agents.tools.runtime import ToolRuntime
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


def _runtime(service, records, *, session_id=7, user_id=11):
    return ToolRuntime(
        kb_name="ADAS",
        ctx=None,
        chat_session_id=str(session_id),
        source_scope="attachment_only",
        attachment_refs=service.build_refs(records),
        attachment_service=service,
        attachment_user_id=user_id,
        attachment_session_id=session_id,
    )


def _chain(service, *, session_id=7, user_id=11):
    parent = service.upload(
        session_id=session_id,
        user_id=user_id,
        filename="hsi.txt",
        stream=io.BytesIO("ADAS ICD v0 保留内容\n".encode()),
    )
    first = service.create_attachment_revision(
        parent_attachment_id=parent.attachment_id,
        session_id=session_id,
        user_id=user_id,
        before="v0",
        after="v1",
    )
    second = service.create_attachment_revision(
        parent_attachment_id=first.child_attachment_id,
        session_id=session_id,
        user_id=user_id,
        before="v1",
        after="v2",
    )
    records = [
        parent,
        service.get_attachment(
            attachment_id=first.child_attachment_id, session_id=session_id, user_id=user_id
        ),
        service.get_attachment(
            attachment_id=second.child_attachment_id, session_id=session_id, user_id=user_id
        ),
    ]
    return parent, first, second, records


def test_edit_without_target_binds_the_latest_candidate(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    _parent, _first, second, records = _chain(service)
    runtime = _runtime(service, records)

    payload = json.loads(
        make_attachment_edit(runtime)(before="v2", after="v3")
    )

    assert payload["status"] == "completed"
    assert "替换为" in payload["change_summary"]
    persisted = service.get_attachment_revision(
        revision_id=payload["revision_id"], session_id=7, user_id=11
    )
    # The new candidate chains from the newest version, not from the upload.
    assert persisted.parent_attachment_id == second.child_attachment_id


def test_explicit_revision_id_and_latest_selector_bind_versions(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    _parent, first, second, records = _chain(service)
    runtime = _runtime(service, records)

    from_selector = json.loads(
        make_attachment_edit(runtime)(
            before="v2",
            after="v2b",
            attachment_id="latest_candidate",
        )
    )
    persisted_selector = service.get_attachment_revision(
        revision_id=from_selector["revision_id"], session_id=7, user_id=11
    )
    assert persisted_selector.parent_attachment_id == second.child_attachment_id

    from_revision = json.loads(
        make_attachment_edit(runtime)(
            before="v1",
            after="v1b",
            attachment_id=first.revision_id,
        )
    )
    persisted = service.get_attachment_revision(
        revision_id=from_revision["revision_id"], session_id=7, user_id=11
    )
    assert persisted.parent_attachment_id == first.child_attachment_id


def test_history_lists_versions_summaries_and_release_state(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    _parent, first, second, records = _chain(service)
    runtime = _runtime(service, records)
    service.request_attachment_release(
        revision_id=first.revision_id, session_id=7, user_id=11
    )

    output = make_attachment_revision_history(runtime)()

    assert first.revision_id in output
    assert second.revision_id in output
    assert "发布审核中" in output
    assert "变更:" in output
    assert "hsi.revision" in output
    # Newest candidate is listed first.
    assert output.index(second.revision_id) < output.index(first.revision_id)


def test_edit_accepts_a_filename_alias_and_reports_attachment_ids(tmp_path, monkeypatch):
    from src.agents.tools.attachment_tools import make_attachment_list

    service, _store = _service(tmp_path, monkeypatch)
    _parent, first, _second, records = _chain(service)
    runtime = _runtime(service, records)

    listed = make_attachment_list(runtime)()
    assert f"attachment_id: {records[0].attachment_id}" in listed

    payload = json.loads(
        make_attachment_edit(runtime)(
            before="v1",
            after="v1-alias",
            attachment_id="hsi.revision",  # never a real id
        )
    )
    # A partial/other alias must stay blocked rather than guessing one target.
    assert payload["status"] == "blocked"

    payload = json.loads(
        make_attachment_edit(runtime)(
            before="v1",
            after="v1-alias",
            attachment_id=records[1].filename,
        )
    )
    assert payload["status"] == "completed"
    persisted = service.get_attachment_revision(
        revision_id=payload["revision_id"], session_id=7, user_id=11
    )
    assert persisted.parent_attachment_id == first.child_attachment_id


def test_blocked_edit_emits_a_visible_stage_event(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    _parent, _first, _second, records = _chain(service)
    runtime = _runtime(service, records)
    events: list[dict] = []
    runtime.on_event = events.append

    json.loads(
        make_attachment_edit(runtime)(
            before="v1", after="v1b", attachment_id="attachment-revision-missing"
        )
    )

    stages = [
        event for event in events
        if event["type"] == "stage" and event["payload"].get("key") == "attachment_edit"
    ]
    assert stages and stages[0]["payload"]["status"] == "blocked"
    assert stages[0]["payload"]["error_code"] == "attachment_scope_denied"


def test_follow_up_binds_lineage_even_when_candidate_is_not_in_turn_scope(
    tmp_path, monkeypatch
):
    service, _store = _service(tmp_path, monkeypatch)
    parent, _first, second, _records = _chain(service)
    # The follow-up turn only carried the original attachment; the candidate
    # from the previous turn must still be resolvable through its lineage.
    runtime = _runtime(service, [parent])
    runtime.question = "继续修改最新候选：把「v2」替换为「v3」，只生成候选。"

    payload = json.loads(make_attachment_edit(runtime)(before="v2", after="v3"))

    assert payload["status"] == "completed"
    persisted = service.get_attachment_revision(
        revision_id=payload["revision_id"], session_id=7, user_id=11
    )
    assert persisted.parent_attachment_id == second.child_attachment_id


def test_quoted_target_must_match_the_user_wording(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    _parent, _first, _second, records = _chain(service)
    runtime = _runtime(service, records)
    runtime.question = "请把附件中的「v2」替换为「v3」"

    blocked = json.loads(
        make_attachment_edit(runtime)(before="v2 保留内容", after="v3")
    )
    assert blocked["status"] == "blocked"
    assert blocked["error_code"] == "attachment_target_not_explicit"

    ok = json.loads(make_attachment_edit(runtime)(before="v2", after="v3"))
    assert ok["status"] == "completed"


def test_continue_editing_locks_to_the_latest_candidate(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent, _first, second, records = _chain(service)
    runtime = _runtime(service, records)
    runtime.question = "继续修改最新候选：把「v2」替换为「v3」，只生成候选。"

    wrong_version = json.loads(
        make_attachment_edit(runtime)(
            before="v2", after="v3", attachment_id=parent.attachment_id
        )
    )
    assert wrong_version["status"] == "blocked"
    assert wrong_version["error_code"] == "attachment_version_mismatch"

    correct = json.loads(make_attachment_edit(runtime)(before="v2", after="v3"))
    assert correct["status"] == "completed"
    persisted = service.get_attachment_revision(
        revision_id=correct["revision_id"], session_id=7, user_id=11
    )
    assert persisted.parent_attachment_id == second.child_attachment_id


def test_edit_rejects_foreign_revision_id(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    _parent, first, _second, records = _chain(service)
    runtime = _runtime(service, records)

    payload = json.loads(
        make_attachment_edit(runtime)(
            before="v0", after="v0b", attachment_id="attachment-revision-unknown"
        )
    )

    assert payload["status"] == "blocked"
    assert payload["error_code"] == "attachment_scope_denied"


def test_missing_revision_id_is_reported_to_the_model(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    _parent, _first, _second, records = _chain(service)
    runtime = _runtime(service, records)

    payload = json.loads(
        make_attachment_edit(runtime)(
            before="v1", after="v1b", attachment_id="attachment-revision-does-not-exist"
        )
    )

    assert payload["status"] == "blocked"
    assert payload["error_code"] == "attachment_scope_denied"
