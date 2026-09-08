"""Contract tests for the scoped document-authoring chat tools."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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
        output_spec_draft={"output_spec_id": "output-spec-a", "version": 3},
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
