"""Governed two-stage attachment section edits (proposal -> user confirmation).

The model may propose content, but nothing is written until the user confirms;
confirmation executes the governed edit and produces an immutable candidate.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import src.settings
from src.agents.orchestration.adapters import GovernedToolActionAdapters
from src.agents.orchestration.contracts import ActionSpec, compile_deterministic_action_plan
from src.agents.runner import _direct_attachment_proposal_answer
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore


def _service(tmp_path, monkeypatch):
    monkeypatch.setattr(src.settings, "CHAT_ATTACHMENTS_ENABLED", True)
    monkeypatch.setattr(
        src.settings, "CHAT_ATTACHMENT_INDEX_DB_PATH", str(tmp_path / "att.db")
    )
    monkeypatch.setattr(
        src.settings, "CHAT_ATTACHMENT_STORAGE_DIR", str(tmp_path / "storage")
    )
    store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
    return AttachmentService(store=store), store


def _docx_bytes() -> bytes:
    from docx import Document

    document = Document()
    document.add_heading("1 概述", level=1)
    document.add_paragraph("概述内容")
    document.add_heading("2 硬件架构", level=1)
    document.add_paragraph("旧架构内容")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _parent(service):
    return service.upload(
        session_id=7, user_id=11, filename="hsi.docx", stream=io.BytesIO(_docx_bytes())
    )


def test_proposal_lifecycle_and_confirmation(tmp_path, monkeypatch):
    service, store = _service(tmp_path, monkeypatch)
    parent = _parent(service)

    proposal = service.create_attachment_edit_proposal(
        session_id=7,
        user_id=11,
        parent_attachment_id=parent.attachment_id,
        heading_path=["2 硬件架构"],
        after="新架构内容一\n新架构内容二",
        evidence_refs=["ev-1"],
        note="测试提案",
    )
    assert proposal.status == "pending"
    assert proposal.heading_path == ["2 硬件架构"]

    # Same pending content is deduplicated instead of stacking proposals.
    duplicate = service.create_attachment_edit_proposal(
        session_id=7,
        user_id=11,
        parent_attachment_id=parent.attachment_id,
        heading_path=["2 硬件架构"],
        after="新架构内容一\n新架构内容二",
        evidence_refs=["ev-1"],
    )
    assert duplicate.proposal_id == proposal.proposal_id

    assert service.get_pending_attachment_proposal(session_id=7, user_id=11) is not None
    assert service.get_pending_attachment_proposal(session_id=7, user_id=99) is None

    revision = service.confirm_attachment_proposal(
        proposal_id=proposal.proposal_id, session_id=7, user_id=11
    )
    assert revision.operation == "replace_section"
    persisted = store.get_attachment_edit_proposal(proposal.proposal_id)
    assert persisted is not None and persisted.status == "confirmed"
    assert persisted.confirmed_revision_id == revision.revision_id

    _record, child_bytes = service.read_source_bytes(
        attachment_id=revision.child_attachment_id, session_id=7, user_id=11
    )
    from docx import Document

    texts = [paragraph.text for paragraph in Document(io.BytesIO(child_bytes)).paragraphs]
    assert "新架构内容一" in texts and "1 概述" in texts
    assert "旧架构内容" not in texts


def test_rejected_proposal_can_be_replaced(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = _parent(service)
    first = service.create_attachment_edit_proposal(
        session_id=7,
        user_id=11,
        parent_attachment_id=parent.attachment_id,
        heading_path=["2 硬件架构"],
        after="第一版内容",
        evidence_refs=["ev-1"],
    )

    service.reject_attachment_proposal(
        proposal_id=first.proposal_id, session_id=7, user_id=11, note="不合适"
    )
    assert service.get_pending_attachment_proposal(session_id=7, user_id=11) is None

    second = service.create_attachment_edit_proposal(
        session_id=7,
        user_id=11,
        parent_attachment_id=parent.attachment_id,
        heading_path=["2 硬件架构"],
        after="第二版内容",
        evidence_refs=["ev-1"],
    )
    assert second.proposal_id != first.proposal_id
    assert second.status == "pending"


def test_plan_executor_creates_a_proposal_only_with_cited_evidence(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = _parent(service)

    def adapters(evidence_ids):
        return GovernedToolActionAdapters(
            tools=[],
            query="把附件第2章替换为新的内容",
            attachment_service=service,
            attachment_session_id=7,
            attachment_user_id=11,
            evidence_ids=frozenset(evidence_ids),
        )

    action = ActionSpec(
        action_id="a1",
        kind="revise_attachment",
        target_ref=f"attachment:{parent.attachment_id}",
        parameters={
            "operation": "replace_section",
            "heading_path": ["2 硬件架构"],
            "after": "模型生成的新章节内容",
            "evidence_refs": ["ev-1"],
        },
        required_capabilities=["attachment.revise"],
        completion_contract="propose a section rewrite for user confirmation",
    )

    blocked = adapters([]).revise_attachment(action, {})
    assert blocked["status"] == "blocked"
    assert blocked["error_code"] == "attachment_proposal_evidence_unresolved"

    unknown = adapters(["ev-other"]).revise_attachment(action, {})
    assert unknown["error_code"] == "attachment_proposal_evidence_unresolved"

    outcome = adapters(["ev-1"]).revise_attachment(action, {})
    assert outcome["status"] == "accepted"
    assert outcome["data"]["proposal_id"].startswith("attachment-proposal-")
    assert service.get_pending_attachment_proposal(session_id=7, user_id=11) is not None


def test_plan_guard_marks_section_requests_without_writing():
    plan = compile_deterministic_action_plan(
        "把附件第2章替换为新的内容", has_attachments=True, has_kb=False
    )
    assert plan.actions == []
    assert plan.unresolved_refs == ["attachment_section_proposal"]


def test_direct_confirmation_and_cancellation_paths(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = _parent(service)
    service.create_attachment_edit_proposal(
        session_id=7,
        user_id=11,
        parent_attachment_id=parent.attachment_id,
        heading_path=["2 硬件架构"],
        after="确认后的新内容",
        evidence_refs=["ev-1"],
    )
    events: list[dict] = []
    rt = SimpleNamespace(
        attachment_service=service,
        attachment_session_id=7,
        attachment_user_id=11,
        kb_name="ADAS",
    )

    answer = _direct_attachment_proposal_answer(rt=rt, query="确认修改", emit_event=events.append)
    assert answer is not None and "已确认章节修改提案" in answer
    assert any(event["type"] == "attachment_revision" for event in events)
    assert service.get_pending_attachment_proposal(session_id=7, user_id=11) is None

    service.create_attachment_edit_proposal(
        session_id=7,
        user_id=11,
        parent_attachment_id=parent.attachment_id,
        heading_path=["2 硬件架构"],
        after="另一版内容",
        evidence_refs=["ev-1"],
    )
    cancelled = _direct_attachment_proposal_answer(
        rt=rt, query="取消修改", emit_event=events.append
    )
    assert cancelled is not None and "已取消" in cancelled
    assert service.get_pending_attachment_proposal(session_id=7, user_id=11) is None

    unrelated = _direct_attachment_proposal_answer(
        rt=rt, query="继续检索知识库", emit_event=events.append
    )
    assert unrelated is None


def test_proposal_requires_docx_heading_and_content(tmp_path, monkeypatch):
    service, _store = _service(tmp_path, monkeypatch)
    parent = _parent(service)

    for kwargs, message in (
        ({"heading_path": [], "after": "x"}, "heading"),
        ({"heading_path": ["2 硬件架构"], "after": ""}, "content"),
    ):
        try:
            service.create_attachment_edit_proposal(
                session_id=7,
                user_id=11,
                parent_attachment_id=parent.attachment_id,
                evidence_refs=["ev-1"],
                **kwargs,
            )
            raise AssertionError("proposal should have been rejected")
        except ValueError as exc:
            assert message in str(exc)

    text_parent = service.upload(
        session_id=7, user_id=11, filename="notes.md", stream=io.BytesIO(b"old\n")
    )
    from src.attachments.models import AttachmentUnsupportedType

    try:
        service.create_attachment_edit_proposal(
            session_id=7,
            user_id=11,
            parent_attachment_id=text_parent.attachment_id,
            heading_path=["标题"],
            after="内容",
            evidence_refs=["ev-1"],
        )
        raise AssertionError("md proposal should have been rejected")
    except AttachmentUnsupportedType:
        pass
