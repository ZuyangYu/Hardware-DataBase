"""Conversation-led v2 intake state machine against the real stores.

The reported production failure was a session/state fork: every direct
generation request re-created the OutputSpec intake session, discarded the
recorded clarification answers, and then failed plan compilation with a canned
"answer the pending clarification first" message.  These tests pin the
corrected contract: one session per conversation, a status that mirrors the
intake progress, a template-owned outline, and truthful proposal errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.app_pipeline import AppPipeline
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    KnowledgeBaseSourceSnapshot,
    RendererPolicy,
    TemplateSecurityReport,
    TemplateUnitBinding,
    TemplateVersion,
    WorkbookTableColumnSchema,
    WorkbookTableSchema,
)
from src.document_authoring.planning.intake import OutputSpecIntakeService
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext

TENANT = "tenant-a"
USER = "user-a"
KB = "hardware"
DEPARTMENT_ID = 1
KB_ID = 1
TEMPLATE_HASH = "sha256:" + "a" * 64


def _ctx(*, permission: str = "write") -> RequestContext:
    return RequestContext(
        user_id=USER,
        tenant_id=TENANT,
        metadata={
            "department_id": "hw",
            "document_template_kb_name": KB,
            "resource_department_id": DEPARTMENT_ID,
            "kb_id": KB_ID,
        },
        kb_permissions={f"{DEPARTMENT_ID}:{KB}": permission},
    )


@pytest.fixture()
def pipeline(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.settings.DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", (TENANT,))
    monkeypatch.setattr("src.settings.DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES", ("icd",))
    monkeypatch.setattr("src.settings.DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS", ("xlsx",))
    # Keep the transient-retrieval retry loop instant in tests.
    monkeypatch.setattr("src.settings.DOCUMENT_PREVIEW_RETRIEVAL_BACKOFF_SECONDS", 0.0)
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "files"))
    store.save_renderer_policy(RendererPolicy(renderer_policy_id="renderer-1"))
    store.save_template(
        TemplateVersion(
            template_version_id="tv-1",
            template_id="t-1",
            format="xlsx",
            content_hash=TEMPLATE_HASH,
            template_schema_id="ts-1",
            template_schema_version="1",
            renderer_policy_id="renderer-1",
            tenant_id=TENANT,
            knowledge_base_name=KB,
            resource_department_id=DEPARTMENT_ID,
            knowledge_base_id=KB_ID,
            status="approved",
        ),
        b"template-bytes",
        TemplateSecurityReport(
            report_id="rep-1", content_hash=TEMPLATE_HASH, format="xlsx"
        ),
    )
    store.create_knowledge_base_source_snapshot(
        KnowledgeBaseSourceSnapshot.create(
            tenant_id=TENANT,
            knowledge_base_name=KB,
            source_names=["spec.pdf"],
            created_by=USER,
        )
    )
    service = DocumentGenerationService(store=store)
    service.register_document_schema(
        DocumentSchema(
            document_schema_id="ts-1",
            version="1",
            document_type="连接器管脚定义表",
            status="approved",
            execution_mode="deterministic_only",
            fields=[
                DocumentFieldSchema(
                    field_id="pin-table",
                    label="核心管脚定义明细表",
                    value_type="table",
                    retrieval_policy_id="generic-retrieval",
                    verification_policy_id="generic-verification",
                    table_columns={"A": "管脚号", "B": "管脚定义", "C": "功能描述", "D": "备注"},
                    table_row_scope="connector",
                    table_row_keys=["pin-1", "pin-2"],
                ),
            ],
        )
    )
    store.save_unit_bindings([
        TemplateUnitBinding(
            binding_id="binding-pin-table",
            template_schema_id="ts-1",
            template_schema_version="1",
            semantic_unit_type="field",
            semantic_unit_id="pin-table",
            target_region_ids=["region-pin-table"],
            table_schema=WorkbookTableSchema(
                table_region_id="region-pin-table",
                semantic_unit_id="pin-table",
                sheet_name="ICD",
                header_row=1,
                first_data_row=2,
                last_template_row=50,
                style_source_row=2,
                max_output_rows=200,
                columns=[
                    WorkbookTableColumnSchema(column_id="A", label="管脚号", column_letter="A"),
                    WorkbookTableColumnSchema(column_id="B", label="管脚定义", column_letter="B"),
                    WorkbookTableColumnSchema(column_id="C", label="功能描述", column_letter="C"),
                    WorkbookTableColumnSchema(column_id="D", label="备注", column_letter="D"),
                ],
                expected_row_keys=["pin-1", "pin-2"],
                required_columns=["A", "B", "C", "D"],
            ),
        )
    ])
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = service
    pipeline.document_job_store = DocumentAuthoringJobStore(str(tmp_path / "jobs.db"))
    pipeline.backend = object()
    pipeline.list_file_infos = lambda kb_name, ctx=None: []
    return pipeline


def _create_session(pipeline, ctx):
    return pipeline.create_document_generation_session(
        ctx,
        knowledge_base_name=KB,
        template_version_id="tv-1",
        purpose="参考该文档模板，根据知识库内容，生成新的icd文档",
        contract_version="output_spec_v1",
    )


def _answer(pipeline, ctx, session, question_id: str, answer: str):
    return pipeline.answer_document_generation_session(
        ctx,
        session.session_id,
        question_id=question_id,
        answer=answer,
        client_request_id=f"answer:{question_id}",
    )


def _review_session(pipeline, ctx):
    session = _create_session(pipeline, ctx)
    if OutputSpecIntakeService().next_question(session.output_spec_draft).question_id == "target_identity":
        session = _answer(pipeline, ctx, session, "target_identity", "按照知识库中的模块和连接来确定")
    return _answer(pipeline, ctx, session, "recommendations", "采用推荐方案")


def test_content_review_confirmation_advances_and_compiles(pipeline):
    ctx = _ctx()
    session = _review_session(pipeline, ctx)
    session = _answer(pipeline, ctx, session, "content_confirmation", "确认")
    assert session.status == "awaiting_plan"
    spec = OutputSpecIntakeService.to_output_spec(session.output_spec_draft)
    assert spec.document_type == "icd"


@pytest.mark.parametrize("answer", ["不要生成", "不确认", "确认前先只看CAN模块", "继续之前请调整范围"])
def test_content_review_does_not_treat_negative_or_edit_as_confirmation(pipeline, answer):
    ctx = _ctx()
    session = _review_session(pipeline, ctx)
    revised = _answer(pipeline, ctx, session, "content_confirmation", answer)
    assert revised.status == "needs_clarification"
    assert not revised.output_spec_draft.get("content_preview_confirmed")
    assert any(item.get("raw_text") == answer for item in revised.output_spec_draft["additional_requirements"])


def test_content_review_retrieves_real_evidence_before_claiming_it(pipeline):
    from src.pipelines.document_rag.schemas import Evidence
    class Backend:
        def retrieve(self, kb, query, *, top_k, ctx, filters):
            assert kb == KB
            assert filters["source_names"] == ["spec.pdf"]
            return [Evidence(id="pin-evidence", content="X1 管脚1 电源12V", source_name="spec.pdf")]
    pipeline.backend = Backend()
    pipeline.list_file_infos = lambda kb, ctx=None: [{"name": "spec.pdf"}]
    session = _review_session(pipeline, _ctx())
    preview = session.output_spec_draft["content_preview"]
    assert preview["evidence_count"] == 1
    assert "X1 管脚1 电源12V" in session.messages[-1].content
    assert "spec.pdf" in session.messages[-1].content
    assert "按当前知识库自动发现相关模块" in session.messages[-1].content


def test_empty_source_review_reports_no_evidence_not_completed_content(pipeline):
    session = _review_session(pipeline, _ctx())
    assert session.output_spec_draft["content_preview"]["evidence_count"] == 0
    assert "未检索到" in session.messages[-1].content


def test_draft_template_uses_analysis_for_discovery_without_approval(pipeline):
    from src.document_authoring.template_analysis import TemplateAnalysis, TemplateAnalysisSuggestion, TemplateAnalysisUnit
    store = pipeline.document_generation.store
    template = store.get_template("tv-1").model_copy(update={
        "status": "draft", "template_schema_id": "not-activated-yet",
    })
    store.replace_template(template)
    store.save_template_analysis(TemplateAnalysis(
        analysis_id="analysis-draft", template_version_id="tv-1", content_hash=TEMPLATE_HASH,
        format="xlsx", status="requires_human",
        units=[TemplateAnalysisUnit(unit_id="sheet:ICD!A2", locator={"sheet_name": "ICD", "cell": "A2"}, writable=True)],
        suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="interfaces", label="接口清单", target_unit_ids=["sheet:ICD!A2"],
            confidence=0.9, value_shape="repeating_table",
        )],
    ))
    session = _create_session(pipeline, _ctx())
    assert session.output_spec_draft["outline"] == [{
        "unit_id": "interfaces", "kind": "table", "title": "接口清单", "required": True,
    }]
    assert store.get_template("tv-1").status == "draft"


def test_review_filters_out_evidence_outside_frozen_sources(pipeline):
    from src.pipelines.document_rag.schemas import Evidence
    class Backend:
        def retrieve(self, *args, **kwargs):
            return [Evidence(id="foreign", content="OTHER PROJECT DATA", source_name="other.pdf")]
    pipeline.backend = Backend()
    pipeline.list_file_infos = lambda kb, ctx=None: [{"name": "spec.pdf"}]
    session = _review_session(pipeline, _ctx())
    assert session.output_spec_draft["content_preview"]["evidence_count"] == 0
    assert "OTHER PROJECT DATA" not in session.messages[-1].content


def test_failed_retrieval_keeps_answer_and_asks_for_retry(pipeline):
    class Backend:
        def __init__(self):
            self.calls = 0

        def retrieve(self, *args, **kwargs):
            self.calls += 1
            raise ConnectionError("backend failed")
    backend = Backend()
    pipeline.backend = backend
    pipeline.list_file_infos = lambda kb, ctx=None: [{"name": "spec.pdf"}]
    session = _review_session(pipeline, _ctx())
    draft = session.output_spec_draft
    # The accepted recommendations stay durable even though the transient
    # retrieval outage skipped the preview.  All configured attempts ran
    # before the retryable question was published.
    assert backend.calls == 3
    assert draft["missing_data_policy"] == "mark_tbd"
    assert draft["inference_policy"] == "forbid"
    assert draft["approval_policy_id"]
    assert not draft.get("content_preview_confirmed")
    assert draft.get("content_preview") is None
    assert session.last_question_id == "content_preview_retry"
    assert "检索暂未完成" in session.messages[-1].content


def test_transient_retrieval_failure_is_retried_within_the_turn(pipeline):
    from src.pipelines.document_rag.schemas import Evidence

    class FlakyOnce:
        def __init__(self):
            self.calls = 0

        def retrieve(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("embedding endpoint unreachable")
            return [Evidence(id="e-1", content="X1 管脚1 电源12V", source_name="spec.pdf")]

    backend = FlakyOnce()
    pipeline.backend = backend
    pipeline.list_file_infos = lambda kb, ctx=None: [{"name": "spec.pdf"}]
    session = _review_session(pipeline, _ctx())

    # The first attempt failed; the retry succeeded transparently, so the
    # conversation continues without asking the user to answer again.
    assert backend.calls == 2
    assert session.last_question_id == "content_confirmation"
    assert session.output_spec_draft["content_preview"]["evidence_count"] == 1


def test_content_preview_retry_succeeds_after_a_transient_failure(pipeline):
    from src.pipelines.document_rag.schemas import Evidence

    class Flaky:
        def __init__(self, fail_times):
            self.calls = 0
            self.fail_times = fail_times

        def retrieve(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise ConnectionError("embedding endpoint unreachable")
            return [Evidence(id="pin-evidence", content="X1 管脚1 电源12V", source_name="spec.pdf")]

    backend = Flaky(fail_times=3)
    pipeline.backend = backend
    pipeline.list_file_infos = lambda kb, ctx=None: [{"name": "spec.pdf"}]
    ctx = _ctx()
    session = _review_session(pipeline, ctx)
    assert session.last_question_id == "content_preview_retry"
    assert backend.calls == 3

    session = _answer(pipeline, ctx, session, "content_preview_retry", "重试")

    assert session.last_question_id == "content_confirmation"
    assert session.output_spec_draft["content_preview"]["evidence_count"] == 1
    assert "X1 管脚1 电源12V" in session.messages[-1].content


def test_preview_does_not_substitute_kb_for_requested_attachment_scope(pipeline):
    draft = {"source_scope": {"attachments": ["attachment-1"]}}
    with pytest.raises(ValueError, match="附件"):
        pipeline._prepare_document_content_preview(_ctx(), KB, draft)


def test_content_confirmation_never_advances_unapproved_template_to_plan(pipeline):
    store = pipeline.document_generation.store
    store.replace_template(store.get_template("tv-1").model_copy(update={"status": "draft"}))
    session = _review_session(pipeline, _ctx())
    session = _answer(pipeline, _ctx(), session, "content_confirmation", "确认")
    assert session.status != "awaiting_plan"
    assert store.get_template("tv-1").status == "draft"
    assert "模板" in session.messages[-1].content


@pytest.mark.parametrize("consent,approved", [("允许替换模板示例数据", True), ("不要替换模板示例数据", False)])
def test_template_replacement_consent_is_handled_in_the_conversation(pipeline, tmp_path, consent, approved):
    from tests.test_template_mapping_review import _service_with_table
    service, _, template_id = _service_with_table(tmp_path)
    ctx = _ctx()
    service.store.replace_template(service.store.get_template(template_id).model_copy(update={
        "knowledge_base_name": KB, "resource_department_id": DEPARTMENT_ID, "knowledge_base_id": KB_ID,
    }))
    pipeline.document_generation = service
    session = pipeline.create_document_generation_session(
        ctx, knowledge_base_name=KB, template_version_id=template_id,
        purpose="按照知识库中的全部模块生成icd文档", contract_version="output_spec_v1",
    )
    question = OutputSpecIntakeService().next_question(session.output_spec_draft)
    if question.question_id == "target_identity":
        session = _answer(pipeline, ctx, session, "target_identity", "按照知识库中的模块和连接来确定")
    session = _answer(pipeline, ctx, session, "recommendations", "采用推荐方案")
    session = _answer(pipeline, ctx, session, "content_confirmation", "确认")
    assert session.status == "needs_clarification"
    assert session.messages[-1].question_id == "template_mapping_confirmation"
    assert service.store.get_template(template_id).status == "draft"
    session = _answer(pipeline, ctx, session, "template_mapping_confirmation", consent)
    assert (service.store.get_template(template_id).status == "approved") is approved
    if approved:
        assert session.status == "awaiting_plan"
        assert OutputSpecIntakeService.to_output_spec(session.output_spec_draft).document_type == "icd"
        proposal = pipeline.create_document_plan_proposal(
            ctx, session.session_id, expected_output_spec_version=session.output_spec_version,
            client_request_id="plan-after-mapping-consent",
        )
        assert proposal["executable"] is True, proposal.get("blockers")
    else:
        assert session.status != "awaiting_plan"


def test_template_bound_session_seeds_outline_and_tracks_intake_status(pipeline):
    """The template schema owns the outline; the session status mirrors intake."""
    ctx = _ctx()
    intake = OutputSpecIntakeService()

    session = _create_session(pipeline, ctx)
    # Execution policy uses canonical document-type identifiers.  A template
    # display label or filename must never become the allowlist scope.
    assert session.output_spec_draft["document_type"] == "icd"
    # The template owns both layout and file format, so neither the outline nor
    # the Excel delivery format is asked back to the user.
    assert session.status == "needs_clarification"
    assert session.output_spec_draft["artifact"]["deliverables"] == [{
        "format": "xlsx", "role": "primary", "required": True,
    }]
    assert session.output_spec_draft["target_identity"] == {
        "mode": "knowledge_base_discovery",
        "value": "按知识库中的模块、连接器和接口数据确定",
    }
    assert intake.next_question(session.output_spec_draft).question_id == "recommendations"
    assert [
        unit["unit_id"] for unit in session.output_spec_draft["outline"]
    ] == ["pin-table"]

    session = _answer(pipeline, ctx, session, "recommendations", "采用推荐方案")
    assert session.status == "needs_clarification"
    assert session.messages[-1].question_id == "content_confirmation"
    session = _answer(pipeline, ctx, session, "content_confirmation", "确认")
    assert session.status == "awaiting_plan"
    assert intake.next_question(session.output_spec_draft) is None


def test_incomplete_session_rejects_plan_proposal_with_the_real_reason(pipeline):
    ctx = _ctx()
    session = _create_session(pipeline, ctx)

    with pytest.raises(ValueError, match="not awaiting a plan proposal"):
        pipeline.create_document_plan_proposal(
            ctx,
            session.session_id,
            client_request_id="document-generation:early",
            expected_output_spec_version=session.output_spec_version,
        )


def test_completed_intake_compiles_the_template_backed_proposal(pipeline):
    ctx = _ctx()
    session = _review_session(pipeline, ctx)
    session = _answer(pipeline, ctx, session, "content_confirmation", "确认")

    proposal = pipeline.create_document_plan_proposal(
        ctx,
        session.session_id,
        client_request_id="document-generation:proposal-1",
        expected_output_spec_version=session.output_spec_version,
    )

    assert proposal["session_id"] == session.session_id
    assert proposal["output_spec_hash"].startswith("sha256:")
    assert proposal["plan_hash"].startswith("sha256:")
    # The schema-owned row contract resolved the table columns and row keys,
    # so the compiled plan is executable without extra clarification.
    assert proposal["executable"] is True
    stored = pipeline.document_generation.store.generation_sessions.get_session(
        session.session_id
    )
    assert stored.status == "awaiting_plan_confirmation"


def test_plan_proposal_reports_execution_allowlist_blocker_before_confirmation(
    pipeline, monkeypatch,
):
    monkeypatch.setattr(
        "src.settings.DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", (TENANT,),
    )
    monkeypatch.setattr(
        "src.settings.DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES", ("report",),
    )
    monkeypatch.setattr(
        "src.settings.DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS", ("xlsx",),
    )
    ctx = _ctx()
    session = _review_session(pipeline, ctx)
    session = _answer(pipeline, ctx, session, "content_confirmation", "确认")

    proposal = pipeline.create_document_plan_proposal(
        ctx,
        session.session_id,
        client_request_id="document-generation:blocked-preflight",
        expected_output_spec_version=session.output_spec_version,
    )

    assert proposal["executable"] is False
    assert proposal["blockers"][0]["code"] == "execution_scope_not_allowlisted"
    assert proposal["status"] == "awaiting_plan"


def test_conversation_lookup_resumes_the_same_intake_session(pipeline):
    """A follow-up turn without the session pointer must resume, not fork."""
    from src.pipelines.document_rag.schemas import RequestContext as RC

    ctx = RC(
        user_id=USER,
        tenant_id=TENANT,
        metadata={
            "department_id": "hw",
            "document_template_kb_name": KB,
            "resource_department_id": DEPARTMENT_ID,
            "kb_id": KB_ID,
            "conversation_id": "conversation-42",
        },
        kb_permissions={f"{DEPARTMENT_ID}:{KB}": "write"},
    )
    intake = OutputSpecIntakeService()

    session = _create_session(pipeline, ctx)
    resumed = pipeline.find_reusable_document_generation_session(
        ctx,
        knowledge_base_name=KB,
        template_version_id="tv-1",
    )
    assert resumed is not None
    assert resumed.session_id == session.session_id

    assert intake.next_question(resumed.output_spec_draft).question_id == "recommendations"

    # A foreign template reference must not match the conversation session.
    assert (
        pipeline.find_reusable_document_generation_session(
            ctx, knowledge_base_name=KB, template_version_id="tv-other",
        )
        is None
    )


def test_non_transient_retrieval_failure_is_not_retried(pipeline):
    class Broken:
        def __init__(self):
            self.calls = 0

        def retrieve(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("deterministic parser bug")

    backend = Broken()
    pipeline.backend = backend
    pipeline.list_file_infos = lambda kb, ctx=None: [{"name": "spec.pdf"}]
    session = _review_session(pipeline, _ctx())

    assert backend.calls == 1
    assert session.last_question_id == "content_preview_retry"


def test_retry_classifier_treats_transport_failures_as_transient():
    from src.core.app_pipeline import _is_retryable_retrieval_error
    from src.pipelines.document_rag.ragflow_backend import RAGFlowAPIError

    assert _is_retryable_retrieval_error(
        RAGFlowAPIError({"code": 100, "message": "EmbeddingError"})
    )
    assert not _is_retryable_retrieval_error(
        RAGFlowAPIError({"code": 102, "message": "not owner"})
    )
    assert not _is_retryable_retrieval_error(PermissionError("no kb permission"))
    assert _is_retryable_retrieval_error(ConnectionError("connection reset"))


def test_later_turn_inherits_knowledge_base_scope_without_reasking_identity(pipeline):
    """The recorded KB-delegated scope survives into later intake drafts."""
    ctx = _ctx()
    ctx.metadata["conversation_id"] = "conv-inherit"
    first = _create_session(pipeline, ctx)
    assert first.output_spec_draft["target_identity"]["mode"] == "knowledge_base_discovery"

    later = pipeline.create_document_generation_session(
        ctx,
        knowledge_base_name=KB,
        template_version_id="tv-1",
        purpose="帮我填充上传的模板文件，生成本项目新的icd的excel文件",
        contract_version="output_spec_v1",
    )

    assert later.output_spec_draft["target_identity"]["mode"] == "knowledge_base_discovery"
    question = OutputSpecIntakeService().next_question(later.output_spec_draft)
    assert question is None or question.question_id != "target_identity"
