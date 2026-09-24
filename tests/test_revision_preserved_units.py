"""T6 local-reuse and preservation regressions.

The child plan for a section revision must reuse the parent's verified draft
for unaffected units (with its own receipt), recompute the affected unit, and
fail the revision if a supposedly preserved unit changed.  The comparison is
unit-level, never a whole-file hash (repair plan §8.2 R1 / §15.8).
"""

from __future__ import annotations

from types import SimpleNamespace


from src.document_authoring.harness.runtime import InternalDocumentHarnessRuntime
from src.document_authoring.models import (
    DocumentArtifact,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    KnowledgeBaseSourceSnapshot,
    TemplateVersion,
)
from src.document_authoring.planning.models import OutputSpec
from src.document_authoring.planning.revision import RevisionPatch, compile_revision_plan
from src.document_authoring.revisions import (
    ArtifactRevisionStore,
    preservation_result,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import (
    DeterministicEvidenceWriter,
    ManagedWriter,
)
from src.pipelines.document_rag.schemas import EvidenceEnvelope

from tests.test_document_plan_dag_execution import _plan, _output_payload_for_runtime
from tests.test_revision_execution_inputs import _CapturingWriter, _rebind_snapshot


def test_preservation_result_fails_on_mismatch_and_missing():
    parent = {"cover": SimpleNamespace(content="ADAS ICD", typed_value=None, proposed_value=None)}
    same = {"cover": SimpleNamespace(content="ADAS ICD", typed_value=None, proposed_value=None)}
    changed = {"cover": SimpleNamespace(content="ADAS ICD v2", typed_value=None, proposed_value=None)}

    assert preservation_result(
        reused_unit_ids=["cover"], parent_drafts=parent, child_drafts=same
    )["passed"] is True

    failed = preservation_result(
        reused_unit_ids=["cover"], parent_drafts=parent, child_drafts=changed
    )
    assert failed["passed"] is False
    assert failed["failed"] == ["cover"]

    missing = preservation_result(
        reused_unit_ids=["cover"], parent_drafts=parent, child_drafts={}
    )
    assert missing["passed"] is False
    assert missing["missing"] == ["cover"]


def _retrieve(snapshot, requested: list[str] | None = None):
    def retrieve(requirement, attempt, query_override=None):
        del attempt, query_override
        if requested is not None:
            requested.append(requirement.semantic_unit_id)
        is_table = requirement.semantic_unit_id == "field:pins"
        if is_table:
            rows = [("J1:1", "1", "CANH"), ("J1:2", "2", "CANL")]
            evidences = [EvidenceEnvelope(
                id=f"ev-{row_key}",
                content=f"connector=J1 pin={pin} signal={signal}",
                source_name="design.pdf",
                metadata={
                    "knowledge_base_name": "ADAS",
                    "table_row": {
                        "row_key": row_key,
                        "cells": {"connector": "J1", "pin": pin, "signal": signal},
                    },
                },
            ) for row_key, pin, signal in rows]
        else:
            evidences = [EvidenceEnvelope(
                id="ev-cover",
                content="Cover title: ADAS ICD",
                source_name="design.pdf",
                metadata={"knowledge_base_name": "ADAS"},
            )]
        return __import__(
            "src.agents.claim_evidence", fromlist=["RetrievalOutcome"]
        ).RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits",
            evidences=evidences,
            query_fingerprint="query",
            applied_source_set_snapshot_id=snapshot.source_set_snapshot_id,
        )

    return retrieve


def test_revision_reuses_unaffected_unit_and_verifies_preservation(tmp_path, monkeypatch):
    import src.settings

    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_EXECUTION_ENABLED", True)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", {"tenant-a"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES", {"icd"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS", {"xlsx"}, raising=False)

    from src.core.auth import RequestContext

    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="source-plan-reuse",
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
        ctx, knowledge_base_name="ADAS", idempotency_key="reuse-task",
    )
    assert task is not None

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

    def _order(work_order_id: str) -> DocumentWorkOrder:
        return DocumentWorkOrder(
            work_order_id=work_order_id,
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

    parent_order = _order("wo-reuse-parent")
    store.create_work_order(parent_order)
    service.task_service.store.attach_work_order(task.task_id, parent_order.work_order_id)
    runtime = InternalDocumentHarnessRuntime(store=store)

    parent_run, parent_manifest = runtime.create_run(
        parent_order, policy, snapshot, template, schema
    )
    parent_result = runtime.execute(
        work_order=parent_order, run=parent_run, manifest=parent_manifest,
        policy=policy, schema=schema, snapshot=snapshot, legacy_claims=[],
        writer=ManagedWriter(DeterministicEvidenceWriter()),
        retrieve=_retrieve(snapshot),
    )
    assert len(parent_result.drafts) == 2
    parent_drafts = {
        draft.unit_id: draft for draft in store.list_unit_drafts(parent_run.harness_run_id)
    }
    assert set(parent_drafts) == {"field:cover", "field:pins"}

    parent_artifact = DocumentArtifact(
        artifact_id="artifact-reuse-parent",
        tenant_id="tenant-a",
        work_order_id=parent_order.work_order_id,
        run_id=parent_run.harness_run_id,
        output_format="xlsx",
        stage="approved_release",
        content_hash="parent-hash",
        validation_report_id="report-parent",
        integrity_manifest_id="manifest-parent",
    )
    store.save_artifact(parent_artifact, b"parent-bytes", "xlsx")

    patch = RevisionPatch(
        patch_id="revision-patch:reuse",
        revision_id="artifact-revision-reuse",
        parent_artifact_id=parent_artifact.artifact_id,
        parent_artifact_content_hash=parent_artifact.content_hash,
        parent_plan_id=parent_plan.document_plan_id,
        parent_plan_version=parent_plan.version,
        parent_plan_hash=parent_plan.plan_hash,
        operation="section_update",
        instruction="将“ADAS ICD”替换为“ADAS Interface Control Document”",
        changed_sections=["cover"],
    )
    compilation = compile_revision_plan(parent_plan, spec, patch)
    child_plan = compilation.child_plan
    assert list(compilation.affected_scope.affected_unit_ids) == ["cover"]
    assert list(compilation.affected_scope.reused_unit_ids) == ["pins"]
    store.planning.create_plan(child_plan, tenant_id="tenant-a", user_id="user-a")
    store.planning.accept_plan_for_revision(
        child_plan.document_plan_id,
        child_plan.version,
        expected_plan_hash=child_plan.plan_hash,
        revision_id=patch.revision_id,
        tenant_id="tenant-a",
        user_id="user-a",
    )
    revision_store = ArtifactRevisionStore(str(tmp_path / "authoring.db"))
    revision = revision_store.create(
        task_id=task.task_id,
        work_order_id=parent_order.work_order_id,
        parent_artifact_id=parent_artifact.artifact_id,
        request_type="section_update",
        request="将“ADAS ICD”替换为“ADAS Interface Control Document”",
        changed_sections=["cover"],
        input_snapshot_hash="sha256:reuse-input",
        source_snapshot_hash=snapshot.content_hash,
        parent_plan_id=parent_plan.document_plan_id,
        parent_plan_version=parent_plan.version,
        parent_plan_hash=parent_plan.plan_hash,
        child_plan_id=child_plan.document_plan_id,
        child_plan_version=child_plan.version,
        child_plan_hash=child_plan.plan_hash,
        plan_diff_hash=compilation.plan_diff.diff_hash,
        affected_unit_ids=list(compilation.affected_scope.affected_unit_ids),
        reused_unit_ids=list(compilation.affected_scope.reused_unit_ids),
        execution_scope_hash=compilation.affected_scope.scope_hash,
        impact_scope={"kind": "sections", "sections": ["cover"]},
        revalidation_scope=["artifact", "cover", "pins"],
        client_request_id="revision-reuse-1",
    )

    child_order = service.restart_work_order_for_revision(
        ctx, parent_order.work_order_id, revision_id=revision.revision_id,
    )
    assert child_order.document_plan_id == child_plan.document_plan_id

    retrieved: list[str] = []
    child_run, child_manifest = runtime.create_run(
        child_order, policy, snapshot, template, schema
    )
    capturing = _CapturingWriter(DeterministicEvidenceWriter())
    child_result = runtime.execute(
        work_order=child_order, run=child_run, manifest=child_manifest,
        policy=policy, schema=schema, snapshot=snapshot, legacy_claims=[],
        writer=ManagedWriter(capturing),
        retrieve=_retrieve(snapshot, retrieved),
    )

    assert retrieved == ["field:cover"]
    assert {request.unit_id for request in capturing.requests} == {"field:cover"}
    assert {draft.unit_id for draft in child_result.drafts} == {"field:cover", "field:pins"}
    child_drafts = {
        draft.unit_id: draft for draft in store.list_unit_drafts(child_run.harness_run_id)
    }
    assert child_drafts["field:cover"].content != parent_drafts["field:cover"].content
    assert child_drafts["field:pins"].content == parent_drafts["field:pins"].content

    child_artifact = DocumentArtifact(
        artifact_id="artifact-reuse-child",
        tenant_id="tenant-a",
        work_order_id=child_order.work_order_id,
        run_id=child_run.harness_run_id,
        output_format="xlsx",
        stage="review_candidate",
        content_hash="child-hash",
        validation_report_id="report-child",
        integrity_manifest_id="manifest-child",
        parent_artifact_id=parent_artifact.artifact_id,
    )
    store.save_artifact(child_artifact, b"child-bytes", "xlsx")

    completed = service.revision_service.bind_generated_child(
        child_order, child_artifact, revalidation_status="passed",
    )
    preservation = completed.revalidation_result["preservation"]
    assert preservation["passed"] is True
    assert preservation["verified"] == ["pins"]
    assert completed.revalidation_result["target_change"]["changed"] == ["cover"]
    assert completed.status == "revalidated"
