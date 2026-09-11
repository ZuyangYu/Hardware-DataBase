"""Contract tests for the scoped document-authoring chat tools."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import src.settings

from src.agents.tools.document_authoring_tools import DocumentAuthoringToolset
from src.agents.tools.document_authoring_tools import GenerateDocumentArgs
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
        task_id="document-task-a",
    )


class _Pipeline:
    def __init__(self) -> None:
        self.analysis = _analysis()
        self.session = _session()
        self.order = _order()
        self.document_generation = SimpleNamespace(
            store=SimpleNamespace(
                get_template=lambda _template_id: SimpleNamespace(
                    template_schema_id="schema-a",
                    template_schema_version="1",
                    status="approved",
                    format="xlsx",
                ),
            ),
        )

    def get_document_template_analysis_for_review(self, _ctx, *, analysis_id):
        assert analysis_id == "analysis-a"
        return self.analysis

    def create_document_generation_session(self, _ctx, **_kwargs):
        return self.session

    def prepare_knowledge_base_document_generation(self, _ctx, **kwargs):
        self.prepare_kwargs = kwargs
        return {
            "stage": "ready",
            "work_order_id": self.order.work_order_id,
            "task_id": self.order.task_id,
        }

    def answer_document_generation_session(self, _ctx, session_id, **_kwargs):
        assert session_id == self.session.session_id
        self.answer_kwargs = _kwargs
        return self.session

    def confirm_document_generation_session(self, _ctx, session_id):
        assert session_id == self.session.session_id
        return self.session

    def get_document_generation_session(self, _ctx, session_id):
        assert session_id == self.session.session_id
        return self.session

    def create_document_revision(self, _ctx, task_id, **kwargs):
        return {
            "revision_id": "revision-a",
            "task_id": task_id,
            "parent_artifact_id": kwargs["parent_artifact_id"],
            "status": "generating",
            "work_order_id": "work-order-a",
            "regeneration": {
                "work_order_id": "work-order-b",
                "run_id": "run-revision-a",
                "status": "queued",
            },
        }

    def create_knowledge_base_document_work_order(self, _ctx, **_kwargs):
        return self.order

    def get_document_run_status(self, work_order_id, _ctx):
        assert work_order_id == self.order.work_order_id
        return {
            "work_order_id": work_order_id,
            "task_id": self.order.task_id,
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
        "generation-session-a", "purpose", "review", client_request_id="clarification-request-1"
    ).status == "succeeded"
    assert toolset.pipeline.answer_kwargs["client_request_id"] == "clarification-request-1"
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
        "generate_document_from_template",
        "get_document_template_analysis",
        "start_document_generation_session",
        "answer_clarification",
        "confirm_generation_session",
        "create_document_work_order",
        "create_document_revision",
        "get_document_generation_status",
    }
    outer = next(
        tool for tool in toolset.as_tools()
        if tool.name == "get_document_template_analysis"
    )
    encoded = outer.invoke({"analysis_id": "analysis-a"})
    assert isinstance(encoded, str)
    assert json.loads(encoded)["status"] == "succeeded"


def test_chat_can_create_a_governed_task_bound_revision(tmp_path):
    toolset = _toolset(tmp_path)

    result = toolset.create_document_revision(
        task_id="document-task-a",
        parent_artifact_id="artifact-a",
        request_type="section_update",
        request="更新评审结论",
        changed_sections=["conclusion"],
        client_request_id="revision-request-a",
    )

    assert result.status == "succeeded"
    assert result.task_id == "document-task-a"
    assert result.work_order_id == "work-order-b"
    assert result.data["revision_id"] == "revision-a"
    assert result.data["regeneration"]["work_order_id"] == "work-order-b"
    assert "get_document_generation_status" in result.next_actions
    assert "后台" in result.message


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
    assert sink[0]["card"]["task_id"] == "document-task-a"
    assert "get_document_generation_status" in sink[0]["card"]["next_actions"]
    assert set(sink[0]["card"]) <= {
        "kind", "task_id", "work_order_id", "generation_session_id", "status", "next_actions", "kb_name",
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


def test_create_work_order_job_keeps_the_document_task_association(tmp_path):
    toolset = _toolset(tmp_path)

    result = toolset.create_document_work_order(
        document_schema_id="schema-a",
        document_schema_version="1",
        generation_session_id="generation-session-a",
    )

    assert result.status == "succeeded"
    assert result.task_id == "document-task-a"
    job = toolset.job_store.get(result.job_id)
    assert job is not None
    assert job.task_id == "document-task-a"


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
    assert card["task_id"] == "document-task-a"
    assert card["next_actions"] == ["poll_status"]
    assert set(card) <= {
        "kind", "task_id", "work_order_id", "generation_session_id", "status", "next_actions", "kb_name",
        "target_format", "artifacts",
    }


def test_status_card_carries_sanitized_artifacts_and_target_format(tmp_path):
    sink = []
    toolset = _toolset(tmp_path, event_sink=sink.append)
    toolset.pipeline.get_document_run_status = lambda work_order_id, _ctx: {
        "work_order_id": work_order_id,
        "task_id": "document-task-a",
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
    assert result.task_id == "document-task-a"
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
        "kind", "task_id", "work_order_id", "generation_session_id", "status", "next_actions", "kb_name",
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


def test_answer_clarification_returns_the_validation_reason(tmp_path):
    toolset = _toolset(tmp_path)

    def reject(_ctx, _session_id, **_kwargs):
        raise ValueError("invalid missing-data policy answer: A")

    toolset.pipeline.answer_document_generation_session = reject

    result = toolset.answer_clarification(
        "generation-session-a", "missing_data_policy", "A",
    )

    assert result.status == "rejected"
    assert result.error_code == "clarification_rejected"
    assert "invalid missing-data policy answer: A" in result.message


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


def test_direct_generation_derives_schema_and_queues_template_artifact(tmp_path):
    """A direct user request must create the real template work order.

    The model should not have to guess the schema id/version emitted by
    template activation.  The server owns that lookup and queues the durable
    document-generation job, rather than exporting the chat answer.
    """
    sink = []
    toolset = _toolset(tmp_path, event_sink=sink.append)

    result = toolset.generate_document_from_template(
        purpose="参考模板，根据知识库生成新的 ICD 文档",
    )

    assert result.status == "succeeded"
    assert result.work_order_id == "work-order-a"
    assert result.task_id == "document-task-a"
    assert result.job_id
    assert toolset.pipeline.prepare_kwargs["document_schema_id"] == "schema-a"
    assert toolset.pipeline.prepare_kwargs["document_schema_version"] == "1"
    assert toolset.pipeline.prepare_kwargs["generation_session_id"] == "generation-session-a"
    assert sink[-1]["card"]["kind"] == "work_order_created"


def test_template_generation_output_contract_accepts_native_format(tmp_path):
    args = GenerateDocumentArgs(output_format="xlsx")
    assert args.output_format == "xlsx"

    result = _toolset(tmp_path).generate_document_from_template(
        purpose="参考模板生成 ICD 文档",
        output_format="xlsx",
    )

    assert result.status == "succeeded"
    assert result.data["output_contract"]["requested_format"] == "xlsx"
    assert result.data["output_contract"]["conversion_status"] == "not_required"


def test_template_generation_output_contract_rejects_unavailable_conversion(tmp_path):
    result = _toolset(tmp_path).generate_document_from_template(
        purpose="参考模板生成 ICD 文档并导出 PDF",
        output_format="pdf",
    )

    assert result.status == "rejected"
    assert result.error_code == "template_output_conversion_not_enabled"
    assert result.data["output_contract"] == {
        "native_format": "xlsx",
        "requested_format": "pdf",
        "conversion_status": "not_enabled",
    }


def test_template_generation_output_contract_queues_supported_conversion(tmp_path):
    toolset = _toolset(tmp_path)
    toolset.pipeline.submit_document_artifact_conversion = lambda *_args, **_kwargs: None
    result = toolset.generate_document_from_template(
        purpose="参考模板生成 ICD 文档并导出 PDF",
        output_format="pdf",
    )

    assert result.status == "succeeded"
    assert result.data["output_contract"]["requested_format"] == "pdf"
    assert result.data["output_contract"]["conversion_status"] == "pending_native_artifact"
    job = toolset.job_store.get(result.job_id)
    assert job is not None
    assert job.payload["requested_output_format"] == "pdf"


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


# ---------------------------------------------------------------------------
# Phase 1 Task 11: v2 plan-proposal/confirmation tool surface
# ---------------------------------------------------------------------------


def _v2_session(*, status: str = "awaiting_plan") -> GenerationSession:
    return GenerationSession(
        session_id="generation-session-a",
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
        contract_version="output_spec_v1",
        status=status,
        output_spec_id="output-spec-a",
        output_spec_version=3,
        output_spec_draft={
            "output_spec_id": "output-spec-a",
            "version": 3,
            "purpose": "生成评审报告",
            "document_type": "report",
            "artifact": {"deliverables": [{"format": "xlsx", "role": "primary"}]},
            "layout_source": {
                "mode": "provided_template",
                "template_version_id": "template-a",
                "template_schema_id": "schema-a",
                "template_schema_version": "1",
            },
            "outline": [{"unit_id": "summary", "kind": "section", "required": True}],
            "missing_data_policy": "mark_tbd",
            "inference_policy": "forbid",
            "approval_policy_id": "default-document-v1",
        },
    )


def _v2_pipeline() -> _Pipeline:
    pipeline = _Pipeline()
    pipeline.v2_session = _v2_session()
    pipeline.create_kwargs = {}
    pipeline.proposal = {
        "session_id": "generation-session-a",
        "task_id": "document-task-a",
        "document_plan_id": "plan-a",
        "document_plan_version": 1,
        "plan_hash": "sha256:plan",
        "output_spec_id": "output-spec-a",
        "output_spec_version": 3,
        "output_spec_hash": "sha256:spec",
        "status": "awaiting_plan_confirmation",
        "executable": True,
        "deliverables": [{"format": "xlsx", "role": "primary"}],
        "layout_summary": {"mode": "provided_template"},
        "outline_count": 2,
        "table_count": 1,
        "source_summary": {"knowledge_base_count": 1},
        "policies": {"missing_data": "mark_tbd"},
        "warnings": [],
        "blockers": [],
        "next_actions": ["confirm_document_plan"],
    }
    pipeline.submission = {
        "submission_id": "submission-a",
        "status": "pending",
        "session_id": "generation-session-a",
        "task_id": "document-task-a",
        "document_plan_id": "plan-a",
        "document_plan_version": 1,
        "plan_hash": "sha256:plan",
        "work_order_id": None,
        "job_id": None,
        "next_actions": ["await_generation"],
    }

    def _create_v2_session(_ctx, **kwargs):
        pipeline.create_kwargs = kwargs
        return pipeline.v2_session

    def _propose(_ctx, session_id, **kwargs):
        pipeline.propose_kwargs = {"session_id": session_id, **kwargs}
        return dict(pipeline.proposal, session_id=session_id)

    def _confirm(_ctx, session_id, **kwargs):
        pipeline.confirm_kwargs = {"session_id": session_id, **kwargs}
        return dict(pipeline.submission, session_id=session_id)

    def _answer(_ctx, session_id, **kwargs):
        pipeline.answer_v2_kwargs = {"session_id": session_id, **kwargs}
        return pipeline.v2_session

    def _task_projection(_ctx, task_id):
        pipeline.task_projection_id = task_id
        return {
            "task_id": task_id,
            "status": "awaiting_plan_confirmation",
            "knowledge_base_name": "hardware",
            "planning_state": {
                "document_plan_id": "plan-a",
                "plan_hash": "sha256:plan",
                "output_spec_hash": "sha256:spec",
            },
            "next_actions": ["confirm_document_plan"],
        }

    pipeline.create_document_generation_session = _create_v2_session
    pipeline.create_document_plan_proposal = _propose
    pipeline.confirm_document_plan = _confirm
    pipeline.answer_document_generation_session = _answer
    pipeline.get_document_task_projection = _task_projection

    def _get_v2_session(_ctx, session_id):
        assert session_id == pipeline.v2_session.session_id
        return pipeline.v2_session

    pipeline.get_document_generation_session = _get_v2_session
    return pipeline


def _v2_toolset(tmp_path, monkeypatch, *, template=True, permission="write"):
    import src.settings

    monkeypatch.setattr(src.settings, "DOCUMENT_PLANNING_V2_ENABLED", True)
    monkeypatch.setattr(
        src.settings,
        "DOCUMENT_RISK_BASED_PLAN_GATE_ENABLED",
        False,
        raising=False,
    )
    ctx = _request_context(permission=permission)
    if template:
        context = build_document_context(
            DocumentContextInput(
                analysis_id="analysis-a",
                template_version_id="template-a",
                knowledge_base_name="hardware",
                client_request_id="client-a",
            ),
            ctx=ctx,
            expected_kb="hardware",
        )
    else:
        from src.document_authoring.chat_context import DocumentAuthoringContextInput

        context = build_document_context(
            DocumentAuthoringContextInput(
                knowledge_base_name="hardware",
                client_request_id="client-a",
            ),
            ctx=ctx,
            expected_kb="hardware",
        )
    return DocumentAuthoringToolset(
        pipeline=_v2_pipeline(),
        ctx=ctx,
        context=context,
        chat_session_id="chat-a",
        job_store=DocumentAuthoringJobStore(str(tmp_path / "jobs.db")),
    )


def test_v2_tool_surface_swaps_work_order_for_plan_confirmation(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)

    names = {tool.name for tool in toolset.as_tools()}
    assert "create_document_work_order" not in names
    assert "generate_document_from_template" in names
    assert {"propose_document_plan", "confirm_document_plan", "get_document_task_status"} <= names


def test_v2_direct_template_request_returns_proposal_not_work_order(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)

    result = toolset.generate_document_from_template(
        purpose="参考模板生成 ICD 文档",
        use_recommended_defaults=True,
    )

    assert result.status == "waiting_human"
    assert "confirm_document_plan" in result.next_actions
    assert result.data["proposal"]["plan_hash"] == "sha256:plan"
    assert result.data["proposal"]["output_spec_hash"] == "sha256:spec"
    # No Work Order preflight and no job may be created before confirmation.
    assert not hasattr(toolset.pipeline, "prepare_kwargs")
    assert toolset.job_store.list_pending(limit=10) == []
    # Recommendations may be applied, but never a silent confirmation.
    assert toolset.pipeline.create_kwargs.get("contract_version") == "output_spec_v1"
    assert "auto_confirm_recommended" not in toolset.pipeline.create_kwargs


def test_v2_low_risk_direct_request_auto_confirms_the_hash_bound_plan(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)
    monkeypatch.setattr(
        src.settings,
        "DOCUMENT_RISK_BASED_PLAN_GATE_ENABLED",
        True,
        raising=False,
    )

    result = toolset.generate_document_from_template(
        purpose="参考模板生成 ICD 文档",
        use_recommended_defaults=True,
    )

    assert result.status == "succeeded"
    assert result.data["confirmation_required"] is False
    assert result.data["status"] == "pending"
    assert result.next_actions == ["await_generation"]
    assert toolset.pipeline.confirm_kwargs["expected_output_spec_hash"] == "sha256:spec"
    assert toolset.pipeline.confirm_kwargs["expected_plan_hash"] == "sha256:plan"


def test_v2_risky_direct_request_keeps_confirmation_and_explains_why(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)
    monkeypatch.setattr(
        src.settings,
        "DOCUMENT_RISK_BASED_PLAN_GATE_ENABLED",
        True,
        raising=False,
    )
    toolset.pipeline.proposal["warnings"] = ["template_mapping_requires_review"]

    result = toolset.generate_document_from_template(
        purpose="参考模板生成 ICD 文档",
        use_recommended_defaults=True,
    )

    assert result.status == "waiting_human"
    assert result.data["confirmation_required"] is True
    assert "proposal_has_warnings" in result.data["confirmation_reasons"]
    assert not hasattr(toolset.pipeline, "confirm_kwargs")


def test_v2_direct_template_request_surfaces_pending_intake_question(tmp_path, monkeypatch):
    """An incomplete direct request asks for intake data before compiling."""
    sink = []
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.event_sink = sink.append
    toolset.pipeline.v2_session = toolset.pipeline.v2_session.model_copy(update={
        "output_spec_draft": {
            "output_spec_id": "output-spec-a",
            "version": 3,
            "purpose": "参考模板生成文档",
            "document_type": "report",
            "layout_source": {
                "mode": "provided_template",
                "template_version_id": "template-a",
                "template_schema_id": "schema-a",
                "template_schema_version": "1",
            },
            "missing_data_policy": "mark_tbd",
            "inference_policy": "forbid",
            "approval_policy_id": "default-document-v1",
        },
    })
    toolset.pipeline.create_document_plan_proposal = lambda *_args, **_kwargs: pytest.fail(
        "an incomplete output spec must not be compiled"
    )

    result = toolset.generate_document_from_template(
        purpose="参考模板生成文档",
        use_recommended_defaults=True,
    )

    assert result.status == "waiting_human"
    assert result.data["status"] == "needs_clarification"
    assert result.data["question_id"] == "deliverables"
    assert result.data["content"] == "希望交付什么格式？"
    assert result.next_actions == ["answer_clarification"]
    # The card channel is progress-only: the question lives in the chat turn,
    # so the popup must not duplicate it as a second input surface.
    card = sink[-1]["card"]
    assert card["kind"] == "generation_session"
    assert card["status"] == "needs_clarification"
    assert card["generation_session_id"] == "generation-session-a"
    assert "question_id" not in card
    assert "content" not in card
    assert "options" not in card


def test_v2_direct_request_honors_persisted_content_confirmation_question(tmp_path, monkeypatch):
    """A persisted clarification question wins over draft-derived planning."""
    from src.document_authoring.generation_sessions import ClarificationMessage

    toolset = _v2_toolset(tmp_path, monkeypatch)
    session = toolset.pipeline.v2_session.model_copy(update={
        "status": "needs_clarification",
        "messages": [ClarificationMessage(
            role="assistant",
            content="请确认文档正文范围。",
            question_id="content_confirmation",
            options=["仅使用已确认资料", "允许补充资料"],
            reason="正文范围需要人工确认。",
        )],
    })
    toolset.pipeline.v2_session = session
    toolset.pipeline.create_document_plan_proposal = lambda *_args, **_kwargs: pytest.fail(
        "a persisted clarification must not be compiled into a plan"
    )

    result = toolset.generate_document_from_template(
        purpose="参考模板生成文档",
        use_recommended_defaults=True,
    )

    assert result.status == "waiting_human"
    assert result.data["status"] == "needs_clarification"
    assert result.data["question_id"] == "content_confirmation"
    assert result.data["content"] == "请确认文档正文范围。"
    assert result.data["options"] == ["仅使用已确认资料", "允许补充资料"]
    assert result.next_actions == ["answer_clarification"]


def test_v2_pending_question_does_not_revive_answered_persisted_question(tmp_path, monkeypatch):
    """An answer recorded after a question makes that question ineligible."""
    from src.document_authoring.generation_sessions import ClarificationMessage

    toolset = _v2_toolset(tmp_path, monkeypatch)
    session = toolset.pipeline.v2_session.model_copy(update={
        "status": "needs_clarification",
        "messages": [
            ClarificationMessage(
                role="assistant", content="请确认文档正文范围。",
                question_id="content_confirmation",
            ),
            ClarificationMessage(
                role="user", content="仅使用已确认资料。",
                question_id="content_confirmation", answer="仅使用已确认资料。",
            ),
        ],
    })

    assert DocumentAuthoringToolset._pending_output_spec_question(session) is None


def test_direct_request_reports_unapproved_template_as_human_waiting_state(tmp_path, monkeypatch):
    """Template mapping review is surfaced without pretending the tool can approve it."""
    monkeypatch.setattr(src.settings, "DOCUMENT_PLANNING_V2_ENABLED", False)
    toolset = _toolset(tmp_path)
    toolset.pipeline.document_generation.store.get_template = lambda _template_id: SimpleNamespace(
        template_schema_id="schema-a",
        template_schema_version="1",
        status="pending_review",
        format="xlsx",
    )

    result = toolset.generate_document_from_template(
        purpose="参考模板生成文档",
        use_recommended_defaults=True,
    )

    assert result.status == "rejected"
    assert result.error_code == "template_not_approved"
    assert "模板映射" in result.message
    assert "人工" in result.message
    assert result.next_actions == ["get_document_template_analysis"]
    assert result.data["mapping_status"] == "pending_review"


def test_v2_direct_request_can_open_intake_for_draft_template(tmp_path, monkeypatch):
    """v2 discovery may plan against a draft template without approving it."""
    from src.document_authoring.generation_sessions import ClarificationMessage

    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.pipeline.document_generation.store.get_template = lambda _template_id: SimpleNamespace(
        template_schema_id="",
        template_schema_version="",
        status="pending_review",
        format="xlsx",
    )
    toolset.pipeline.v2_session = toolset.pipeline.v2_session.model_copy(update={
        "status": "needs_clarification",
        "messages": [ClarificationMessage(
            role="assistant", content="请确认文档正文范围。",
            question_id="content_confirmation",
        )],
    })
    toolset.pipeline.create_document_plan_proposal = lambda *_args, **_kwargs: pytest.fail(
        "draft-template intake must stop for clarification"
    )

    result = toolset.generate_document_from_template(purpose="发现模板生成文档")

    assert result.status == "waiting_human"
    assert result.data["status"] == "needs_clarification"
    assert result.next_actions == ["answer_clarification"]
    assert not hasattr(toolset.pipeline, "confirm_kwargs")


def test_v2_direct_request_surfaces_persisted_blocked_mapping_state(tmp_path, monkeypatch):
    """An unrepairable mapping block is reported without restarting intake."""
    from src.document_authoring.generation_sessions import ClarificationMessage

    toolset = _v2_toolset(tmp_path, monkeypatch)
    blocked = toolset.pipeline.v2_session.model_copy(update={
        "status": "blocked",
        "messages": [ClarificationMessage(
            role="assistant",
            content="模板映射无法可靠确定，当前无法继续。",
            question_id="",
            reason="template_mapping_unresolved",
        )],
    })
    toolset.pipeline.v2_session = blocked
    toolset.context = toolset.context.model_copy(update={
        "generation_session_id": "generation-session-a",
    })
    toolset.pipeline.create_document_generation_session = lambda *_args, **_kwargs: pytest.fail(
        "blocked intake must not create a replacement session"
    )
    toolset.pipeline.create_document_plan_proposal = lambda *_args, **_kwargs: pytest.fail(
        "blocked intake must not compile a replacement plan"
    )

    result = toolset.generate_document_from_template(purpose="继续生成文档")

    assert result.status == "waiting_human"
    assert result.error_code == "template_mapping_unresolved"
    assert result.data["status"] == "blocked"
    assert result.message == "模板映射无法可靠确定，当前无法继续。"
    assert result.next_actions == []


def test_legacy_direct_request_still_rejects_draft_template(tmp_path, monkeypatch):
    """The legacy artifact path remains fail-closed until template approval."""
    monkeypatch.setattr(src.settings, "DOCUMENT_PLANNING_V2_ENABLED", False)
    toolset = _toolset(tmp_path)
    toolset.pipeline.document_generation.store.get_template = lambda _template_id: SimpleNamespace(
        template_schema_id="schema-a", template_schema_version="1",
        status="pending_review", format="xlsx",
    )

    result = toolset.generate_document_from_template(purpose="发现模板生成文档")

    assert result.status == "rejected"
    assert result.error_code == "template_not_approved"


def test_v2_direct_template_request_reuses_the_attached_intake_session(tmp_path, monkeypatch):
    """The conversation owns one intake session; a direct request must not fork it.

    Recreating a session per tool call discarded the already-recorded
    clarification answers and made the flow re-ask them.
    """
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.context = toolset.context.model_copy(update={
        "generation_session_id": "generation-session-a",
    })

    result = toolset.generate_document_from_template(
        purpose="参考模板生成文档",
        use_recommended_defaults=True,
    )

    assert result.status == "waiting_human"
    assert "confirm_document_plan" in result.next_actions
    # The existing session was reused: no new session, no repeated
    # recommendation answer, and the proposal binds the current version.
    assert toolset.pipeline.create_kwargs == {}
    assert not hasattr(toolset.pipeline, "answer_v2_kwargs")
    assert toolset.pipeline.propose_kwargs["session_id"] == "generation-session-a"
    assert toolset.pipeline.propose_kwargs["expected_output_spec_version"] == 3


def test_v2_direct_request_applies_recommendations_once_for_a_fresh_session(tmp_path, monkeypatch):
    """A fresh session gets the safe defaults exactly once, then never again."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    draft = dict(toolset.pipeline.v2_session.output_spec_draft)
    for key in ("missing_data_policy", "inference_policy", "approval_policy_id"):
        draft.pop(key, None)
    fresh = toolset.pipeline.v2_session.model_copy(update={
        "output_spec_draft": draft,
        "output_spec_version": 4,
    })
    pipeline = toolset.pipeline
    pipeline.v2_session = fresh

    def _answer(_ctx, session_id, **kwargs):
        pipeline.answer_v2_kwargs = {"session_id": session_id, **kwargs}
        revised_draft = dict(fresh.output_spec_draft)
        revised_draft.update({
            "missing_data_policy": "mark_tbd",
            "inference_policy": "forbid",
            "approval_policy_id": "default-document-v1",
        })
        answered = fresh.model_copy(update={
            "output_spec_draft": revised_draft,
            "output_spec_version": 5,
        })
        # Simulate the durable store update so the post-answer re-read sees it.
        pipeline.v2_session = answered
        return answered

    pipeline.answer_document_generation_session = _answer

    result = toolset.generate_document_from_template(
        purpose="参考模板生成文档",
        use_recommended_defaults=True,
    )

    assert result.status == "waiting_human"
    assert pipeline.answer_v2_kwargs["question_id"] == "recommendations"
    # The proposal binds the version produced by the advisory answer.
    assert pipeline.propose_kwargs["expected_output_spec_version"] == 5


def test_v2_proposal_failure_surfaces_the_pipeline_reason(tmp_path, monkeypatch):
    """A rejected proposal carries the real pipeline reason, never a canned hint."""
    toolset = _v2_toolset(tmp_path, monkeypatch)

    def _boom(_ctx, _session_id, **_kwargs):
        raise ValueError("generation session is not awaiting a plan proposal")

    toolset.pipeline.create_document_plan_proposal = _boom

    result = toolset.generate_document_from_template(
        purpose="参考模板生成文档",
        use_recommended_defaults=True,
    )

    assert result.status == "rejected"
    assert result.error_code == "plan_proposal_rejected"
    assert "generation session is not awaiting a plan proposal" in result.message
    assert "pending clarification" not in result.message
    assert result.next_actions == ["propose_document_plan"]


def test_v2_propose_document_plan_tool_forwards_session_and_request_id(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)

    result = toolset.propose_document_plan("generation-session-a")

    assert result.status == "succeeded"
    assert toolset.pipeline.propose_kwargs["session_id"] == "generation-session-a"
    assert toolset.pipeline.propose_kwargs["expected_output_spec_version"] == 3
    assert toolset.pipeline.propose_kwargs["client_request_id"]
    assert result.data["plan_hash"] == "sha256:plan"


def test_v2_confirm_document_plan_requires_explicit_hashes(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)

    missing = toolset.confirm_document_plan(
        "generation-session-a",
        expected_output_spec_hash="",
        expected_plan_hash="sha256:plan",
        client_request_id="confirm-1",
    )
    assert missing.status == "rejected"
    assert not hasattr(toolset.pipeline, "confirm_kwargs")

    confirmed = toolset.confirm_document_plan(
        "generation-session-a",
        expected_output_spec_hash="sha256:spec",
        expected_plan_hash="sha256:plan",
        client_request_id="confirm-1",
    )
    assert confirmed.status == "succeeded"
    assert toolset.pipeline.confirm_kwargs["expected_output_spec_hash"] == "sha256:spec"
    assert toolset.pipeline.confirm_kwargs["expected_plan_hash"] == "sha256:plan"
    assert toolset.pipeline.confirm_kwargs["client_request_id"] == "confirm-1"
    assert confirmed.data["status"] == "pending"
    assert confirmed.work_order_id is None and confirmed.job_id is None


def test_v2_get_document_task_status_reads_aggregate_by_task_id(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)

    result = toolset.get_document_task_status("document-task-a")

    assert result.status == "succeeded"
    assert toolset.pipeline.task_projection_id == "document-task-a"
    assert result.data["planning_state"]["plan_hash"] == "sha256:plan"
    assert result.data["next_actions"] == ["confirm_document_plan"]


def test_v2_scope_exception_tool_resolves_and_resumes_generation(tmp_path, monkeypatch):
    """A non-blocking scope exception can be resolved from the conversation."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    review = SimpleNamespace(
        status="pending",
        exceptions=[SimpleNamespace(
            exception_id="exception-a",
            kind="connector_scope_ambiguous",
            refdes="X301",
            user_instruction="请确认使用 X301 还是 X302。",
        )],
    )
    calls: dict = {}
    toolset.pipeline.get_icd_scope_review = lambda _ctx, work_order_id: review
    toolset.pipeline.submit_icd_scope_resolution = (
        lambda _ctx, work_order_id, *, resolutions, comment: calls.update({
            "work_order_id": work_order_id,
            "resolutions": resolutions,
            "comment": comment,
        }) or SimpleNamespace(status="frozen")
    )
    toolset.pipeline.submit_knowledge_base_document_generation = (
        lambda _ctx, work_order_id: "run-resumed"
    )

    result = toolset.resolve_icd_scope_exception(
        action="exclude",
        exception_ids=["exception-a"],
        comment="排除 X301",
        work_order_id="wo-scope",
    )

    assert result.status == "succeeded"
    assert calls["work_order_id"] == "wo-scope"
    assert calls["resolutions"] == [{"exception_id": "exception-a", "action": "exclude"}]
    assert calls["comment"] == "排除 X301"
    assert result.data["run_id"] == "run-resumed"
    assert result.next_actions == ["await_generation", "get_document_task_status"]


def test_v2_scope_exception_tool_refuses_blocking_kind_with_instruction(tmp_path, monkeypatch):
    """connector_mapping_missing cannot be waved through from chat."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.pipeline.get_icd_scope_review = lambda _ctx, _work_order_id: SimpleNamespace(
        status="pending",
        exceptions=[SimpleNamespace(
            exception_id="exception-b",
            kind="connector_mapping_missing",
            refdes="X302",
            user_instruction="已确定接插件 X302，但当前冻结来源中未找到其 EDF 管脚映射。",
        )],
    )
    toolset.pipeline.submit_icd_scope_resolution = lambda *_args, **_kwargs: pytest.fail(
        "blocking scope exceptions must not be resolved from chat"
    )

    result = toolset.resolve_icd_scope_exception(
        action="include",
        comment="包含 X302",
        work_order_id="wo-scope",
    )

    assert result.status == "rejected"
    assert result.error_code == "icd_scope_blocking"
    assert "X302" in result.message
    assert "EDF" in result.message
    assert result.next_actions == ["open_document_workbench"]


def test_v2_scope_exception_tool_confirms_edf_connector_replacement(tmp_path, monkeypatch):
    """A user-confirmed EDF replacement bypasses include/exclude resolution."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.pipeline.get_icd_scope_review = lambda _ctx, _order_id: SimpleNamespace(
        status="pending",
        exceptions=[SimpleNamespace(
            exception_id="exception-a",
            kind="connector_mapping_missing",
            refdes="X302",
            user_instruction="模板示例位号，冻结 EDF 实际为 X1900。",
            suggested_refdes=["X1900", "X1902"],
        )],
    )
    calls: dict = {}
    toolset.pipeline.rebuild_icd_scope_from_edf = (
        lambda _ctx, order_id, *, comment: calls.update({
            "order_id": order_id,
            "comment": comment,
        }) or {"status": "resumed", "run_id": "run-edf"}
    )
    toolset.pipeline.submit_icd_scope_resolution = lambda *_args, **_kwargs: pytest.fail(
        "EDF replacement must not use include/exclude"
    )

    result = toolset.resolve_icd_scope_exception(
        action="use_edf_connectors",
        comment="按 EDF 实际位号生成",
        work_order_id="wo-scope",
    )

    assert result.status == "succeeded"
    assert calls["order_id"] == "wo-scope"
    assert calls["comment"] == "按 EDF 实际位号生成"
    assert result.data["run_id"] == "run-edf"
    assert result.data["refdes_source"] == "edf"
    assert "已按冻结 EDF 的实际位号替换模板示例位号" in result.message


def test_v2_scope_exception_tool_selects_by_refdes(tmp_path, monkeypatch):
    """A per-connector instruction must not silently resolve every exception."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    review = SimpleNamespace(
        status="pending",
        exceptions=[
            SimpleNamespace(
                exception_id="exception-a",
                kind="connector_scope_ambiguous",
                refdes="X301",
                user_instruction="请确认 X301。",
            ),
            SimpleNamespace(
                exception_id="exception-b",
                kind="connector_scope_ambiguous",
                refdes="X302",
                user_instruction="请确认 X302。",
            ),
        ],
    )
    calls: dict = {}
    toolset.pipeline.get_icd_scope_review = lambda _ctx, _order_id: review
    toolset.pipeline.submit_icd_scope_resolution = (
        lambda _ctx, _order_id, *, resolutions, comment: calls.update({
            "resolutions": resolutions,
        }) or SimpleNamespace(status="frozen")
    )
    toolset.pipeline.submit_knowledge_base_document_generation = (
        lambda _ctx, _order_id: "run-refdes"
    )

    result = toolset.resolve_icd_scope_exception(
        action="exclude",
        refdes=["x302"],
        comment="排除 X302",
        work_order_id="wo-scope",
    )

    assert result.status == "succeeded"
    assert calls["resolutions"] == [{"exception_id": "exception-b", "action": "exclude"}]


def test_v2_scope_exception_tool_retries_submission_when_review_already_frozen(tmp_path, monkeypatch):
    """A frozen review still needs its generation submission retried."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.pipeline.get_icd_scope_review = lambda _ctx, _order_id: SimpleNamespace(
        status="frozen",
        exceptions=[],
    )
    toolset.pipeline.submit_icd_scope_resolution = lambda *_args, **_kwargs: pytest.fail(
        "a frozen review must not be resolved again"
    )
    calls: dict = {}
    toolset.pipeline.submit_knowledge_base_document_generation = (
        lambda _ctx, order_id: calls.update({"order_id": order_id}) or "run-retry"
    )

    result = toolset.resolve_icd_scope_exception(
        action="include",
        comment="重试提交",
        work_order_id="wo-scope",
    )

    assert result.status == "succeeded"
    assert calls["order_id"] == "wo-scope"
    assert result.data["run_id"] == "run-retry"


def test_v2_scope_exception_tool_reports_queue_failure_without_claiming_success(tmp_path, monkeypatch):
    """Freezing the review and failing to queue must stay recoverable."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.pipeline.get_icd_scope_review = lambda _ctx, _order_id: SimpleNamespace(
        status="pending",
        exceptions=[SimpleNamespace(
            exception_id="exception-a",
            kind="connector_scope_ambiguous",
            refdes="X301",
            user_instruction="请确认 X301。",
        )],
    )
    toolset.pipeline.submit_icd_scope_resolution = (
        lambda *_args, **_kwargs: SimpleNamespace(status="frozen")
    )

    def _boom(*_args, **_kwargs):
        raise ValueError("queue unavailable")

    toolset.pipeline.submit_knowledge_base_document_generation = _boom

    result = toolset.resolve_icd_scope_exception(
        action="include",
        comment="包含 X301",
        work_order_id="wo-scope",
    )

    assert result.status == "waiting_human"
    assert "queue unavailable" in result.message
    assert "重试" in result.message
    assert "resolve_icd_scope_exception" in result.next_actions


def test_v2_scope_exception_tool_reports_when_generation_cannot_be_resumed(tmp_path, monkeypatch):
    """Compat pipelines without the submission worker must fail closed."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.pipeline.get_icd_scope_review = lambda _ctx, _order_id: SimpleNamespace(
        status="frozen",
        exceptions=[],
    )
    if hasattr(toolset.pipeline, "submit_knowledge_base_document_generation"):
        delattr(toolset.pipeline, "submit_knowledge_base_document_generation")

    result = toolset.resolve_icd_scope_exception(
        action="include",
        comment="继续",
        work_order_id="wo-scope",
    )

    assert result.status == "waiting_human"
    assert "工作台" in result.message
    assert result.data["status"] == "submission_unavailable"


def test_v2_start_session_without_template_creates_intake_session(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch, template=False)

    result = toolset.start_document_generation_session(purpose="生成评审报告")

    assert result.status == "succeeded"
    assert toolset.pipeline.create_kwargs["template_version_id"] is None
    assert toolset.pipeline.create_kwargs["contract_version"] == "output_spec_v1"
    assert result.data["status"] == "awaiting_plan"


def test_v2_generate_document_from_template_still_uses_legacy_path_when_flag_off(tmp_path):
    toolset = _toolset(tmp_path)

    result = toolset.generate_document_from_template(purpose="参考模板生成 ICD 文档")
    assert result.status == "succeeded"
    assert result.work_order_id == "work-order-a"


def test_v2_answer_clarification_emits_requirement_clarification_card(tmp_path, monkeypatch):
    import src.settings

    monkeypatch.setattr(src.settings, "DOCUMENT_PLANNING_V2_ENABLED", True)
    sink = []
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.event_sink = sink.append
    toolset.pipeline.answer_document_generation_session = lambda _ctx, session_id, **_kw: toolset.pipeline.v2_session

    result = toolset.answer_clarification(
        "generation-session-a", "purpose", "评审报告", client_request_id="answer-1",
    )

    assert result.status == "succeeded"
    assert result.data["output_spec_id"] == "output-spec-a"
    assert result.data["output_spec_version"] == 3
    assert result.data["output_spec_draft"]["document_type"] == "report"
    kinds = [event["card"]["kind"] for event in sink]
    assert "requirement_clarification" in kinds


def test_v2_answer_clarification_result_carries_the_canonical_next_options(tmp_path, monkeypatch):
    """The tool result must expose the server-published options verbatim.

    Without them the model invents its own option labels, which the intake
    rejects three times before giving up.
    """
    from src.document_authoring.generation_sessions import ClarificationMessage

    toolset = _v2_toolset(tmp_path, monkeypatch)
    session = toolset.pipeline.v2_session.model_copy(update={
        "messages": [
            ClarificationMessage(
                role="assistant",
                content="找不到可靠资料时如何处理？",
                question_id="missing_data_policy",
                options=["标记未提供", "保留空白", "停止并提示"],
                reason="缺失数据策略决定是否允许继续生成。",
            ),
        ],
    })
    toolset.pipeline.v2_session = session
    toolset.pipeline.answer_document_generation_session = (
        lambda _ctx, _session_id, **_kw: session
    )

    result = toolset.answer_clarification(
        "generation-session-a", "deliverables", "excel",
    )

    assert result.status == "succeeded"
    pending = result.data["pending_question"]
    assert pending["question_id"] == "missing_data_policy"
    assert pending["content"] == "找不到可靠资料时如何处理？"
    assert pending["options"] == ["标记未提供", "保留空白", "停止并提示"]


def test_v2_direct_request_resumes_the_conversation_intake_session(tmp_path, monkeypatch):
    """A tool call without the session pointer resumes the conversation's intake.

    Forking a fresh session discarded the recorded answers and restarted the
    whole clarification flow from scratch.
    """
    toolset = _v2_toolset(tmp_path, monkeypatch)
    pipeline = toolset.pipeline
    finder_calls: list[dict] = []

    def _find(_ctx, **kwargs):
        finder_calls.append(kwargs)
        return pipeline.v2_session

    pipeline.find_reusable_document_generation_session = _find

    result = toolset.generate_document_from_template(
        purpose="参考模板生成文档",
        use_recommended_defaults=True,
    )

    assert result.status == "waiting_human"
    assert "confirm_document_plan" in result.next_actions
    assert finder_calls and finder_calls[0]["knowledge_base_name"] == "hardware"
    assert pipeline.create_kwargs == {}
    assert not hasattr(pipeline, "answer_v2_kwargs")
    assert pipeline.propose_kwargs["session_id"] == "generation-session-a"


def test_v2_proposal_card_carries_safe_plan_summary(tmp_path, monkeypatch):
    sink = []
    toolset = _v2_toolset(tmp_path, monkeypatch)
    toolset.event_sink = sink.append

    toolset.propose_document_plan("generation-session-a")

    card = sink[-1]["card"]
    assert card["kind"] == "output_spec_confirmation"
    proposal = card["proposal"]
    assert proposal["plan_hash"] == "sha256:plan"
    assert proposal["output_spec_hash"] == "sha256:spec"
    assert proposal["document_plan_id"] == "plan-a"
    assert "source_names" not in json.dumps(card)
    assert "evidence" not in json.dumps(card)


def test_v2_answer_clarification_recovers_from_a_stale_session_reference(tmp_path, monkeypatch):
    """A lost pointer must resume the conversation session, not reject or fork.

    The observed incident submitted "采用推荐方案" against a reference that no
    longer existed; the tool answered ``clarification_rejected`` and the model
    then created a second intake session that re-asked answered questions.
    """
    toolset = _v2_toolset(tmp_path, monkeypatch)
    pipeline = toolset.pipeline
    finder_calls: list[str] = []

    def _get_session(_ctx, session_id):
        if session_id != "generation-session-a":
            raise KeyError("generation session not found")
        return pipeline.v2_session

    def _find(_ctx, **_kwargs):
        finder_calls.append("called")
        return pipeline.v2_session

    pipeline.get_document_generation_session = _get_session
    pipeline.find_reusable_document_generation_session = _find

    result = toolset.answer_clarification(
        "generation-session-stale", "recommendations", "采用推荐方案",
    )

    assert result.status == "succeeded"
    assert result.generation_session_id == "generation-session-a"
    assert pipeline.answer_v2_kwargs["session_id"] == "generation-session-a"
    assert finder_calls == ["called"]


def test_v2_answer_clarification_uses_the_conversation_session_without_a_pointer(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)
    pipeline = toolset.pipeline

    def _get_session(_ctx, _session_id):
        raise KeyError("generation session not found")

    def _find(_ctx, **_kwargs):
        return pipeline.v2_session

    pipeline.get_document_generation_session = _get_session
    pipeline.find_reusable_document_generation_session = _find

    result = toolset.answer_clarification(
        "generation-session-fabricated", "target_identity", "X1900",
    )

    assert result.status == "succeeded"
    assert pipeline.answer_v2_kwargs["session_id"] == "generation-session-a"


def test_v2_answer_clarification_fails_closed_without_any_live_session(tmp_path, monkeypatch):
    toolset = _v2_toolset(tmp_path, monkeypatch)
    pipeline = toolset.pipeline

    def _get_session(_ctx, _session_id):
        raise KeyError("generation session not found")

    pipeline.get_document_generation_session = _get_session
    pipeline.find_reusable_document_generation_session = lambda _ctx, **_kwargs: None

    result = toolset.answer_clarification(
        "generation-session-stale", "recommendations", "采用推荐方案",
    )

    assert result.status == "rejected"
    assert result.error_code == "clarification_session_unresolved"


def test_v2_start_session_resumes_the_conversation_intake_session(tmp_path, monkeypatch):
    """Repeated start must not fork a new draft over a live intake session."""
    toolset = _v2_toolset(tmp_path, monkeypatch)
    pipeline = toolset.pipeline
    pipeline.find_reusable_document_generation_session = (
        lambda _ctx, **_kwargs: pipeline.v2_session
    )

    result = toolset.start_document_generation_session(purpose="参考模板生成文档")

    assert result.status == "succeeded"
    assert result.message == "generation session resumed"
    assert result.generation_session_id == "generation-session-a"
    assert pipeline.create_kwargs == {}
