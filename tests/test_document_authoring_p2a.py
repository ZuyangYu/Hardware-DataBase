from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest

from src.agents.claim_evidence import RetrievalOutcome
from src.document_authoring.models import (
    DeterministicRuleSpec,
    DocumentArtifact,
    DocumentFieldSchema,
    DocumentHumanEvent,
    DocumentSchema,
    DocumentWorkOrder,
    DraftAssertion,
    DocumentUnitDraft,
    HarnessPolicy,
    HarnessCheckpoint,
    HarnessRun,
    LegacyTemplateClaim,
    NodeExecutionReceipt,
    RendererPolicy,
    ReviewItemSchema,
    TemplateUnitBinding,
    TemplateVersion,
    WorkbookRegionSchema,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.harness.graph import AuthoringGraph
from src.document_authoring.harness.policy import HarnessToolPolicy
from src.document_authoring.harness.policy import HarnessLeaseLost
from src.document_authoring.writers.managed import CallableWriter, ManagedWriter
from src.pipelines.document_rag.schemas import EvidenceEnvelope, RequestContext
from src.projects.models import (
    BaselineItem,
    LogicalDocument,
    ProcessingArtifact,
    Project,
    ProjectBaseline,
    ProjectPrincipalBinding,
    ProjectSourceBinding,
    SourceAsset,
    SourceRegionPolicy,
    SourceVersion,
)
from src.projects.service import ProjectService
from src.projects.store import ProjectStore
from src.projects.retrieval import ProjectEvidenceRetrievalService
from src.agents.claim_evidence import InformationRequirement


def _xlsx_template() -> bytes:
    files = {
        "[Content_Types].xml": b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''',
        "_rels/.rels": b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''',
        "xl/workbook.xml": b'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets>
</workbook>''',
        "xl/_rels/workbook.xml.rels": b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": b'''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Legacy result</t></is></c></row></sheetData>
</worksheet>''',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _prepare_project(tmp_path: Path):
    project_store = ProjectStore(str(tmp_path / "projects.db"))
    service = ProjectService(project_store)
    ctx = RequestContext(user_id="alice", tenant_id="tenant-a", roles=["user"])
    project = Project(project_id="project-a", tenant_id="tenant-a", department_id="hw", name="ADAS")
    service.create_project(ctx, project)
    project_store.add_principal_binding(ProjectPrincipalBinding(
        binding_id="member-a", tenant_id="tenant-a", project_id=project.project_id,
        principal_type="user", principal_id="alice", project_role="project_admin",
    ))
    asset = project_store.create_source_asset(SourceAsset(
        asset_id="asset-a", tenant_id="tenant-a", original_file_name="review.edf",
        content_hash="source-content", content_kind="circuit_design", parser_kind="edf",
        processing_status="ready",
    ))
    document = project_store.create_logical_document(LogicalDocument(
        document_id="document-a", tenant_id="tenant-a", title="Main schematic",
        document_role="released_schematic", owner_department_id="hw",
    ))
    version = project_store.create_source_version(SourceVersion(
        version_id="version-a", tenant_id="tenant-a", document_id=document.document_id,
        asset_id=asset.asset_id, revision="A", approval_status="released",
    ))
    processing = project_store.create_processing_artifact(ProcessingArtifact(
        artifact_id="processing-a", tenant_id="tenant-a", asset_id=asset.asset_id,
        processor_kind="circuit", processor_version="1", content_fingerprint="parse-a", status="ready",
    ))
    project_store.add_project_source_binding(ProjectSourceBinding(
        binding_id="source-binding-a", tenant_id="tenant-a", project_id=project.project_id,
        version_id=version.version_id, usage_type="project_fact",
    ))
    project_store.add_region_policy(SourceRegionPolicy(
        region_policy_id="policy-a", source_version_id=version.version_id,
        processing_artifact_id=processing.artifact_id, locator={"board": "main"},
        region_type="project_fact", allowed_evidence_uses=["review"], decision="allow",
        approved_by="reviewer",
    ))
    baseline = project_store.create_baseline(ProjectBaseline(
        baseline_id="baseline-a", tenant_id="tenant-a", project_id=project.project_id,
        name="Release A", status="approved", items=[BaselineItem(
            baseline_item_id="baseline-item-a", config_item_key="main_schematic",
            source_role="released_schematic", source_version_id=version.version_id,
        )],
    ))
    return service, ctx, project, baseline, processing


def test_source_set_snapshot_freezes_approved_versions_and_regions(tmp_path: Path):
    service, ctx, project, baseline, processing = _prepare_project(tmp_path)

    snapshot = service.create_source_set_snapshot(
        ctx, work_order_id="work-a", project_id=project.project_id, baseline_id=baseline.baseline_id,
    )

    assert snapshot.baseline_content_hash == baseline.content_hash
    assert snapshot.source_version_ids == ["version-a"]
    assert snapshot.processing_artifact_ids == [processing.artifact_id]
    assert snapshot.region_policy_versions == {"policy-a": "1"}


def test_project_retrieval_fails_closed_when_adapter_broadens_source_scope(tmp_path: Path):
    service, ctx, project, baseline, processing = _prepare_project(tmp_path)
    snapshot = service.create_source_set_snapshot(
        ctx, work_order_id="work-retrieval", project_id=project.project_id, baseline_id=baseline.baseline_id,
    )
    retrieval = ProjectEvidenceRetrievalService(service)
    requirement = InformationRequirement(
        requirement_id="need-network", semantic_unit_id="network", claim_type="relationship",
        subject="CAN_H", required_capabilities=["relationship_lookup"], project_id=project.project_id,
    )

    outcome = retrieval.retrieve(
        ctx, requirement, snapshot.source_set_snapshot_id,
        lambda version_id, artifact_ids, policies: [EvidenceEnvelope(
            id="wrong", content="not permitted", project_id=project.project_id,
            source_version_id="other-version", processing_artifact_id=processing.artifact_id,
        )],
    )

    assert outcome.status == "retrieval_failed"
    assert outcome.evidences == []
    assert outcome.source_outcomes[0].status == "filter_unsupported"


def test_p2a_xlsx_candidate_and_hash_bound_release(tmp_path: Path):
    project_service, ctx, project, baseline, _ = _prepare_project(tmp_path)
    authoring_store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    service = DocumentGenerationService(project_service, authoring_store)
    template_content = _xlsx_template()
    template_hash = hashlib.sha256(template_content).hexdigest()
    policy = service.register_renderer_policy(RendererPolicy(renderer_policy_id="policy-render", version="1"))
    schema = service.register_document_schema(DocumentSchema(
        document_schema_id="schematic-review", version="1", document_type="schematic_design_review_checklist_xlsm",
        status="approved", execution_mode="deterministic_only", review_items=[ReviewItemSchema(
            review_item_id="network-name", label="Network name", evaluation_mode="deterministic_auto",
            retrieval_rule_id="network-evidence", deterministic_rule_id="network-exact", pass_policy_id="all-match",
        )],
    ))
    service.register_deterministic_rule(DeterministicRuleSpec(
        rule_id="network-exact", rule_version="1", operation="exact_match",
        input_requirements=["actual", "expected"], capability="relationship_lookup",
        approved_operation_name="exact_match", expected_value_type="text", implementation_version="1",
    ))
    template = service.register_template(
        TemplateVersion(
            template_version_id="template-a", template_id="schematic-review", format="xlsx",
            content_hash=template_hash, template_schema_id="workbook-review", template_schema_version="1",
            renderer_policy_id=policy.renderer_policy_id,
        ),
        template_content,
        regions=[WorkbookRegionSchema(
            region_id="network-result", sheet_name="Review", locator={"cell": "A1"},
            role="evidence_derived", write_policy="deterministic_only",
        )],
        bindings=[TemplateUnitBinding(
            binding_id="bind-network-result", template_schema_id="workbook-review", template_schema_version="1",
            semantic_unit_type="review_item", semantic_unit_id="network-name", target_region_ids=["network-result"],
        )],
    )
    assert template.status == "draft"
    service.approve_template(template.template_version_id, actor_id="template-admin")

    order = service.create_document_work_order(
        ctx, project_id=project.project_id, baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=schema.document_schema_id, document_schema_version=schema.version,
    )
    candidate = service.run_deterministic_work_order(
        ctx, order.work_order_id,
        rule_inputs={"network-name": {"actual": "CAN_H", "expected": "can_h"}},
        retrieval_outcomes={"network-evidence": RetrievalOutcome(
            requirement_id="network-evidence", status="success_with_hits",
            evidences=[EvidenceEnvelope(
                id="evidence-a", content="CAN_H", project_id=project.project_id,
                source_version_id="version-a", processing_artifact_id="processing-a",
            )],
            query_fingerprint="query-a", applied_source_set_snapshot_id=order.source_set_snapshot_id,
            applied_region_policy_versions={"policy-a": "1"},
        )},
    )
    assert candidate.stage == "review_candidate"
    assert candidate.content_hash == hashlib.sha256(authoring_store.read_artifact_content(candidate.artifact_id)).hexdigest()
    with zipfile.ZipFile(io.BytesIO(authoring_store.read_artifact_content(candidate.artifact_id))) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rendered = "".join(root.itertext())
    assert "PASS: CAN_H == can_h" in rendered

    released = service.approve_document_artifact(ctx, candidate.artifact_id)
    assert released.stage == "approved_release"
    assert released.content_hash == candidate.content_hash
    assert authoring_store.read_artifact_content(released.artifact_id) == authoring_store.read_artifact_content(candidate.artifact_id)
    assert released.approval_subject_hash
    assert service.download_document_artifact(ctx, released.artifact_id)
    repeated_release = service.approve_document_artifact(ctx, candidate.artifact_id)
    assert repeated_release.artifact_id == released.artifact_id
    assert len(authoring_store.list_human_events(candidate.artifact_id)) == 1
    assert len(authoring_store.list_artifacts(order.work_order_id)) == 2


def test_formula_like_fill_is_rejected_by_renderer_policy(tmp_path: Path):
    project_service, ctx, project, baseline, _ = _prepare_project(tmp_path)
    authoring_store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    service = DocumentGenerationService(project_service, authoring_store)
    content = _xlsx_template()
    service.register_renderer_policy(RendererPolicy(renderer_policy_id="policy-render", version="1"))
    service.register_document_schema(DocumentSchema(
        document_schema_id="schema", version="1", document_type="review", status="approved",
        review_items=[ReviewItemSchema(review_item_id="review", label="Review", evaluation_mode="deterministic_auto", retrieval_rule_id="r", deterministic_rule_id="rule", pass_policy_id="p")],
    ))
    service.register_deterministic_rule(DeterministicRuleSpec(
        rule_id="rule", rule_version="1", operation="exact_match", input_requirements=["actual", "expected"],
        capability="relationship_lookup", approved_operation_name="exact_match", expected_value_type="text", implementation_version="1",
    ))
    template = service.register_template(
        TemplateVersion(template_version_id="t", template_id="t", format="xlsx", content_hash=hashlib.sha256(content).hexdigest(), template_schema_id="ts", template_schema_version="1", renderer_policy_id="policy-render"),
        content,
        regions=[WorkbookRegionSchema(region_id="cell", sheet_name="Review", locator={"cell": "A1"}, role="evidence_derived", write_policy="deterministic_only")],
        bindings=[TemplateUnitBinding(binding_id="binding", template_schema_id="ts", template_schema_version="1", semantic_unit_type="review_item", semantic_unit_id="review", target_region_ids=["cell"])],
    )
    service.approve_template(template.template_version_id, "admin")
    order = service.create_document_work_order(ctx, project_id=project.project_id, baseline_id=baseline.baseline_id, template_version_id="t", document_schema_id="schema", document_schema_version="1")
    # The rendered status is controlled by the service and therefore cannot be
    # formula-like. The direct renderer guard is covered by passing a crafted
    # workbook fill through its public API below.
    from src.document_authoring.models import WorkbookFill, WorkbookFillPlan
    with pytest.raises(ValueError, match="formula-like"):
        service.renderer.render(
            content,
            authoring_store.list_workbook_regions("ts", "1"),
            WorkbookFillPlan(template_version_id="t", fills=[WorkbookFill(region_id="cell", semantic_unit_id="review", value="=HYPERLINK(\"bad\")")]),
            authoring_store.get_renderer_policy("policy-render"),
            security_approved=True,
        )
    assert order.status == "planned"


def test_internal_harness_writes_only_validated_evidence_draft(tmp_path: Path):
    project_service, ctx, project, baseline, _ = _prepare_project(tmp_path)
    authoring_store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    service = DocumentGenerationService(project_service, authoring_store)
    content = _xlsx_template()
    service.register_renderer_policy(RendererPolicy(renderer_policy_id="policy-render", version="1"))
    policy = service.register_harness_policy(HarnessPolicy(
        harness_policy_id="bounded-writer", version="1", status="approved",
        writer_provider_id="deterministic_evidence_writer",
    ))
    schema = service.register_document_schema(DocumentSchema(
        document_schema_id="semantic-schema", version="1", document_type="hardware_design_spec",
        status="approved", execution_mode="internal_harness", fields=[DocumentFieldSchema(
            field_id="controller", label="主控制器", description="已批准主控制器型号",
            retrieval_policy_id="controller-evidence", verification_policy_id="controller-verify",
            required_capabilities=["document_claim_lookup"], authoring_policy="managed_writer",
        )],
    ))
    template = service.register_template(
        TemplateVersion(
            template_version_id="semantic-template", template_id="hardware-design-spec", format="xlsx",
            content_hash=hashlib.sha256(content).hexdigest(), template_schema_id="semantic-workbook",
            template_schema_version="1", renderer_policy_id="policy-render",
        ),
        content,
        regions=[WorkbookRegionSchema(
            region_id="controller-cell", sheet_name="Review", locator={"cell": "A1"},
            role="semantic_draft", write_policy="validated_draft",
        )],
        bindings=[TemplateUnitBinding(
            binding_id="controller-binding", template_schema_id="semantic-workbook", template_schema_version="1",
            semantic_unit_type="field", semantic_unit_id="controller", target_region_ids=["controller-cell"],
        )],
    )
    service.approve_template(template.template_version_id, "template-admin")
    order = service.create_document_work_order(
        ctx, project_id=project.project_id, baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id, document_schema_id=schema.document_schema_id,
        document_schema_version=schema.version, harness_policy_id=policy.harness_policy_id,
    )

    def retrieve(requirement, attempt, query_override=None):
        assert requirement.semantic_unit_id == "field:controller"
        assert attempt == 1
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id, status="success_with_hits",
            evidences=[EvidenceEnvelope(
                id="controller-evidence", content="主控制器为 STM32H743IIT6。", project_id=project.project_id,
                source_version_id="version-a", processing_artifact_id="processing-a",
            )],
            query_fingerprint="controller-query", applied_source_set_snapshot_id=order.source_set_snapshot_id,
            applied_region_policy_versions={"policy-a": "1"},
        )

    # A later policy revision must not alter the frozen work order's writer
    # allowlist or budget.
    service.register_harness_policy(HarnessPolicy(
        harness_policy_id=policy.harness_policy_id, version="2", status="approved",
        writer_provider_id="some-other-provider",
    ))
    candidate = service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)

    assert candidate.stage == "review_candidate"
    run = authoring_store.get_harness_run(candidate.run_id)
    assert run is not None and run.status == "completed"
    manifest = authoring_store.get_run_manifest(run.run_manifest_id)
    assert manifest is not None
    assert manifest.harness_policy_version == "1"
    assert manifest.source_set_snapshot_hash
    assert manifest.evidence_content_hashes["controller-evidence"] == hashlib.sha256(
        "主控制器为 STM32H743IIT6。".encode("utf-8")
    ).hexdigest()
    draft = authoring_store.list_unit_drafts(candidate.run_id)[0]
    assert draft.validation_status == "supported"
    with zipfile.ZipFile(io.BytesIO(authoring_store.read_artifact_content(candidate.artifact_id))) as archive:
        rendered = "".join(ET.fromstring(archive.read("xl/worksheets/sheet1.xml")).itertext())
    assert "STM32H743IIT6" in rendered


def test_internal_harness_detects_legacy_template_contamination(tmp_path: Path):
    project_service, ctx, project, baseline, _ = _prepare_project(tmp_path)
    authoring_store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    service = DocumentGenerationService(project_service, authoring_store)
    content = _xlsx_template()
    service.register_renderer_policy(RendererPolicy(renderer_policy_id="policy-render", version="1"))
    policy = service.register_harness_policy(HarnessPolicy(
        harness_policy_id="custom-writer", version="1", status="approved", writer_provider_id="callable_writer",
    ))
    schema = service.register_document_schema(DocumentSchema(
        document_schema_id="contamination-schema", version="1", document_type="hardware_design_spec",
        status="approved", execution_mode="internal_harness", fields=[DocumentFieldSchema(
            field_id="controller", label="主控制器", retrieval_policy_id="controller-evidence",
            verification_policy_id="controller-verify", authoring_policy="managed_writer",
        )],
    ))
    template = service.register_template(
        TemplateVersion(
            template_version_id="contamination-template", template_id="hardware-design-spec", format="xlsx",
            content_hash=hashlib.sha256(content).hexdigest(), template_schema_id="contamination-workbook",
            template_schema_version="1", renderer_policy_id="policy-render",
        ),
        content,
        regions=[WorkbookRegionSchema(
            region_id="controller-cell", sheet_name="Review", locator={"cell": "A1"},
            role="semantic_draft", write_policy="validated_draft",
        )],
        bindings=[TemplateUnitBinding(
            binding_id="controller-binding", template_schema_id="contamination-workbook", template_schema_version="1",
            semantic_unit_type="field", semantic_unit_id="controller", target_region_ids=["controller-cell"],
        )],
        legacy_claims=[LegacyTemplateClaim(claim_id="legacy-controller", text="LEGACY-CTRL-01", locator={"cell": "A1"})],
    )
    service.approve_template(template.template_version_id, "template-admin")
    order = service.create_document_work_order(
        ctx, project_id=project.project_id, baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id, document_schema_id=schema.document_schema_id,
        document_schema_version=schema.version, harness_policy_id=policy.harness_policy_id,
    )

    def writer_callback(request):
        return DocumentUnitDraft(
            unit_id=request.unit_id, run_id=request.run_id, generated_by="managed_writer",
            content="依据证据，主控制器为 LEGACY-CTRL-01。", proposed_value="LEGACY-CTRL-01",
            evidence_ids=["controller-evidence"], assertions=[DraftAssertion(
                assertion_id="assertion-a", claim_id="claim-controller", text="主控制器为 LEGACY-CTRL-01。",
                evidence_ids=["controller-evidence"], value="LEGACY-CTRL-01",
            )],
        )

    def retrieve(requirement, attempt, query_override=None):
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id, status="success_with_hits",
            evidences=[EvidenceEnvelope(
                id="controller-evidence", content="主控制器为 LEGACY-CTRL-01。", project_id=project.project_id,
                source_version_id="version-a", processing_artifact_id="processing-a",
            )],
            query_fingerprint="controller-query", applied_source_set_snapshot_id=order.source_set_snapshot_id,
            applied_region_policy_versions={"policy-a": "1"},
        )

    candidate = service.run_internal_harness(
        ctx, order.work_order_id, retrieve=retrieve, writer=ManagedWriter(CallableWriter(writer_callback)),
    )

    report = authoring_store.get_validation_report(candidate.validation_report_id)
    assert report is not None and report.status == "requires_human"
    assert any(issue["kind"] == "template_contamination" for issue in report.issues)
    assert authoring_store.get_work_order(order.work_order_id).status == "waiting_human_input"


def test_internal_harness_budget_exhaustion_routes_to_human_review(tmp_path: Path):
    project_service, ctx, project, baseline, _ = _prepare_project(tmp_path)
    authoring_store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    service = DocumentGenerationService(project_service, authoring_store)
    content = _xlsx_template()
    service.register_renderer_policy(RendererPolicy(renderer_policy_id="policy-render", version="1"))
    policy = service.register_harness_policy(HarnessPolicy(
        harness_policy_id="tiny-budget", version="1", status="approved", max_steps=1,
        writer_provider_id="deterministic_evidence_writer",
    ))
    schema = service.register_document_schema(DocumentSchema(
        document_schema_id="budget-schema", version="1", document_type="hardware_design_spec",
        status="approved", execution_mode="internal_harness", fields=[DocumentFieldSchema(
            field_id="controller", label="主控制器", retrieval_policy_id="controller-evidence",
            verification_policy_id="controller-verify", authoring_policy="managed_writer",
        )],
    ))
    template = service.register_template(
        TemplateVersion(
            template_version_id="budget-template", template_id="hardware-design-spec", format="xlsx",
            content_hash=hashlib.sha256(content).hexdigest(), template_schema_id="budget-workbook",
            template_schema_version="1", renderer_policy_id="policy-render",
        ),
        content,
        regions=[WorkbookRegionSchema(
            region_id="controller-cell", sheet_name="Review", locator={"cell": "A1"},
            role="semantic_draft", write_policy="validated_draft",
        )],
        bindings=[TemplateUnitBinding(
            binding_id="controller-binding", template_schema_id="budget-workbook", template_schema_version="1",
            semantic_unit_type="field", semantic_unit_id="controller", target_region_ids=["controller-cell"],
        )],
    )
    service.approve_template(template.template_version_id, "template-admin")
    order = service.create_document_work_order(
        ctx, project_id=project.project_id, baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id, document_schema_id=schema.document_schema_id,
        document_schema_version=schema.version, harness_policy_id=policy.harness_policy_id,
    )

    def retrieve(requirement, attempt, query_override=None):
        raise AssertionError("budget must stop before invoking retrieval")

    candidate = service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)

    run = authoring_store.get_harness_run(candidate.run_id)
    report = authoring_store.get_validation_report(candidate.validation_report_id)
    assert run is not None and run.status == "waiting_human"
    assert report is not None and report.status == "requires_human"
    assert any(issue["kind"] == "harness_budget_exceeded" for issue in report.issues)
    assert authoring_store.get_work_order(order.work_order_id).status == "waiting_human_input"


def test_harness_retries_each_unit_with_a_separate_global_budget():
    policy = HarnessPolicy(
        harness_policy_id="retrieval-budget",
        version="1",
        status="approved",
        max_steps=20,
        max_retrieval_rounds=4,
        max_retrieval_attempts_per_unit=1,
    )
    graph = AuthoringGraph(HarnessToolPolicy(policy), Mock())
    state = {"step_count": 0, "retrieval_round_count": 0}
    first_attempts: list[int] = []
    second_attempts: list[int] = []
    first_requirement = InformationRequirement(
        requirement_id="first",
        semantic_unit_id="field:first",
        claim_type="attribute",
        subject="first",
    )
    second_requirement = InformationRequirement(
        requirement_id="second",
        semantic_unit_id="field:second",
        claim_type="attribute",
        subject="second",
    )

    def first_retrieve(requirement, attempt, query_override=None):
        first_attempts.append(attempt)
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="retrieval_failed",
            query_fingerprint=f"first-{attempt}",
            applied_source_set_snapshot_id="snapshot",
        )

    def second_retrieve(requirement, attempt, query_override=None):
        second_attempts.append(attempt)
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_empty",
            query_fingerprint=f"second-{attempt}",
            applied_source_set_snapshot_id="snapshot",
        )

    graph._retrieve_with_budget(state, first_requirement, first_retrieve)
    graph._retrieve_with_budget(state, second_requirement, second_retrieve)

    assert policy.max_retrieval_attempts_per_unit == 1
    assert first_attempts == [1]
    assert second_attempts == [1]
    assert state["retrieval_round_count"] == 2


def test_harness_fencing_checkpoint_and_node_receipts_are_durable(tmp_path: Path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    order = store.create_work_order(DocumentWorkOrder(
        work_order_id="wo-lease", tenant_id="tenant-a", project_id="project-a", baseline_id="baseline-a",
        baseline_content_hash="baseline-hash", source_set_snapshot_id="snapshot-a",
        template_version_id="template-a", document_schema_id="schema-a", document_schema_version="1",
        template_schema_id="template-schema-a", template_schema_version="1", retrieval_policy_version="1",
        renderer_policy_version="1", target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id="harness-policy", harness_policy_version="1", created_by="alice",
    ))
    run = store.create_harness_run(HarnessRun(
        harness_run_id="harness-lease", work_order_id=order.work_order_id, run_manifest_id="manifest-lease",
        status="queued", max_retries=1,
    ))
    claimed = store.claim_harness_run(run.harness_run_id, "worker-1", lease_seconds=60)
    assert claimed.fencing_token == 1
    checkpoint = HarnessCheckpoint(
        checkpoint_id="checkpoint-lease", harness_run_id=run.harness_run_id, work_order_id=order.work_order_id,
        input_fingerprint=order.input_fingerprint, source_set_snapshot_id=order.source_set_snapshot_id,
        fencing_token=claimed.fencing_token, current_node="retrieve_requirement_evidence", step_count=2,
    )
    store.save_harness_checkpoint_owned(checkpoint, "worker-1", claimed.fencing_token)

    paused = store.request_harness_run_state(run.harness_run_id, "paused")
    assert paused.fencing_token == 2
    with pytest.raises(HarnessLeaseLost):
        store.update_harness_run_owned(run.harness_run_id, "worker-1", claimed.fencing_token, current_node="stale")

    retried = store.queue_harness_retry(run.harness_run_id, max_retries=1)
    assert retried.status == "retrying" and retried.retry_count == 1
    reclaimed = store.claim_harness_run(run.harness_run_id, "worker-2", lease_seconds=60)
    assert reclaimed.fencing_token == 3
    started = store.begin_node_execution_owned(NodeExecutionReceipt(
        receipt_id="receipt-lease", harness_run_id=run.harness_run_id, node_name="draft_ready_unit",
        unit_id="field:controller", input_fingerprint="writer-input-hash", fencing_token=reclaimed.fencing_token,
    ), "worker-2", reclaimed.fencing_token)
    committed = store.commit_node_execution_owned(
        started.receipt_id, run.harness_run_id, "worker-2", reclaimed.fencing_token, {"value": "STM32H743"},
    )
    duplicate = store.begin_node_execution_owned(NodeExecutionReceipt(
        receipt_id="receipt-duplicate", harness_run_id=run.harness_run_id, node_name="draft_ready_unit",
        unit_id="field:controller", input_fingerprint="writer-input-hash", fencing_token=reclaimed.fencing_token,
    ), "worker-2", reclaimed.fencing_token)
    assert duplicate.receipt_id == committed.receipt_id
    assert duplicate.output_payload == {"value": "STM32H743"}


def test_artifact_and_human_event_submission_are_idempotent(tmp_path: Path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    order = store.create_work_order(DocumentWorkOrder(
        work_order_id="wo-idempotent", tenant_id="tenant-a", project_id="project-a", baseline_id="baseline-a",
        baseline_content_hash="baseline-hash", source_set_snapshot_id="snapshot-a",
        template_version_id="template-a", document_schema_id="schema-a", document_schema_version="1",
        template_schema_id="template-schema-a", template_schema_version="1", retrieval_policy_version="1",
        renderer_policy_version="1", target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id="harness-policy", harness_policy_version="1", created_by="alice",
    ))
    artifact = DocumentArtifact(
        artifact_id="artifact-first", tenant_id="tenant-a", work_order_id=order.work_order_id,
        run_id="harness-idempotent", stage="review_candidate", content_hash="candidate-content-hash",
        validation_report_id="report-a", integrity_manifest_id="manifest-a",
    )
    first = store.save_artifact(artifact, b"candidate bytes", "xlsx")
    second = store.save_artifact(
        artifact.model_copy(update={"artifact_id": "artifact-retry", "storage_ref": ""}),
        b"candidate bytes",
        "xlsx",
    )
    assert second.artifact_id == first.artifact_id
    assert len(store.list_artifacts(order.work_order_id)) == 1

    event = DocumentHumanEvent(
        event_id="event-first", work_order_id=order.work_order_id, run_id=first.run_id,
        artifact_id=first.artifact_id, unit_id="artifact", event_type="approve",
        subject_artifact_content_hash=first.content_hash, approval_subject_hash="approval-subject-a",
        actor_id="alice", actor_role="project_admin", comment="approved",
    )
    first_event = store.save_human_event(event)
    second_event = store.save_human_event(event.model_copy(update={"event_id": "event-retry"}))
    assert second_event.event_id == first_event.event_id
    assert len(store.list_human_events(first.artifact_id)) == 1
    event_types = {item.event_type for item in store.list_pending_outbox_events()}
    assert "document_work_order.created" in event_types
    assert "document_artifact.review_candidate_created" in event_types
    assert "document_human_event.approve_recorded" in event_types


def test_work_order_state_changes_use_transactional_outbox(tmp_path: Path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files"))
    order = store.create_work_order(DocumentWorkOrder(
        work_order_id="wo-outbox", tenant_id="tenant-a", project_id="project-a", baseline_id="baseline-a",
        baseline_content_hash="baseline-hash", source_set_snapshot_id="snapshot-a",
        template_version_id="template-a", document_schema_id="schema-a", document_schema_version="1",
        template_schema_id="template-schema-a", template_schema_version="1", retrieval_policy_version="1",
        renderer_policy_version="1", target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id="harness-policy", harness_policy_version="1", created_by="alice",
    ))
    revised = order.model_copy(update={"status": "retrieving", "lock_version": 1})
    store.replace_work_order(revised)

    events = store.list_pending_outbox_events()
    changed = next(event for event in events if event.event_type == "document_work_order.state_changed")
    assert changed.payload["status"] == "retrieving"
    assert changed.payload["lock_version"] == 1
    failed = store.mark_outbox_event_failed(changed.event_id, "temporary delivery failure")
    assert failed.status == "failed" and failed.delivery_attempts == 1
    delivered = store.mark_outbox_event_delivered(changed.event_id)
    assert delivered.status == "delivered" and delivered.delivery_attempts == 2
    assert changed.event_id not in {event.event_id for event in store.list_pending_outbox_events()}
