from __future__ import annotations

from types import SimpleNamespace

from src.document_authoring.planning import (
    DocumentPlan,
    diff_document_plans,
)
from src.document_authoring.planning.registry import build_builtin_registries
import src.document_authoring.planning as planning_module
from src.document_authoring.revisions import ArtifactRevision
from src.document_authoring.tasks import DocumentTaskStore
from src.pipelines.document_rag.schemas import RequestContext


def _plan() -> DocumentPlan:
    return DocumentPlan(
        document_plan_id="plan:phase4",
        version=1,
        output_spec_id="spec:phase4",
        output_spec_version=1,
        output_spec_hash="sha256:spec-phase4",
        source_snapshot_id="snapshot:phase4",
        source_snapshot_hash="sha256:snapshot-phase4",
        domain_strategy_id="generic_report",
        domain_strategy_version="1",
        layout_adapter_id="provided_template",
        layout_adapter_version="1",
        renderer_capability_id="docx",
        renderer_capability_version="1",
        layout_contract={
            "kind": "template",
            "template_version_id": "template:phase4",
            "template_schema_id": "schema:phase4",
            "template_schema_version": "1",
        },
        semantic_units=[
            {"unit_id": "overview", "kind": "section"},
            {"unit_id": "summary", "kind": "paragraph"},
            {"unit_id": "evidence", "kind": "paragraph"},
        ],
        dependency_edges=[
            {"upstream_task_id": "unit-task:overview", "downstream_task_id": "unit-task:summary"},
            {"upstream_task_id": "unit-task:summary", "downstream_task_id": "unit-task:evidence"},
        ],
        coverage_contract={
            "requirements": [
                {"requirement_id": unit, "unit_id": unit, "kind": "paragraph" if unit != "overview" else "section"}
                for unit in ("overview", "summary", "evidence")
            ]
        },
        unit_tasks=[
            {"task_id": f"unit-task:{unit}", "unit_id": unit, "plan_version": 1,
             "dependencies": [f"unit-task:{previous}"] if previous else [], "action_key": f"action:{unit}"}
            for unit, previous in (("overview", None), ("summary", "overview"), ("evidence", "summary"))
        ],
    )


def test_builtin_domain_strategies_are_registry_bound_and_executable():
    registries = build_builtin_registries()
    for strategy_id in ("generic_report", "icd", "fpt", "requirements"):
        implementation = registries.domain_strategies.implementation(strategy_id, "1")
        assert implementation is not None
        assert implementation.strategy_id == strategy_id
        assert callable(implementation.identify_requirements)
        assert callable(implementation.compile_semantic_units)
        requirements = implementation.identify_requirements(
            SimpleNamespace(document_type="generic", outline=[]),
            {},
        )
        assert requirements.strategy_id == strategy_id


def test_domain_strategies_emit_typed_requirements_and_reject_unsupported_columns():
    registries = build_builtin_registries()
    icd = registries.domain_strategies.implementation("icd", "1")
    assert icd is not None
    result = icd.identify_requirements(SimpleNamespace(
        document_type="icd",
        outline=[SimpleNamespace(unit_id="pins", kind="table", required=True)],
        table_requirements=[SimpleNamespace(unit_id="pins", required_columns=["connector", "pin"])],
    ))
    assert result.required_unit_ids == ["pins"]
    assert any(issue.code == "domain_required_columns_missing" for issue in result.issues)
    assert all(issue.path.startswith("table_requirements.pins") for issue in result.issues)

    requirements = registries.domain_strategies.implementation("requirements", "1")
    assert requirements is not None
    unsupported = requirements.identify_requirements(
        SimpleNamespace(document_type="icd", outline=[], table_requirements=[])
    )
    assert any(issue.code == "strategy_document_type_unsupported" for issue in unsupported.issues)


def test_plan_diff_compiles_deterministic_transitive_affected_subgraph():
    parent = _plan()
    child_data = parent.model_dump(mode="json")
    child_data["document_plan_id"] = "plan:phase4-child"
    child_data["version"] = 2
    child_data["semantic_units"][1]["output_schema"] = {"type": "changed"}
    for task in child_data["unit_tasks"]:
        task["plan_version"] = 2
    child_data.pop("plan_hash", None)
    child = DocumentPlan.model_validate(child_data)

    diff = diff_document_plans(parent, child)

    affected = getattr(diff, "affected_unit_ids", None)
    tasks = getattr(diff, "affected_task_ids", None)
    assert affected is not None, "PlanDiff must expose an affected unit closure"
    assert tasks is not None, "PlanDiff must expose an affected task closure"
    assert affected == ["evidence", "summary"]
    assert tasks == ["unit-task:evidence", "unit-task:summary"]
    assert diff.reused_unit_ids == ["overview"]
    assert diff.diff_hash
    assert diff.diff_hash == diff_document_plans(parent, child).diff_hash
    scope = planning_module.compile_affected_subgraph(parent, child, diff)
    assert scope.scope_hash
    assert scope.affected_task_ids == diff.affected_task_ids


def test_artifact_revision_can_bind_plan_diff_and_frozen_policy_hashes():
    assert "parent_plan_hash" in ArtifactRevision.model_fields, "revision must bind the parent plan"
    assert "plan_diff_hash" in ArtifactRevision.model_fields, "revision must bind the plan diff"


def test_plan_bound_revision_freezes_scope_and_policy_lineage(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "tasks.db"))
    task = task_store.create_task(
        tenant_id="tenant-a", user_id="user-a", origin="workbench",
        created_by="user-a", knowledge_base_name="hardware", status="completed",
    )
    parent_artifact = SimpleNamespace(
        artifact_id="artifact:parent", work_order_id="order:phase4",
        content_hash="sha256:artifact-parent", validation_report_id="report:parent",
        approval_event_ids=[],
    )
    order = SimpleNamespace(
        work_order_id="order:phase4", task_id=task.task_id, tenant_id="tenant-a",
        knowledge_base_name="hardware", source_set_snapshot_id="snapshot:phase4",
        baseline_content_hash="", template_version_id="",
    )
    child_data = _plan().model_dump(mode="json")
    child_data["document_plan_id"] = "plan:phase4-child"
    child_data["version"] = 2
    child_data["semantic_units"][1]["output_schema"] = {"type": "changed"}
    for unit_task in child_data["unit_tasks"]:
        unit_task["plan_version"] = 2
    child_data.pop("plan_hash", None)
    child_plan = DocumentPlan.model_validate(child_data)

    class AuthoringStore:
        def get_artifact(self, artifact_id):
            return parent_artifact if artifact_id == parent_artifact.artifact_id else None

        def get_work_order(self, work_order_id):
            return order if work_order_id == order.work_order_id else None

        def update_artifact(self, *_args, **_kwargs):
            return parent_artifact

    service = __import__(
        "src.document_authoring.revisions", fromlist=["DocumentRevisionService"]
    ).DocumentRevisionService(
        authoring_store=AuthoringStore(), task_store=task_store,
        revision_store=__import__(
            "src.document_authoring.revisions", fromlist=["ArtifactRevisionStore"]
        ).ArtifactRevisionStore(tmp_path / "revisions.db"),
        source_snapshot_resolver=lambda _order: SimpleNamespace(
            content_hash="sha256:snapshot-phase4"
        ),
    )
    revision = service.create_revision(
        RequestContext(
            user_id="user-a", tenant_id="tenant-a",
            kb_permissions={"1:hardware": "write"}, metadata={"resource_department_id": 1, "kb_id": 1},
        ),
        task_id=task.task_id, parent_artifact_id=parent_artifact.artifact_id,
        request_type="section_update", request="refresh summary",
        changed_sections=["summary"], client_request_id="phase4-revision-1",
        parent_plan=_plan(), child_plan=child_plan,
    )

    assert revision.parent_plan_hash == _plan().plan_hash
    assert revision.child_plan_hash == child_plan.plan_hash
    assert revision.plan_diff_hash
    assert revision.affected_unit_ids == ["evidence", "summary"]
    assert revision.reused_unit_ids == ["overview"]
    assert revision.execution_scope_hash
    assert revision.strategy_hash and revision.policy_hash


def test_release_policy_exposes_signed_gate_facts_and_reconstructable_lineage():
    policy_cls = getattr(planning_module, "ApprovalPolicy", None)
    gate_cls = getattr(planning_module, "GateFact", None)
    service_cls = getattr(planning_module, "ReleasePolicyService", None)
    assert policy_cls is not None, "versioned approval policy is not implemented"
    assert gate_cls is not None, "release gate facts are not implemented"
    assert service_cls is not None, "release policy service is not implemented"


def _lineage(*, artifact_id: str = "artifact:child", parent_artifact_id: str | None = None, policy_id: str = "default-document-v1"):
    policy = planning_module.build_builtin_approval_policy_registry().resolve(policy_id, "1")
    return planning_module.ArtifactLineage(
        artifact_id=artifact_id,
        artifact_hash=f"sha256:{artifact_id}",
        parent_artifact_id=parent_artifact_id,
        plan_id="plan:phase4",
        plan_version=1,
        plan_hash="sha256:plan-phase4",
        source_snapshot_id="snapshot:phase4",
        source_snapshot_hash="sha256:snapshot-phase4",
        strategy_id="generic_report",
        strategy_version="1",
        strategy_hash="sha256:strategy",
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_hash=policy.policy_hash,
        run_manifest_hash="sha256:manifest",
    )


def test_high_risk_release_requires_two_passed_gates_and_role_bound_signature():
    service = planning_module.ReleasePolicyService()
    lineage = _lineage()
    gate1 = planning_module.GateFact(
        fact_id="fact:gate1", gate="gate1", status="passed",
        plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
    )
    gate2 = planning_module.GateFact(
        fact_id="fact:gate2", gate="gate2", status="passed",
        plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
        artifact_hash=lineage.artifact_hash,
    )
    unsigned = service.evaluate(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id="default-document-v1", policy_version="1",
        gate_facts=[gate1, gate2], lineage=lineage,
    )
    assert unsigned.status == "blocked"
    assert "required_signer_missing" in unsigned.reasons

    signature = planning_module.ReleaseSignature(
        signature_id="signature:gate2",
        gate="gate2", actor_id="user:owner", actor_role="document_owner",
        subject_hash=gate2.fact_hash,
    )
    released = service.evaluate(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id="default-document-v1", policy_version="1",
        gate_facts=[gate1, gate2], lineage=lineage, signatures=[signature],
    )
    assert released.release_allowed is True
    assert released.status == "released"


def test_release_rejects_gate_facts_bound_to_a_different_plan_or_artifact():
    service = planning_module.ReleasePolicyService()
    lineage = _lineage()
    gate1 = planning_module.GateFact(
        fact_id="fact:mismatch-1", gate="gate1", status="passed",
        plan_id="plan:other", plan_version=1, plan_hash="sha256:other",
    )
    gate2 = planning_module.GateFact(
        fact_id="fact:mismatch-2", gate="gate2", status="passed",
        plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
        artifact_hash="sha256:other-artifact",
    )
    result = service.evaluate(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id="default-document-v1", policy_version="1",
        gate_facts=[gate1, gate2], lineage=lineage,
    )
    assert result.status == "blocked"
    assert "gate_fact_plan_mismatch" in result.reasons
    assert "gate_fact_artifact_mismatch" in result.reasons


def test_low_risk_auto_release_requires_matching_threshold_evidence():
    policy = planning_module.build_builtin_approval_policy_registry().resolve("low-risk-document-v1", "1")
    tenant_policy = planning_module.TenantReleasePolicy(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id=policy.policy_id, policy_version=policy.version,
        auto_release_enabled=True, threshold_evidence_hash="sha256:threshold",
    )
    service = planning_module.ReleasePolicyService(tenant_policies=[tenant_policy])
    lineage = _lineage(policy_id=policy.policy_id)
    facts = [
        planning_module.GateFact(
            fact_id="fact:threshold-1", gate="gate1", status="passed",
            plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
        ),
        planning_module.GateFact(
            fact_id="fact:threshold-2", gate="gate2", status="passed",
            plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
            artifact_hash=lineage.artifact_hash,
        ),
    ]
    blocked = service.evaluate(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id=policy.policy_id, policy_version=policy.version,
        gate_facts=facts, lineage=lineage,
        threshold_evidence_hash="sha256:stale",
    )
    assert blocked.status == "needs_review"
    assert "threshold_evidence_mismatch" in blocked.reasons


def test_low_risk_auto_release_requires_exact_tenant_threshold_and_allowlists():
    policy = planning_module.build_builtin_approval_policy_registry().resolve("low-risk-document-v1", "1")
    tenant_policy = planning_module.TenantReleasePolicy(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id=policy.policy_id, policy_version=policy.version,
        auto_release_enabled=True, allowed_formats=["docx"],
        allowlisted_recipe_ids=["generic-report"],
        threshold_evidence_hash="sha256:threshold",
    )
    service = planning_module.ReleasePolicyService(tenant_policies=[tenant_policy])
    lineage = _lineage(policy_id=policy.policy_id)
    facts = [
        planning_module.GateFact(
            fact_id="fact:gate1-low", gate="gate1", status="passed",
            plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
        ),
        planning_module.GateFact(
            fact_id="fact:gate2-low", gate="gate2", status="passed",
            plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
            artifact_hash=lineage.artifact_hash,
        ),
    ]
    released = service.evaluate(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id=policy.policy_id, policy_version=policy.version,
        gate_facts=facts, lineage=lineage, output_format="docx", recipe_id="generic-report",
    )
    assert released.release_allowed is True

    outside_allowlist = service.evaluate(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id=policy.policy_id, policy_version=policy.version,
        gate_facts=facts, lineage=lineage, output_format="pdf", recipe_id="generic-report",
    )
    assert outside_allowlist.release_allowed is False
    assert outside_allowlist.status == "needs_review"


def test_release_rejects_stale_policy_lineage_and_reconstructs_parent_chain():
    service = planning_module.ReleasePolicyService()
    lineage = _lineage()
    stale = lineage.model_copy(update={"policy_hash": "sha256:old-policy"})
    facts = [
        planning_module.GateFact(
            fact_id="fact:stale-1", gate="gate1", status="passed",
            plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
        ),
        planning_module.GateFact(
            fact_id="fact:stale-2", gate="gate2", status="passed",
            plan_id=lineage.plan_id, plan_version=1, plan_hash=lineage.plan_hash,
            artifact_hash=lineage.artifact_hash,
        ),
    ]
    result = service.evaluate(
        tenant_id="tenant-a", document_type="generic_report",
        policy_id="default-document-v1", policy_version="1",
        gate_facts=facts, lineage=stale,
    )
    assert result.status == "blocked"
    assert "policy_hash_mismatch" in result.reasons

    parent = _lineage(artifact_id="artifact:parent")
    child = _lineage(parent_artifact_id=parent.artifact_id)
    chain = service.reconstruct_lineage([child, parent], child.artifact_id)
    assert [item.artifact_id for item in chain] == [parent.artifact_id, child.artifact_id]
