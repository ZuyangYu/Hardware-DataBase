"""Full end-to-end integration test for the governed document authoring flow.

Exercises the full pipeline:
1. Project setup + baseline freeze
2. Template registration
3. Work order creation with an explicit deterministic HarnessPolicy
4. Harness execution
5. Artifact generation + saved to disk

Also validates the UNIQUE constraint regression fix by running _schema_harness_policy
twice with the same schema.

Run: uv run python -m pytest tests/test_full_generation_flow.py -v -s
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# Env-isolation guard: pin document gating switches to safe defaults so local
# .env overrides (e.g. DOCUMENT_AUTO_PUBLISH_VERIFIED=true) cannot flip the
# governed review flow these tests assert.
from tests.document_gating_env import pin_deterministic_document_gating  # noqa: F401

from src.agents.claim_evidence import InformationRequirement, RetrievalOutcome
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    HarnessPolicy,
    RendererPolicy,
    TemplateUnitBinding,
    TemplateVersion,
    WorkbookRegionSchema,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
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


def _xlsx_template() -> bytes:
    """Minimal XLSX with a single cell A1."""
    files = {
        "[Content_Types].xml": (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'  <Default Extension="xml" ContentType="application/xml"/>'
            b'  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            b'  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            b'</Types>'
        ),
        "_rels/.rels": (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            b'    Target="xl/workbook.xml"/>'
            b'</Relationships>'
        ),
        "xl/workbook.xml": (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            b'  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            b'  <sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets>'
            b'</workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"'
            b'    Target="worksheets/sheet1.xml"/>'
            b'</Relationships>'
        ),
        "xl/worksheets/sheet1.xml": (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b'  <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>MCU Model</t></is></c></row></sheetData>'
            b'</worksheet>'
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _prepare_project(tmp_path: Path):
    """Create a project with a frozen baseline and return key objects."""
    project_store = ProjectStore(str(tmp_path / "projects.db"))
    service = ProjectService(project_store)
    ctx = RequestContext(user_id="alice", tenant_id="tenant-a", roles=["user"])
    project = Project(project_id="project-full", tenant_id="tenant-a", department_id="hw", name="Full Flow Test")
    service.create_project(ctx, project)
    project_store.add_principal_binding(ProjectPrincipalBinding(
        binding_id="member-full", tenant_id="tenant-a", project_id=project.project_id,
        principal_type="user", principal_id="alice", project_role="project_admin",
    ))
    asset = project_store.create_source_asset(SourceAsset(
        asset_id="asset-full", tenant_id="tenant-a", original_file_name="design-spec.docx",
        content_hash="src-content", content_kind="document", parser_kind="rag_flow",
        processing_status="ready",
    ))
    document = project_store.create_logical_document(LogicalDocument(
        document_id="document-full", tenant_id="tenant-a", title="Design Spec",
        document_role="released_design", owner_department_id="hw",
    ))
    version = project_store.create_source_version(SourceVersion(
        version_id="version-full", tenant_id="tenant-a", document_id=document.document_id,
        asset_id=asset.asset_id, revision="A", approval_status="released",
    ))
    processing = project_store.create_processing_artifact(ProcessingArtifact(
        artifact_id="artifact-full", tenant_id="tenant-a", asset_id=asset.asset_id,
        processor_kind="rag_flow", processor_version="1", content_fingerprint="parse-full",
        status="ready",
    ))
    project_store.add_project_source_binding(ProjectSourceBinding(
        binding_id="binding-full", tenant_id="tenant-a", project_id=project.project_id,
        version_id=version.version_id, usage_type="project_fact",
    ))
    project_store.add_region_policy(SourceRegionPolicy(
        region_policy_id="policy-full", source_version_id=version.version_id,
        processing_artifact_id=processing.artifact_id, locator={"board": "main"},
        region_type="project_fact", allowed_evidence_uses=["review"], decision="allow",
        approved_by="admin",
    ))
    baseline = project_store.create_baseline(ProjectBaseline(
        baseline_id="baseline-full", tenant_id="tenant-a", project_id=project.project_id,
        name="Release A", status="approved", items=[BaselineItem(
            baseline_item_id="baseline-item-full", config_item_key="design_spec",
            source_role="released_design", source_version_id=version.version_id,
        )],
    ))
    return service, ctx, project, baseline, processing


def _setup_template_and_schema(
    service: DocumentGenerationService,
    *,
    field_count: int = 1,
) -> tuple[TemplateVersion, DocumentSchema]:
    """Register a renderer policy, schema, template, and return them."""
    content = _xlsx_template()

    service.register_renderer_policy(RendererPolicy(
        renderer_policy_id="policy-render-full", version="1",
    ))

    schema = service.register_document_schema(DocumentSchema(
        document_schema_id="schema-full", version="1", document_type="test",
        status="approved", execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id=f"field-{i}", label=f"Field {i}",
                description=f"Test field {i}",
                retrieval_policy_id=f"retrieval-{i}",
                verification_policy_id=f"verify-{i}",
                required_capabilities=["document_claim_lookup"],
                authoring_policy="managed_writer",
            )
            for i in range(field_count)
        ],
    ))

    template = service.register_template(
        TemplateVersion(
            template_version_id="template-full",
            template_id="full-flow-template",
            format="xlsx",
            content_hash=hashlib.sha256(content).hexdigest(),
            template_schema_id="schema-full",
            template_schema_version="1",
            renderer_policy_id="policy-render-full",
        ),
        content,
        regions=[WorkbookRegionSchema(
            region_id="region-full-0", sheet_name="Review",
            locator={"cell": "A1"}, role="semantic_draft",
            write_policy="validated_draft",
            allow_nonempty_overwrite=True,
        )],
        bindings=[TemplateUnitBinding(
            binding_id="binding-full-0",
            template_schema_id="schema-full",
            template_schema_version="1",
            semantic_unit_type="field",
            semantic_unit_id="field-0",
            target_region_ids=["region-full-0"],
        )],
    )
    service.approve_template(template.template_version_id, "admin")

    return template, schema


# ── tests ────────────────────────────────────────────────────────────────────


def test_full_generation_flow_with_deterministic_writer(tmp_path: Path):
    """Full end-to-end flow: project -> template -> work order -> harness -> artifact.

    Uses a deterministic writer (no LLM) so the test is offline-safe.  Also
    verifies the UNIQUE-constraint regression on _schema_harness_policy by
    running the flow twice with an auto-generated harness policy.
    """
    project_service, ctx, project, baseline, processing = _prepare_project(tmp_path)

    authoring_store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "authoring-files"),
    )
    service = DocumentGenerationService(project_service, authoring_store)
    template, schema = _setup_template_and_schema(service)

    # Register an explicit deterministic policy so we don't hit the LLM.
    deterministic_policy = service.register_harness_policy(HarnessPolicy(
        harness_policy_id="deterministic-writer", version="1", status="approved",
        writer_provider_id="deterministic_evidence_writer",
    ))

    # ── First run: create work order + run harness with deterministic policy ─
    order1 = service.create_document_work_order(
        ctx,
        project_id=project.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=schema.document_schema_id,
        document_schema_version=schema.version,
        harness_policy_id=deterministic_policy.harness_policy_id,
    )
    print(f"\n[RUN 1] Work order: {order1.work_order_id}")
    print(f"  harness_policy_id: {order1.harness_policy_id}")
    print(f"  source_set_snapshot_id: {order1.source_set_snapshot_id}")

    def retrieve(requirement: InformationRequirement, attempt: int, query_override: str | None = None) -> RetrievalOutcome:
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits",
            evidences=[EvidenceEnvelope(
                id="ev-full-1",
                content="MCU is STM32H743IIT6.",
                project_id=project.project_id,
                source_version_id="version-full",
                processing_artifact_id="artifact-full",
            )],
            query_fingerprint="q-full",
            applied_source_set_snapshot_id=order1.source_set_snapshot_id,
            applied_region_policy_versions={"policy-full": "1"},
        )

    candidate1 = service.run_internal_harness(ctx, order1.work_order_id, retrieve=retrieve)
    assert candidate1.stage == "review_candidate"
    run1 = authoring_store.get_harness_run(candidate1.run_id)
    assert run1 is not None and run1.status == "completed"
    draft1 = authoring_store.list_unit_drafts(candidate1.run_id)
    assert len(draft1) == 1
    assert draft1[0].validation_status == "supported"
    print(f"  Run status: {run1.status}")
    print(f"  Draft validation status: {draft1[0].validation_status}")

    # Verify artifact content
    artifact_content = authoring_store.read_artifact_content(candidate1.artifact_id)
    with zipfile.ZipFile(io.BytesIO(artifact_content)) as archive:
        rendered = "".join(ET.fromstring(archive.read("xl/worksheets/sheet1.xml")).itertext())
    assert "STM32H743IIT6" in rendered
    print(f"  Rendered content: {rendered}")

    # ── Save the generated artifact locally for user inspection ─────────
    output_dir = Path("storage/_test_output/full_generation_flow")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"generated-{candidate1.artifact_id}.xlsx"
    output_file.write_bytes(artifact_content)
    print(f"\n[SAVED] Generated artifact -> {output_file.resolve()}")

    # ── Second run: verifies the auto-generated policy UNIQUE fix ───────
    # Now use no harness_policy_id - triggers _schema_harness_policy path
    order2 = service.create_document_work_order(
        ctx,
        project_id=project.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=schema.document_schema_id,
        document_schema_version=schema.version,
    )
    print(f"\n[RUN 2] Auto-generated policy: {order2.harness_policy_id}")
    assert order2.harness_policy_version == "units-1-attempts-2-rewrite"

    order3 = service.create_document_work_order(
        ctx,
        project_id=project.project_id,
        baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=schema.document_schema_id,
        document_schema_version=schema.version,
    )
    print(f"[RUN 3] Auto-generated policy: {order3.harness_policy_id}")
    assert order3.harness_policy_id == order2.harness_policy_id

    policies = authoring_store.list_harness_policies()
    auto_policies = [p for p in policies if p.harness_policy_id == order2.harness_policy_id]
    assert len(auto_policies) == 1, (
        f"Expected 1 auto-generated policy row, got {len(auto_policies)}"
    )
    print("[VERIFY] Auto-generated policy stored exactly once (UNIQUE regression fixed)")

    print("\n=== Full generation flow PASSED ===")


def test_schema_harness_policy_idempotent_repeated_calls(tmp_path: Path):
    """Call _schema_harness_policy twice with the same schema - must not raise UNIQUE."""
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "authoring-files"),
    )
    service = DocumentGenerationService(
        ProjectService(ProjectStore(str(tmp_path / "projects.db"))),
        store,
    )
    service.register_renderer_policy(RendererPolicy(
        renderer_policy_id="policy-idemp", version="1",
    ))
    schema = service.register_document_schema(DocumentSchema(
        document_schema_id="schema-idemp", version="1", document_type="test",
        status="approved", execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id="f1", label="F1", retrieval_policy_id="r1",
            verification_policy_id="v1", authoring_policy="managed_writer",
        )],
    ))

    policy1 = service._schema_harness_policy(schema)
    assert policy1 is not None

    # Second call - regression test for UNIQUE constraint failure
    policy2 = service._schema_harness_policy(schema)
    assert policy2 is not None
    assert policy2.harness_policy_id == policy1.harness_policy_id
    assert policy2.version == policy1.version

    # Third call for good measure
    policy3 = service._schema_harness_policy(schema)
    assert policy3.harness_policy_id == policy1.harness_policy_id

    policies = store.list_harness_policies()
    matching = [p for p in policies if p.harness_policy_id == policy1.harness_policy_id]
    assert len(matching) == 1, f"Expected 1 row, got {len(matching)}"
    print("\n=== Idempotent _schema_harness_policy PASSED ===")


def test_schema_harness_policy_scales_to_large_schema(tmp_path: Path):
    """Verify the budget formulas scale correctly for a schema with many units."""
    service = DocumentGenerationService(
        ProjectService(ProjectStore(str(tmp_path / "projects.db"))),
        DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files")),
    )
    service.register_renderer_policy(RendererPolicy(
        renderer_policy_id="policy-large", version="1",
    ))

    schema = service.register_document_schema(DocumentSchema(
        document_schema_id="schema-large", version="1", document_type="test",
        status="approved", execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id=f"f{i}", label=f"F{i}", retrieval_policy_id=f"r{i}",
                verification_policy_id=f"v{i}", authoring_policy="managed_writer",
            )
            for i in range(500)
        ],
    ))

    policy = service._schema_harness_policy(schema)
    assert policy.max_units_per_run == 500
    assert policy.max_retrieval_rounds == 1000
    assert policy.max_steps == 2 + 500 * 6
    print(f"\n=== Large schema (500 units) policy: steps={policy.max_steps} PASSED ===")


def test_schema_harness_policy_rejects_over_cap(tmp_path: Path):
    """Reject a schema with more units than _MAX_AUTO_HARNESS_UNITS allows."""
    import pytest

    service = DocumentGenerationService(
        ProjectService(ProjectStore(str(tmp_path / "projects.db"))),
        DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "authoring-files")),
    )
    service.register_renderer_policy(RendererPolicy(
        renderer_policy_id="policy-reject", version="1",
    ))

    schema = service.register_document_schema(DocumentSchema(
        document_schema_id="schema-over", version="1", document_type="test",
        status="approved", execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id=f"f{i}", label=f"F{i}", retrieval_policy_id=f"r{i}",
                verification_policy_id=f"v{i}", authoring_policy="managed_writer",
            )
            for i in range(501)
        ],
    ))

    with pytest.raises(ValueError, match="schema semantic unit count exceeds"):
        service._schema_harness_policy(schema)
    print("\n=== Over-cap schema rejected PASSED ===")
