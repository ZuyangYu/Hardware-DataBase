"""T2 execution-input regression: the revision text must reach the writer.

This is the test the repair plan explicitly requires: capture the input at the
real execution entry (``InternalDocumentHarnessRuntime``) rather than mocking
work-order creation.  A plan-bound revision compiles a child plan, the child
plan is accepted with the revision command, and the runtime executing that
child plan must pass the revision instruction to the writer for the target
unit only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.document_authoring.harness.runtime import InternalDocumentHarnessRuntime
from src.document_authoring.models import (
    DocumentSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    KnowledgeBaseSourceSnapshot,
    TemplateVersion,
)
from src.document_authoring.planning.models import DocumentPlan, OutputSpec
from src.document_authoring.planning.revision import RevisionPatch, compile_revision_plan
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import (
    DeterministicEvidenceWriter,
    ManagedWriter,
)
from src.pipelines.document_rag.schemas import EvidenceEnvelope

from tests.test_document_plan_dag_execution import _plan, _output_payload_for_runtime


class _CapturingWriter:
    """Delegates to the deterministic writer while recording every request."""

    provider_id = "deterministic_evidence_writer"

    def __init__(self, inner):
        self._inner = inner
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return self._inner.generate(request)


def _rebind_snapshot(plan: DocumentPlan, snapshot: KnowledgeBaseSourceSnapshot) -> DocumentPlan:
    payload = plan.model_dump(mode="json")
    payload.update({
        "source_snapshot_id": snapshot.source_set_snapshot_id,
        "source_snapshot_hash": snapshot.content_hash,
    })
    payload.pop("plan_hash", None)
    return DocumentPlan.model_validate(payload)


def test_revision_instruction_reaches_the_writer_for_the_target_unit(tmp_path, monkeypatch):
    import src.settings

    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_EXECUTION_ENABLED", True)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", {"tenant-a"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES", {"icd"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS", {"xlsx"}, raising=False)

    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="source-plan-revision",
        tenant_id="tenant-a",
        knowledge_base_name="ADAS",
        source_names=["design.pdf"],
        created_by="user-a",
    )
    parent_plan = _rebind_snapshot(_plan(status="accepted"), snapshot)
    spec = OutputSpec.model_validate(_output_payload_for_runtime(parent_plan))
    patch = RevisionPatch(
        patch_id="revision-patch:integration",
        revision_id="artifact-revision-integration",
        parent_artifact_id="artifact-parent",
        parent_artifact_content_hash="sha256:parent",
        parent_plan_id=parent_plan.document_plan_id,
        parent_plan_version=parent_plan.version,
        parent_plan_hash=parent_plan.plan_hash,
        operation="section_update",
        instruction="将引脚表改成英文",
        changed_sections=["Pin Definition"],
    )
    compilation = compile_revision_plan(parent_plan, spec, patch)
    child_plan = compilation.child_plan
    assert compilation.target_unit_ids == ["pins"]

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts")
    )
    store.planning.create_output_spec(spec, tenant_id="tenant-a", user_id="user-a")
    store.planning.create_plan(parent_plan, tenant_id="tenant-a", user_id="user-a")
    store.planning.create_plan(child_plan, tenant_id="tenant-a", user_id="user-a")
    accepted = store.planning.accept_plan_for_revision(
        child_plan.document_plan_id,
        child_plan.version,
        expected_plan_hash=child_plan.plan_hash,
        revision_id="artifact-revision-integration",
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert accepted.status == "accepted"

    order = DocumentWorkOrder(
        work_order_id="wo-plan-revision",
        tenant_id="tenant-a",
        scope_type="knowledge_base",
        knowledge_base_name="ADAS",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id=snapshot.source_set_snapshot_id,
        template_version_id="template-001",
        document_schema_id="schema-001",
        document_schema_version="1",
        template_schema_id="schema-001",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="internal_harness",
        harness_policy_id="policy-1",
        harness_policy_version="1",
        requested_executor="internal_harness",
        input_fingerprint_version=3,
        output_spec_id=child_plan.output_spec_id,
        output_spec_version=child_plan.output_spec_version,
        output_spec_hash=child_plan.output_spec_hash,
        document_plan_id=child_plan.document_plan_id,
        document_plan_version=child_plan.version,
        document_plan_hash=child_plan.plan_hash,
        revision_id=patch.revision_id,
        created_by="user-a",
    )
    store.create_work_order(order)

    policy = HarnessPolicy(
        harness_policy_id="policy-1", version="1", status="approved",
        writer_provider_id="deterministic_evidence_writer",
        max_parallel_units=1,
        max_retrieval_rounds=8,
    )
    template = TemplateVersion(
        template_version_id="template-001", template_id="template-001",
        format="xlsx", content_hash="template-hash", template_schema_id="schema-001",
        template_schema_version="1", renderer_policy_id="renderer-1",
    )
    schema = DocumentSchema(
        document_schema_id="schema-001", version="1", document_type="icd",
        status="approved", execution_mode="internal_harness",
    )
    runtime = InternalDocumentHarnessRuntime(store=store)
    # Load the accepted child plan from the durable store rather than passing
    # the in-memory object, so the test covers the same binding path a worker
    # uses when it rebuilds context (repair plan §12.2).
    run, manifest = runtime.create_run(order, policy, snapshot, template, schema)
    assert run.document_plan_id == child_plan.document_plan_id

    def retrieve(requirement, attempt, query_override=None):
        del attempt, query_override
        is_table = requirement.semantic_unit_id == "field:pins"
        metadata = {
            "knowledge_base_name": "ADAS",
            "table_row": {
                "row_key": "J1:1",
                "cells": {"connector": "J1", "pin": "1", "signal": "CANH"},
            },
        } if is_table else {"knowledge_base_name": "ADAS"}
        content = (
            "connector=J1 pin=1 signal=CANH"
            if is_table else "Cover title: ADAS ICD"
        )
        return __import__(
            "src.agents.claim_evidence", fromlist=["RetrievalOutcome"]
        ).RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits",
            evidences=[EvidenceEnvelope(
                id=f"ev-{requirement.semantic_unit_id}", content=content,
                source_name="design.pdf", metadata=metadata,
            )],
            query_fingerprint="query",
            applied_source_set_snapshot_id=snapshot.source_set_snapshot_id,
        )

    capturing = _CapturingWriter(DeterministicEvidenceWriter())
    result = runtime.execute(
        work_order=order, run=run, manifest=manifest, policy=policy, schema=schema,
        snapshot=snapshot, legacy_claims=[],
        writer=ManagedWriter(capturing),
        retrieve=retrieve,
    )

    assert len(result.drafts) == 2
    pins_request = next(r for r in capturing.requests if r.unit_id == "field:pins")
    cover_request = next(r for r in capturing.requests if r.unit_id == "field:cover")
    assert pins_request.revision_instruction == "将引脚表改成英文"
    assert cover_request.revision_instruction == ""
    assert result.document_plan_id == child_plan.document_plan_id


def test_create_revision_compiles_child_plan_and_rebinds_the_work_order(tmp_path, monkeypatch):
    """Service-level bridge: create_revision → compile → restart uses child plan."""

    from src.core.auth import RequestContext
    from src.document_authoring.models import DocumentArtifact
    from src.document_authoring.service import DocumentGenerationService

    monkeypatch.setattr(
        "src.settings.DOCUMENT_PLAN_DAG_EXECUTION_ENABLED", True, raising=False
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="source-plan-revision-service",
        tenant_id="tenant-a",
        knowledge_base_name="ADAS",
        source_names=["design.pdf"],
        created_by="user-a",
    )
    parent_plan = _rebind_snapshot(_plan(status="accepted"), snapshot)
    spec = OutputSpec.model_validate(_output_payload_for_runtime(parent_plan))

    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts")
    )
    store.planning.create_output_spec(spec, tenant_id="tenant-a", user_id="user-a")
    store.planning.create_plan(parent_plan, tenant_id="tenant-a", user_id="user-a")

    service = DocumentGenerationService(store=store)
    service._template = lambda _template_id: SimpleNamespace(
        status="approved", template_version_id="template-001",
        template_schema_id="schema-001", template_schema_version="1", format="xlsx",
    )
    service._schema = lambda _schema_id, _version: SimpleNamespace(
        status="approved", execution_mode="deterministic_only", fields=[], review_items=[],
        document_schema_id="schema-001", version="1",
    )
    service._policy = lambda _template: SimpleNamespace(version="1")
    service.revision_service.source_snapshot_resolver = lambda _order: snapshot
    service.resolve_source_snapshot = lambda _order: snapshot
    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:ADAS": "write"},
        metadata={"resource_department_id": 1},
    )
    task = service.ensure_document_task(
        ctx, knowledge_base_name="ADAS", idempotency_key="revision-service-task",
    )
    assert task is not None

    order = DocumentWorkOrder(
        work_order_id="wo-plan-revision-service",
        tenant_id="tenant-a",
        scope_type="knowledge_base",
        knowledge_base_name="ADAS",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id=snapshot.source_set_snapshot_id,
        template_version_id="template-001",
        document_schema_id="schema-001",
        document_schema_version="1",
        template_schema_id="schema-001",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="deterministic_only",
        harness_policy_id="policy-1",
        harness_policy_version="1",
        requested_executor="deterministic_only",
        input_fingerprint_version=3,
        resource_department_id="1",
        output_spec_id=parent_plan.output_spec_id,
        output_spec_version=parent_plan.output_spec_version,
        output_spec_hash=parent_plan.output_spec_hash,
        document_plan_id=parent_plan.document_plan_id,
        document_plan_version=parent_plan.version,
        document_plan_hash=parent_plan.plan_hash,
        task_id=task.task_id,
        created_by="user-a",
    )
    store.create_work_order(order)
    service.task_service.store.attach_work_order(task.task_id, order.work_order_id)
    parent_artifact = DocumentArtifact(
        artifact_id="artifact-parent-service",
        tenant_id="tenant-a",
        work_order_id=order.work_order_id,
        run_id="run-service",
        output_format="xlsx",
        stage="approved_release",
        content_hash="parent-service-hash",
        validation_report_id="report-service",
        integrity_manifest_id="manifest-service",
    )
    store.save_artifact(parent_artifact, b"parent", "xlsx")

    revision = service.revision_service.create_revision(
        ctx,
        task_id=task.task_id,
        parent_artifact_id="artifact-parent-service",
        request_type="section_update",
        request="将引脚表改成英文",
        changed_sections=["Pin Definition"],
        client_request_id="revision-service-1",
    )

    assert revision.child_plan_id is not None
    assert revision.child_plan_version == parent_plan.version + 1
    assert revision.affected_unit_ids == ["pins"]
    child = store.planning.get_plan(revision.child_plan_id, revision.child_plan_version)
    assert child is not None and child.status == "accepted"
    assert child.plan_hash == revision.child_plan_hash
    assert revision.metadata["revision_patch"]["instruction"] == "将引脚表改成英文"

    restarted = service.restart_work_order_for_revision(
        ctx, order.work_order_id, revision_id=revision.revision_id,
    )
    assert restarted.document_plan_id == revision.child_plan_id
    assert restarted.document_plan_hash == revision.child_plan_hash
    assert restarted.output_spec_id == parent_plan.output_spec_id

