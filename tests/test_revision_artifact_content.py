"""End-to-end content assertions for governed document revisions.

The revision bridge is only complete when a child artifact's actual OOXML
content changes.  These tests intentionally exercise the plan compiler,
Harness, renderer, artifact store and revision binding together.
"""

from __future__ import annotations

import io
import hashlib
import zipfile
from xml.etree import ElementTree as ET

import src.settings

from src.document_authoring.models import (
    DocumentSchema,
    HarnessPolicy,
    KnowledgeBaseSourceSnapshot,
    RendererPolicy,
    TemplateUnitBinding,
    TemplateVersion,
)
from src.document_authoring.template_analysis import DocxRegionSchema
from src.pipelines.document_rag.schemas import EvidenceEnvelope, RequestContext
from src.agents.claim_evidence import RetrievalOutcome
from src.document_authoring.planning.models import DocumentPlan, OutputSpec
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore

from tests.test_conversation_led_document_authoring_phase2_e2e import _make_fixture
from tests.test_document_planning_contracts import _output_payload, _plan_payload


def _worksheet_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        root = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))
    return "".join(root.itertext())


def _docx_template() -> bytes:
    parts = {
        "[Content_Types].xml": (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b'<Override PartName="/word/document.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            b"</Types>"
        ),
        "_rels/.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            b'Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:body>'
            b'<w:p><w:r><w:t>Cover placeholder</w:t></w:r></w:p>'
            b'<w:p><w:r><w:t>Details placeholder</w:t></w:r></w:p>'
            b'<w:sectPr/></w:body></w:document>'
        ),
        "word/_rels/document.xml.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return output.getvalue()


def _docx_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    return "".join(root.itertext())


def _docx_revision_plan(snapshot: KnowledgeBaseSourceSnapshot, template_hash: str) -> tuple[OutputSpec, DocumentPlan]:
    spec_payload = _output_payload()
    spec_payload["status"] = "accepted"
    spec_payload["artifact"]["deliverables"][0]["format"] = "docx"
    spec_payload["outline"] = [
        {"unit_id": "cover", "kind": "section", "title": "Cover", "required": True},
        {"unit_id": "details", "kind": "section", "title": "Details", "required": True},
    ]
    spec_payload["table_requirements"] = []
    spec_payload["layout_source"] = {
        "mode": "provided_template",
        "template_version_id": "docx-template-001",
        "template_schema_id": "docx-schema-001",
        "template_schema_version": "1",
    }
    spec = OutputSpec.model_validate(spec_payload)
    plan_payload = _plan_payload(
        status="accepted",
        output_spec_hash=spec.content_hash,
        source_snapshot_id=snapshot.source_set_snapshot_id,
        source_snapshot_hash=snapshot.content_hash,
        renderer_capability_id="docx",
        required_capabilities=["kb.read@1", "docx@1"],
        resolved_capabilities=["kb.read@1", "docx@1"],
        layout_contract={
            "kind": "template",
            "template_version_id": "docx-template-001",
            "template_schema_id": "docx-schema-001",
            "template_schema_version": "1",
            "bindings": {"cover": ["cover-region"], "details": ["details-region"]},
        },
        output_spec_summary={"template_content_hash": template_hash},
        render_spec={
            "format": "docx",
            "expected_bindings": {
                "cover": ["cover-region"],
                "details": ["details-region"],
            },
        },
        semantic_units=[
            {
                "unit_id": "cover",
                "kind": "section",
                "required": True,
                "output_schema": {"type": "section"},
                "source_capabilities": ["kb.read@1"],
                "reviewer_id": "deterministic@1",
            },
            {
                "unit_id": "details",
                "kind": "section",
                "required": True,
                "output_schema": {"type": "section"},
                "source_capabilities": ["kb.read@1"],
                "reviewer_id": "deterministic@1",
            },
        ],
        coverage_contract={
            "requirements": [
                {"requirement_id": "cover", "unit_id": "cover", "kind": "section", "required": True},
                {"requirement_id": "details", "unit_id": "details", "kind": "section", "required": True},
            ],
        },
        unit_tasks=[
            {
                "task_id": "task-cover",
                "unit_id": "cover",
                "plan_version": 1,
                "dependencies": [],
                "input_schema": {"type": "section"},
                "output_schema": {"type": "section"},
                "allowed_sources": ["kb.read@1"],
                "allowed_retrievers": ["default@1"],
                "allowed_tools": ["retrieve"],
                "max_attempts": 2,
                "timeout_seconds": 60,
                "action_key": "document-plan:docx-parent:cover",
            },
            {
                "task_id": "task-details",
                "unit_id": "details",
                "plan_version": 1,
                "dependencies": [],
                "input_schema": {"type": "section"},
                "output_schema": {"type": "section"},
                "allowed_sources": ["kb.read@1"],
                "allowed_retrievers": ["default@1"],
                "allowed_tools": ["retrieve"],
                "max_attempts": 2,
                "timeout_seconds": 60,
                "action_key": "document-plan:docx-parent:details",
            },
        ],
        retrieval_specs=[{"retriever_id": "default@1", "scope": snapshot.source_set_snapshot_id}],
    )
    return spec, DocumentPlan.model_validate(plan_payload)


def test_text_replacement_revision_changes_docx_child_and_preserves_other_units(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_EXECUTION_ENABLED", True)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", {"tenant-a"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES", {"icd"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS", {"docx"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_AUTO_PUBLISH_VERIFIED", True)

    ctx = RequestContext(
        user_id="author",
        tenant_id="tenant-a",
        kb_permissions={"hw:ADAS": "write"},
        metadata={"resource_department_id": "hw", "kb_id": "kb-a"},
    )
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts")
    )
    service = DocumentGenerationService(store=store)
    service.register_renderer_policy(RendererPolicy(
        renderer_policy_id="docx-renderer-001", version="1",
        allowed_changed_parts=["word/document.xml"],
    ))
    service.register_harness_policy(HarnessPolicy(
        harness_policy_id="docx-harness-001", version="1", status="approved",
        writer_provider_id="deterministic_evidence_writer", max_parallel_units=2,
        max_retrieval_rounds=8, max_retrieval_attempts_per_unit=1,
    ))
    service.register_document_schema(DocumentSchema(
        document_schema_id="docx-schema-001", version="1", document_type="icd",
        status="approved", execution_mode="internal_harness",
    ))
    template_content = _docx_template()
    template = service.register_template(
        TemplateVersion(
            template_version_id="docx-template-001", template_id="icd-docx",
            format="docx", content_hash=hashlib.sha256(template_content).hexdigest(),
            template_schema_id="docx-schema-001", template_schema_version="1",
            renderer_policy_id="docx-renderer-001",
        ),
        template_content,
        regions=[
            DocxRegionSchema(
                region_id="cover-region", locator={"paragraph_index": 0},
                role="semantic_draft", write_policy="deterministic_only",
            ),
            DocxRegionSchema(
                region_id="details-region", locator={"paragraph_index": 1},
                role="semantic_draft", write_policy="deterministic_only",
            ),
        ],
        bindings=[
            TemplateUnitBinding(
                binding_id="docx-cover-binding", template_schema_id="docx-schema-001",
                template_schema_version="1", semantic_unit_type="section",
                semantic_unit_id="cover", target_region_ids=["cover-region"],
            ),
            TemplateUnitBinding(
                binding_id="docx-details-binding", template_schema_id="docx-schema-001",
                template_schema_version="1", semantic_unit_type="section",
                semantic_unit_id="details", target_region_ids=["details-region"],
            ),
        ],
    )
    service.approve_template(template.template_version_id, actor_id="template-admin")

    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="tenant-a", knowledge_base_name="ADAS", source_names=["design.pdf"],
        created_by="author",
    )
    store.create_knowledge_base_source_snapshot(snapshot)
    spec, plan = _docx_revision_plan(snapshot, template.content_hash)
    store.planning.create_output_spec(spec, tenant_id="tenant-a", user_id="author")
    store.planning.create_plan(plan, tenant_id="tenant-a", user_id="author")
    order = service._create_frozen_work_order(
        ctx,
        scope_type="knowledge_base", snapshot=snapshot, knowledge_base_name="ADAS",
        template_version_id=template.template_version_id,
        document_schema_id="docx-schema-001", document_schema_version="1",
        harness_policy_id="docx-harness-001", execution_mode="internal_harness",
        output_spec_id=spec.output_spec_id, output_spec_version=spec.version,
        output_spec_hash=spec.content_hash, document_plan_id=plan.document_plan_id,
        document_plan_version=plan.version, document_plan_hash=plan.plan_hash,
    )

    def retrieve(requirement, attempt, query_override=None):
        del attempt, query_override
        is_cover = requirement.semantic_unit_id == "field:cover"
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits",
            evidences=[EvidenceEnvelope(
                id="ev-cover" if is_cover else "ev-details",
                content="Cover title: ADAS ICD" if is_cover else "Owner: Hardware Team",
                source_name="design.pdf",
                metadata={"knowledge_base_name": "ADAS"},
            )],
            query_fingerprint="docx-query",
            applied_source_set_snapshot_id=snapshot.source_set_snapshot_id,
        )

    parent = service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)
    parent_bytes = store.read_artifact_content(parent.artifact_id)
    assert "ADAS ICD" in _docx_text(parent_bytes)
    assert "Hardware Team" in _docx_text(parent_bytes)

    revision = service.revision_service.create_revision(
        ctx,
        task_id=order.task_id,
        parent_artifact_id=parent.artifact_id,
        request_type="section_update",
        request="将“ADAS ICD”替换为“ADAS Interface Control Document”",
        changed_sections=["cover"],
        client_request_id="docx-cover-revision",
    )
    child_order = service.restart_work_order_for_revision(
        ctx, order.work_order_id, revision_id=revision.revision_id,
    )
    retrieved: list[str] = []

    def child_retrieve(requirement, attempt, query_override=None):
        retrieved.append(requirement.semantic_unit_id)
        return retrieve(requirement, attempt, query_override)

    child = service.run_internal_harness(
        ctx, child_order.work_order_id, retrieve=child_retrieve,
    )
    child_bytes = store.read_artifact_content(child.artifact_id)

    assert retrieved == ["field:cover"]
    assert store.read_artifact_content(parent.artifact_id) == parent_bytes
    assert child_bytes != parent_bytes
    assert "ADAS ICD" in _docx_text(parent_bytes)
    assert "ADAS Interface Control Document" in _docx_text(child_bytes)
    assert "Hardware Team" in _docx_text(child_bytes)
    assert child.stage == "review_candidate"
    assert child.parent_artifact_id == parent.artifact_id
    assert child.revision_id == revision.revision_id
    child_run = store.get_harness_run(child.run_id)
    assert child_run is not None
    child_manifest = store.get_run_manifest(child_run.run_manifest_id)
    assert child_manifest is not None
    assert child_manifest.release_status != "released"
    completed = service.revision_service.store.get(revision.revision_id)
    assert completed is not None
    assert completed.status == "revalidated"
    assert completed.revalidation_status == "passed"
    assert completed.revalidation_result["preservation"]["verified"] == ["details"]
    assert completed.revalidation_result["target_change"]["changed"] == ["cover"]


def test_text_replacement_revision_changes_xlsx_child_and_preserves_other_units(
    tmp_path, monkeypatch,
) -> None:
    """A compiled scalar edit produces a new candidate XLSX, never a release."""

    service, ctx, store, parent_order, _plan, retrieve = _make_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(src.settings, "DOCUMENT_AUTO_PUBLISH_VERIFIED", True)

    parent = service.run_internal_harness(
        ctx, parent_order.work_order_id, retrieve=retrieve,
    )
    parent_bytes = store.read_artifact_content(parent.artifact_id)
    assert "ADAS ICD" in _worksheet_text(parent_bytes)
    assert "CANH" in _worksheet_text(parent_bytes)

    revision = service.revision_service.create_revision(
        ctx,
        task_id=parent_order.task_id,
        parent_artifact_id=parent.artifact_id,
        request_type="section_update",
        request="将“ADAS ICD”替换为“ADAS Interface Control Document”",
        changed_sections=["cover"],
        client_request_id="replace-cover-title",
    )
    child_order = service.restart_work_order_for_revision(
        ctx, parent_order.work_order_id, revision_id=revision.revision_id,
    )
    child = service.run_internal_harness(
        ctx, child_order.work_order_id, retrieve=retrieve,
    )
    child_bytes = store.read_artifact_content(child.artifact_id)

    assert store.read_artifact_content(parent.artifact_id) == parent_bytes
    assert child.artifact_id != parent.artifact_id
    assert child.content_hash != parent.content_hash
    assert child_bytes != parent_bytes
    assert child.stage == "review_candidate"
    assert child.parent_artifact_id == parent.artifact_id
    assert child.revision_id == revision.revision_id

    parent_text = _worksheet_text(parent_bytes)
    child_text = _worksheet_text(child_bytes)
    assert "ADAS ICD" in parent_text
    assert "ADAS Interface Control Document" in child_text
    assert "CANH" in child_text
    assert "CANL" in child_text

    completed = service.revision_service.store.get(revision.revision_id)
    assert completed is not None
    assert completed.status == "revalidated"
    assert completed.revalidation_status == "passed"
    assert completed.child_artifact_id == child.artifact_id
    assert completed.revalidation_result["target_change"]["changed"] == ["cover"]
    assert completed.revalidation_result["preservation"]["verified"] == ["pins"]
