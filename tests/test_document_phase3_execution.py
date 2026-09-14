from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import src.settings
from src.agents.claim_evidence import RetrievalOutcome
from src.core.app_pipeline import AppPipeline
from src.document_authoring.generation_sessions import GenerationSessionStore
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.models import KnowledgeBaseSourceSnapshot
from src.document_authoring.planning.intake import OutputSpecIntakeService
from src.document_authoring.planning.models import OutputSpec
from src.document_authoring.planning.service import TemplateFreePlanningAdapter
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.tasks import DocumentTaskStore
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import DeterministicEvidenceWriter, ManagedWriter
from src.pipelines.document_rag.schemas import EvidenceEnvelope, RequestContext


TENANT = "tenant-phase3"
USER = "author-phase3"
KB = "hardware-phase3"


def _template_free_env(tmp_path: Path, *, fmt: str = "docx"):
    db = str(tmp_path / "authoring.db")
    store = DocumentAuthoringStore(db, artifact_root=str(tmp_path / "files"))
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id=TENANT,
        knowledge_base_name=KB,
        source_names=["design.pdf"],
        created_by=USER,
    )
    snapshot = store.create_knowledge_base_source_snapshot(snapshot)
    layout = (
        {"mode": "system_recipe", "recipe_id": "generic-report", "recipe_version": "1"}
        if fmt in {"docx", "pdf"}
        else {"mode": "system_recipe", "recipe_id": "structured-table", "recipe_version": "1"}
    )
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec:phase3-execution",
        version=1,
        purpose="生成模板无关报告",
        document_type="generic_report",
        layout_source=layout,
        outline=[
            {"unit_id": "summary", "kind": "section", "title": "Summary", "required": True},
        ],
        deliverables=[{"format": fmt, "role": "primary", "required": True}],
        missing_data_policy="mark_tbd",
        inference_policy="forbid",
        approval_policy_id="default-document-v1",
    )
    spec = intake.to_output_spec(draft).model_copy(update={"status": "proposed"})
    plan = TemplateFreePlanningAdapter().compile(
        output_spec=spec,
        source_snapshot_id=snapshot.source_set_snapshot_id,
        source_snapshot_hash=snapshot.content_hash,
    )
    plan = plan.model_copy(update={"status": "proposed"})
    planning = store.planning
    planning.create_output_spec(spec, tenant_id=TENANT, user_id=USER, task_id="task-phase3")
    planning.create_plan(plan, tenant_id=TENANT, user_id=USER, task_id="task-phase3")
    task = DocumentTaskStore(db).create_task(
        tenant_id=TENANT,
        user_id=USER,
        origin="api",
        created_by=USER,
        knowledge_base_name=KB,
        template_version_id=None,
        generation_session_id=None,
        status="awaiting_plan_confirmation",
    )
    session = GenerationSessionStore(db).create_session(
        tenant_id=TENANT,
        user_id=USER,
        knowledge_base_name=KB,
        template_version_id=None,
        contract_version="output_spec_v1",
        status="awaiting_plan_confirmation",
        document_task_id=task.task_id,
        output_spec_id=spec.output_spec_id,
        output_spec_version=spec.version,
        document_plan_id=plan.document_plan_id,
        document_plan_version=plan.version,
    )
    return SimpleNamespace(
        db=db,
        store=store,
        planning=planning,
        snapshot=snapshot,
        spec=spec,
        plan=plan,
        task=task,
        session=session,
    )


def _pipeline(env, *, job_store=None):
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = DocumentGenerationService(store=env.store)
    pipeline.document_job_store = job_store or DocumentAuthoringJobStore(env.db)
    pipeline.backend = Mock()
    pipeline.spreadsheet_service = None
    pipeline._icd_template_profile = lambda order: None
    pipeline._icd_connector_scope_schema = lambda order: None
    pipeline._icd_front_view_connector_refdes = lambda order: []
    pipeline._document_requirement_resolution_snapshot = Mock(
        return_value={"unresolved_requirements": [], "resolved_fields": {}}
    )
    return pipeline


def _ctx() -> RequestContext:
    return RequestContext(
        user_id=USER,
        tenant_id=TENANT,
        metadata={"department_id": "hw"},
        kb_permissions={f"hw:{KB}": "write"},
    )


def _confirm(env):
    return env.planning.confirm_submission(
        session_id=env.session.session_id,
        tenant_id=TENANT,
        user_id=USER,
        expected_output_spec_hash=env.spec.content_hash,
        expected_plan_hash=env.plan.plan_hash,
        client_request_id="confirm-phase3",
    )


def _enable_phase3(monkeypatch, *, fmt: str = "docx"):
    monkeypatch.setattr(src.settings, "DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_EXECUTION_ENABLED", True)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", {TENANT}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES", {"generic_report"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS", {fmt}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_TENANTS", {TENANT}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_DOCUMENT_TYPES", {"generic_report"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_FORMATS", {fmt}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_RECIPES", {"generic-report@1"}, raising=False)


def test_phase3_confirmation_and_dispatch_materialize_a_recipe_work_order(tmp_path, monkeypatch):
    _enable_phase3(monkeypatch)
    env = _template_free_env(tmp_path)
    submission = _confirm(env)
    pipeline = _pipeline(env)

    result = pipeline.dispatch_confirmed_document_plan(_ctx(), submission)

    assert result["status"] == "dispatched"
    order = env.store.get_work_order(result["work_order_id"])
    assert order is not None
    assert order.template_version_id == "system-recipe:generic-report@1"
    assert order.document_schema_id == "system-recipe-schema:generic-report@1"
    assert order.target_format == "docx"
    assert order.input_fingerprint_version == 3
    assert pipeline.document_job_store.get_by_work_order(order.work_order_id) is not None


def test_phase3_confirmation_remains_closed_when_template_free_flag_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(src.settings, "DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED", False, raising=False)
    env = _template_free_env(tmp_path)

    with pytest.raises(ValueError, match="template-free|disabled"):
        _confirm(env)


def test_phase3_dispatch_rejects_recipe_outside_exact_allowlist(tmp_path, monkeypatch):
    _enable_phase3(monkeypatch)
    env = _template_free_env(tmp_path)
    submission = _confirm(env)
    monkeypatch.setattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_RECIPES", {"structured-table@1"}, raising=False)

    with pytest.raises(ValueError, match="allowlist"):
        _pipeline(env).dispatch_confirmed_document_plan(_ctx(), submission)


def test_phase3_dispatch_does_not_run_template_specific_preflight(tmp_path, monkeypatch):
    _enable_phase3(monkeypatch)
    env = _template_free_env(tmp_path)
    submission = _confirm(env)
    pipeline = _pipeline(env)
    pipeline._kb_document_generation_stages = Mock(
        side_effect=AssertionError("template preflight must not run for a recipe")
    )
    pipeline.submit_knowledge_base_document_generation = Mock(return_value="job-phase3")

    result = pipeline.dispatch_confirmed_document_plan(_ctx(), submission)

    assert result["status"] == "dispatched"
    assert result["job_id"] == "job-phase3"
    pipeline._kb_document_generation_stages.assert_not_called()


def test_phase3_recipe_work_order_runs_through_fresh_structured_renderer(tmp_path, monkeypatch):
    _enable_phase3(monkeypatch)
    monkeypatch.setattr(src.settings, "DOCUMENT_AUTO_PUBLISH_VERIFIED", False)
    env = _template_free_env(tmp_path)
    submission = _confirm(env)
    pipeline = _pipeline(env)
    pipeline.submit_knowledge_base_document_generation = Mock(return_value="job-phase3")
    pipeline.dispatch_confirmed_document_plan(_ctx(), submission)
    order = env.store.list_work_orders_for_knowledge_base(TENANT, KB)[0]
    policy = env.store.get_harness_policy(order.harness_policy_id, order.harness_policy_version)
    assert policy is not None
    env.store.save_harness_policy(policy.model_copy(update={
        "writer_provider_id": DeterministicEvidenceWriter.provider_id,
    }))

    def retrieve(requirement, attempt, query_override=None):
        del attempt, query_override
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits",
            evidences=[EvidenceEnvelope(
                id="ev-summary",
                content="Summary: all systems nominal",
                source_name="design.pdf",
                metadata={"knowledge_base_name": KB},
            )],
            query_fingerprint="query-phase3",
            applied_source_set_snapshot_id=env.snapshot.source_set_snapshot_id,
        )

    artifact = pipeline.document_generation.run_internal_harness(
        _ctx(),
        order.work_order_id,
        retrieve=retrieve,
        writer=ManagedWriter(DeterministicEvidenceWriter()),
    )

    assert artifact.output_format == "docx"
    assert artifact.stage == "review_candidate"
    assert env.store.get_validation_report(artifact.validation_report_id).status == "passed"
    assert env.store.read_artifact_content(artifact.artifact_id).startswith(b"PK")


def test_phase3_runtime_rechecks_template_free_kill_switch(tmp_path, monkeypatch):
    _enable_phase3(monkeypatch)
    env = _template_free_env(tmp_path)
    submission = _confirm(env)
    pipeline = _pipeline(env)
    pipeline.submit_knowledge_base_document_generation = Mock(return_value="job-phase3")
    pipeline.dispatch_confirmed_document_plan(_ctx(), submission)
    order = env.store.list_work_orders_for_knowledge_base(TENANT, KB)[0]
    monkeypatch.setattr(src.settings, "DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED", False)

    with pytest.raises(ValueError, match="template-free|disabled"):
        pipeline.document_generation.run_internal_harness(
            _ctx(), order.work_order_id, retrieve=Mock(),
        )
