"""Contract tests for the six scoped document-authoring chat tools."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.agents.tools.document_authoring_tools import DocumentAuthoringToolset
from src.document_authoring.chat_context import DocumentContextInput, build_document_context
from src.document_authoring.generation_sessions import GenerationBrief, GenerationSession
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.models import DocumentWorkOrder
from src.document_authoring.template_analysis import (
    TemplateActivationDecision,
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)
from src.pipelines.document_rag.schemas import RequestContext


def _request_context(*, permission: str = "write") -> RequestContext:
    return RequestContext(
        user_id="user-a",
        session_id="chat-a",
        tenant_id="tenant-a",
        metadata={"resource_department_id": 7},
        kb_permissions={"7:hardware": permission},
    )


def _analysis() -> TemplateAnalysis:
    return TemplateAnalysis(
        analysis_id="analysis-a",
        template_version_id="template-a",
        content_hash="template-content-hash",
        format="xlsx",
        status="ready_for_confirmation",
        units=[TemplateAnalysisUnit(
            unit_id="sheet:Review!B2",
            locator={"cell": "B2"},
            label="Controller",
            writable=True,
            structural_role_hint="scalar_input",
        )],
        suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="controller",
            label="Controller",
            target_unit_ids=["sheet:Review!B2"],
            confidence=0.96,
            value_shape="scalar",
        )],
        activation_decision=TemplateActivationDecision(
            status="auto_accepted",
            suggestion_ids=["controller"],
        ),
    )


def _session(*, status: str = "ready_to_generate") -> GenerationSession:
    return GenerationSession(
        session_id="generation-session-a",
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
        status=status,
        brief=GenerationBrief(
            purpose="hardware review",
            confirmed=status == "ready_to_generate",
            confidence=0.95,
        ),
    )


def _order() -> DocumentWorkOrder:
    return DocumentWorkOrder(
        work_order_id="work-order-a",
        tenant_id="tenant-a",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        knowledge_base_id="kb-hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id="snapshot-a",
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
        template_schema_id="template-schema-a",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="internal_harness",
        harness_policy_id="policy-a",
        harness_policy_version="1",
        created_by="user-a",
    )


class _Pipeline:
    def __init__(self) -> None:
        self.analysis = _analysis()
        self.session = _session()
        self.order = _order()

    def get_document_template_analysis_for_review(self, _ctx, *, analysis_id):
        assert analysis_id == "analysis-a"
        return self.analysis

    def create_document_generation_session(self, _ctx, **_kwargs):
        return self.session

    def answer_document_generation_session(self, _ctx, session_id, **_kwargs):
        assert session_id == self.session.session_id
        return self.session

    def confirm_document_generation_session(self, _ctx, session_id):
        assert session_id == self.session.session_id
        return self.session

    def get_document_generation_session(self, _ctx, session_id):
        assert session_id == self.session.session_id
        return self.session

    def create_knowledge_base_document_work_order(self, _ctx, **_kwargs):
        return self.order

    def get_document_run_status(self, work_order_id, _ctx):
        assert work_order_id == self.order.work_order_id
        return {
            "work_order_id": work_order_id,
            "status": "queued",
            "phase": "retrieving",
            "scope_type": "knowledge_base",
            "knowledge_base_name": "hardware",
            "target_format": "xlsx",
            "unit_statuses": {},
            "next_actions": ["poll_status"],
            "harness_run": {},
            "artifacts": [],
        }


def _toolset(tmp_path, *, permission: str = "write", context=None, event_sink=None) -> DocumentAuthoringToolset:
    ctx = _request_context(permission=permission)
    document_context = context or build_document_context(
        DocumentContextInput(
            analysis_id="analysis-a",
            template_version_id="template-a",
            knowledge_base_name="hardware",
            client_request_id="client-a",
        ),
        ctx=ctx,
        expected_kb="hardware",
    )
    return DocumentAuthoringToolset(
        pipeline=_Pipeline(),
        ctx=ctx,
        context=document_context,
        chat_session_id="chat-a",
        job_store=DocumentAuthoringJobStore(str(tmp_path / "jobs.db")),
        event_sink=event_sink,
    )


def test_all_six_tools_are_typed_scoped_and_outer_serialized(tmp_path):
    toolset = _toolset(tmp_path)

    analysis_result = toolset.get_document_template_analysis("analysis-a")
    assert analysis_result.status == "succeeded"
    assert analysis_result.data["analysis_id"] == "analysis-a"

    session_result = toolset.start_document_generation_session(purpose="review")
    assert session_result.generation_session_id == "generation-session-a"
    assert toolset.answer_clarification(
        "generation-session-a", "purpose", "review"
    ).status == "succeeded"
    assert toolset.confirm_generation_session("generation-session-a").status == "succeeded"

    queued = toolset.create_document_work_order(
        document_schema_id="schema-a",
        document_schema_version="1",
        generation_session_id="generation-session-a",
    )
    assert queued.status == "succeeded"
    assert queued.job_id

    status = toolset.get_document_generation_status("work-order-a")
    assert status.status == "succeeded"
    assert status.data["job"]["job_id"] == queued.job_id

    names = {tool.name for tool in toolset.as_tools()}
    assert names == {
        "get_document_template_analysis",
        "start_document_generation_session",
        "answer_clarification",
        "confirm_generation_session",
        "create_document_work_order",
        "get_document_generation_status",
    }
    outer = next(
        tool for tool in toolset.as_tools()
        if tool.name == "get_document_template_analysis"
    )
    encoded = outer.invoke({"analysis_id": "analysis-a"})
    assert isinstance(encoded, str)
    assert json.loads(encoded)["status"] == "succeeded"


def test_read_only_or_expired_context_cannot_mutate_but_can_read(tmp_path):
    read_only = _toolset(tmp_path, permission="read")
    assert read_only.get_document_template_analysis("analysis-a").status == "succeeded"
    with pytest.raises(PermissionError, match="write permission"):
        read_only.start_document_generation_session()

    expired_context = read_only.context.model_copy(update={
        "expiry": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    })
    expired = _toolset(tmp_path, permission="read", context=expired_context)
    assert expired.get_document_template_analysis("analysis-a").status == "succeeded"
    with pytest.raises(PermissionError, match="write permission"):
        expired.confirm_generation_session("generation-session-a")


def test_document_card_emitted_on_create_work_order_and_not_on_rejection(tmp_path):
    sink = []
    toolset = _toolset(tmp_path, event_sink=sink.append)

    result = toolset.create_document_work_order(
        document_schema_id="schema-a",
        document_schema_version="1",
        generation_session_id="generation-session-a",
    )
    assert result.status == "succeeded"
    assert len(sink) == 1
    assert sink[0]["type"] == "document_card"
    assert sink[0]["card"]["kind"] == "work_order_created"
    assert sink[0]["card"]["work_order_id"] == "work-order-a"
    assert sink[0]["card"]["status"] == "queued"
    assert sink[0]["card"]["kb_name"] == "hardware"
    assert "get_document_generation_status" in sink[0]["card"]["next_actions"]
    assert set(sink[0]["card"]) <= {
        "kind", "work_order_id", "generation_session_id", "status", "next_actions", "kb_name",
        "target_format", "artifacts",
    }

    rejected = toolset.create_document_work_order(
        document_schema_id="schema-a",
        document_schema_version="1",
        generation_session_id="generation-session-a",
        template_version_id="other-template",
    )
    assert rejected.status == "rejected"
    assert len(sink) == 1


def test_document_card_emitted_on_status_tool(tmp_path):
    sink = []
    toolset = _toolset(tmp_path, event_sink=sink.append)

    result = toolset.get_document_generation_status("work-order-a")
    assert result.status == "succeeded"
    assert len(sink) == 1
    assert sink[0]["type"] == "document_card"
    card = sink[0]["card"]
    assert card["kind"] == "work_order_status"
    assert card["work_order_id"] == "work-order-a"
    assert card["status"] == "queued"
    assert card["kb_name"] == "hardware"
    assert card["next_actions"] == ["poll_status"]
    assert set(card) <= {
        "kind", "work_order_id", "generation_session_id", "status", "next_actions", "kb_name",
        "target_format", "artifacts",
    }


def test_status_card_carries_sanitized_artifacts_and_target_format(tmp_path):
    sink = []
    toolset = _toolset(tmp_path, event_sink=sink.append)
    toolset.pipeline.get_document_run_status = lambda work_order_id, _ctx: {
        "work_order_id": work_order_id,
        "status": "complete",
        "phase": "completed",
        "scope_type": "knowledge_base",
        "knowledge_base_name": "hardware",
        "target_format": "xlsx",
        "unit_statuses": {},
        "next_actions": ["view_result"],
        "harness_run": {},
        "artifacts": [
            {
                "artifact_id": f"artifact-{index}",
                "stage": f"stage-{index}",
                "validation_report_id": "report-a",
                "validity_status": "valid",
                "policy_status": "ok",
            }
            for index in range(9)
        ],
    }

    result = toolset.get_document_generation_status("work-order-a")

    assert result.status == "succeeded"
    assert result.data["target_format"] == "xlsx"
    assert result.data["artifacts"] == [
        {"artifact_id": f"artifact-{index}", "stage": f"stage-{index}"} for index in range(8)
    ]
    assert len(sink) == 1
    card = sink[0]["card"]
    assert card["target_format"] == "xlsx"
    assert card["artifacts"] == [
        {"artifact_id": f"artifact-{index}", "stage": f"stage-{index}"} for index in range(8)
    ]
    assert set(card) <= {
        "kind", "work_order_id", "generation_session_id", "status", "next_actions", "kb_name",
        "target_format", "artifacts",
    }


def test_status_card_omits_artifacts_and_target_format_when_absent(tmp_path):
    sink = []
    toolset = _toolset(tmp_path, event_sink=sink.append)
    toolset.pipeline.get_document_run_status = lambda work_order_id, _ctx: {
        "work_order_id": work_order_id,
        "status": "queued",
        "phase": "retrieving",
        "scope_type": "knowledge_base",
        "knowledge_base_name": "hardware",
        "unit_statuses": {},
        "next_actions": ["poll_status"],
        "harness_run": {},
    }

    result = toolset.get_document_generation_status("work-order-a")

    assert result.status == "succeeded"
    assert result.data["artifacts"] == []
    assert result.data["target_format"] is None
    assert len(sink) == 1
    card = sink[0]["card"]
    assert "artifacts" not in card
    assert "target_format" not in card

    toolset.pipeline.get_document_run_status = lambda work_order_id, _ctx: {
        "work_order_id": work_order_id,
        "status": "complete",
        "knowledge_base_name": "other-kb",
    }
    rejected = toolset.get_document_generation_status("work-order-a")
    assert rejected.status == "rejected"
    assert len(sink) == 1


def test_document_card_session_kinds_and_analysis_silent(tmp_path):
    sink = []
    toolset = _toolset(tmp_path, event_sink=sink.append)

    assert toolset.get_document_template_analysis("analysis-a").status == "succeeded"
    assert sink == []

    toolset.start_document_generation_session(purpose="review")
    toolset.answer_clarification("generation-session-a", "purpose", "review")
    toolset.confirm_generation_session("generation-session-a")
    assert [evt["card"]["kind"] for evt in sink] == ["generation_session"] * 3
    assert all(
        evt["card"]["generation_session_id"] == "generation-session-a" for evt in sink
    )
    assert all(
        set(evt["card"]) <= {
            "kind", "work_order_id", "generation_session_id", "status", "next_actions", "kb_name",
            "target_format", "artifacts",
        }
        for evt in sink
    )


def test_document_card_sink_failure_never_breaks_tool_result(tmp_path):
    def _boom(_evt):
        raise RuntimeError("sink down")

    toolset = _toolset(tmp_path, event_sink=_boom)
    result = toolset.create_document_work_order(
        document_schema_id="schema-a",
        document_schema_version="1",
        generation_session_id="generation-session-a",
    )
    assert result.status == "succeeded"
    assert result.work_order_id == "work-order-a"


def test_client_context_cannot_cross_owner_or_knowledge_base(tmp_path):
    raw = DocumentContextInput(
        analysis_id="analysis-a",
        template_version_id="template-a",
        knowledge_base_name="other-kb",
        client_request_id="client-a",
    )
    with pytest.raises(PermissionError, match="knowledge base mismatch"):
        build_document_context(
            raw,
            ctx=_request_context(),
            expected_kb="hardware",
        )

    toolset = _toolset(tmp_path)
    with pytest.raises(PermissionError, match="owner or tenant"):
        toolset.context.assert_scope(
            ctx=SimpleNamespace(
                user_id="user-b",
                tenant_id="tenant-a",
                has_kb_permission=lambda *_args: True,
            ),
            expected_kb="hardware",
        )
