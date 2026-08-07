from __future__ import annotations

import hashlib

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from src.core.app_pipeline import AppPipeline
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.generation_sessions import GenerationBrief, GenerationSession
from src.document_authoring.models import (
    DocumentSchema,
    DocumentWorkOrder,
    RendererPolicy,
    TemplateSecurityReport,
    TemplateVersion,
    content_hash,
)
from src.document_authoring.requirement_clarifier import RequirementClarifier
from src.document_authoring.template_analysis import TemplateAnalysis
from src.document_authoring.generation_sessions import GenerationSessionStore
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.pipelines.document_rag.schemas import RequestContext


def test_generation_session_round_trips_brief_and_messages(tmp_path):
    store = GenerationSessionStore(str(tmp_path / "authoring.db"))
    session = store.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
    )

    store.append_message(
        session.session_id,
        role="assistant",
        content="请选择项目版本",
        options=["当前发布版本", "最新上传版本"],
    )
    store.update_brief(
        session.session_id,
        {"scope": {"revision": "当前发布版本"}, "confirmed": False},
    )
    confirmed = store.confirm(session.session_id)
    loaded = store.get_session(
        session.session_id,
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert confirmed.status == "ready_to_generate"
    assert loaded.brief.confirmed is True
    assert loaded.brief.scope == {"revision": "当前发布版本"}
    assert loaded.messages[0].options == ["当前发布版本", "最新上传版本"]


def test_generation_session_rejects_cross_user_access(tmp_path):
    store = GenerationSessionStore(str(tmp_path / "authoring.db"))
    session = store.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
    )

    with pytest.raises(PermissionError):
        store.get_session(
            session.session_id,
            tenant_id="tenant-a",
            user_id="user-b",
        )


def test_document_authoring_store_exposes_generation_sessions(tmp_path):
    store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )

    session = store.generation_sessions.create_session(
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
    )

    assert store.generation_sessions.get_session(session.session_id).session_id == session.session_id


def test_confirmed_session_freezes_brief_into_work_order_inputs():
    pipeline = object.__new__(AppPipeline)
    session = GenerationSession(
        session_id="generation-session-1",
        tenant_id="tenant-a",
        user_id="user-a",
        knowledge_base_name="hardware",
        template_version_id="template-a",
        status="ready_to_generate",
        brief=GenerationBrief(
            scope={"revision": "当前发布版本"},
            missing_data_policy="标记未提供",
            inference_policy="禁止推断",
            confirmed=True,
        ),
    )
    pipeline.get_document_generation_session = Mock(return_value=session)

    frozen = pipeline._generation_session_work_order_inputs(
        SimpleNamespace(),
        generation_session_id="generation-session-1",
        knowledge_base_name="hardware",
        template_version_id="template-a",
    )

    assert frozen["generation_session_id"] == "generation-session-1"
    assert frozen["generation_brief"]["scope"]["revision"] == "当前发布版本"
    assert frozen["idempotency_key"] == "generation-session:generation-session-1"


def test_document_status_exposes_brief_and_actionable_error_fields():
    pipeline = object.__new__(AppPipeline)
    order = SimpleNamespace(
        work_order_id="wo-1",
        status="blocked",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        target_format="xlsx",
        unit_statuses={},
        validation_report_id=None,
        run_manifest_id=None,
        generation_session_id="generation-session-1",
        generation_brief={"scope": {"revision": "当前发布版本"}},
        error_code="renderer_safety_violation",
        error_message="duplicate long value fan-out",
        retryable=False,
        next_actions=["replace_template"],
    )
    store = SimpleNamespace(
        get_work_order=Mock(return_value=order),
        list_harness_runs=Mock(return_value=[]),
        list_artifacts=Mock(return_value=[]),
    )
    pipeline.document_generation = SimpleNamespace(
        store=store,
        require_work_order_capability=Mock(),
    )

    status = pipeline.get_document_run_status("wo-1", SimpleNamespace())

    assert status["clarification_session_id"] == "generation-session-1"
    assert status["generation_brief"]["scope"]["revision"] == "当前发布版本"
    assert status["error_code"] == "renderer_safety_violation"
    assert status["next_actions"] == ["replace_template"]


def test_legacy_work_order_fingerprint_ignores_absent_generation_brief():
    order = DocumentWorkOrder(
        work_order_id="wo-legacy",
        tenant_id="tenant-a",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id="snapshot-1",
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        template_schema_id="template-schema-1",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="deterministic_only",
        created_by="user-a",
    )
    legacy_excluded = {
        "input_fingerprint", "created_at", "updated_at", "lock_version", "status",
        "unit_statuses", "evidence_matrix_id", "validation_report_id", "run_manifest_id",
        "error_code", "error_message", "retryable", "next_actions",
        "generation_session_id", "generation_brief",
    }
    legacy_fingerprint = content_hash(order.model_dump(mode="json", exclude=legacy_excluded))
    payload = order.model_dump(mode="json")
    payload["input_fingerprint"] = legacy_fingerprint

    loaded = DocumentWorkOrder.model_validate(payload)

    assert loaded.input_fingerprint == legacy_fingerprint


def test_confirmed_session_binds_exactly_one_real_work_order(tmp_path):
    store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    template_content = b"template"
    template_hash = hashlib.sha256(template_content).hexdigest()
    store.save_renderer_policy(RendererPolicy(renderer_policy_id="renderer-1"))
    store.save_template(
        TemplateVersion(
            template_version_id="template-1",
            template_id="template-1",
            format="xlsx",
            content_hash=template_hash,
            template_schema_id="template-schema-1",
            template_schema_version="1",
            renderer_policy_id="renderer-1",
            tenant_id="tenant-a",
            knowledge_base_name="hardware",
            resource_department_id=1,
            knowledge_base_id=1,
            status="approved",
        ),
        template_content,
        TemplateSecurityReport(
            report_id="template-security-1",
            content_hash=template_hash,
            format="xlsx",
        ),
    )
    store.save_template_analysis(TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash=template_hash,
        format="xlsx",
        status="ready_for_confirmation",
        units=[],
    ))
    store.save_document_schema(DocumentSchema(
        document_schema_id="schema-1",
        version="1",
        document_type="hardware-review",
        status="approved",
        execution_mode="deterministic_only",
    ))
    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={
            "document_template_kb_name": "hardware",
            "resource_department_id": 1,
            "kb_id": 1,
        },
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = DocumentGenerationService(store=store)
    pipeline.requirement_clarifier = RequirementClarifier()

    session = pipeline.create_document_generation_session(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-1",
        purpose="生成硬件评审表",
    )
    for answer in ("当前发布版本", "标记未提供", "禁止推断"):
        question_id = next(
            message.question_id
            for message in reversed(session.messages)
            if message.role == "assistant" and message.question_id
        )
        session = pipeline.answer_document_generation_session(
            ctx,
            session.session_id,
            question_id=question_id,
            answer=answer,
        )
    confirmed = pipeline.confirm_document_generation_session(ctx, session.session_id)
    inputs = pipeline._generation_session_work_order_inputs(
        ctx,
        generation_session_id=confirmed.session_id,
        knowledge_base_name="hardware",
        template_version_id="template-1",
    )
    order = pipeline.document_generation.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["hardware-revision.pdf"],
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        **inputs,
    )
    store.generation_sessions.bind_work_order(confirmed.session_id, order.work_order_id)
    repeated = pipeline.document_generation.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["hardware-revision.pdf"],
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        **inputs,
    )

    saved_session = store.generation_sessions.get_session(
        confirmed.session_id,
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert repeated.work_order_id == order.work_order_id
    assert order.generation_brief["confirmed"] is True
    assert order.generation_brief["scope"]["revision"] == "当前发布版本"
    assert saved_session.work_order_id == order.work_order_id
    assert len(store.list_work_orders_for_knowledge_base("tenant-a", "hardware")) == 1
