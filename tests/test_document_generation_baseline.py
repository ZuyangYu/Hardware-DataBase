"""Task 1: repeatable Phase A baseline on the frozen fixture dataset.

Runs the rated_current xlsx fixture through the real fixed pipeline
(register -> work order -> internal harness with the deterministic writer)
three times and asserts byte-identical artifacts. The full 21-record baseline
report is produced by this same path during the Phase A gate; this test guards
reproducibility in CI.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from src.agents.claim_evidence import RetrievalOutcome
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
from src.evaluation.dataset_loader import load_document_generation_dataset
from src.pipelines.document_rag.schemas import EvidenceEnvelope
from tests.document_gating_env import pin_deterministic_document_gating  # noqa: F401
from tests.test_document_authoring_p2a import _prepare_project

FIXTURE = Path("evaluation/fixtures/document_generation/rated_current_review.xlsx")
TEMPLATE_VERSION_ID = "fixture-rated-current-template"
SCHEMA_ID = "fixture-rated-current-schema"


def _fixture_records():
    records = load_document_generation_dataset(
        "evaluation/datasets/document_generation_v1.jsonl"
    )
    selected = [r for r in records if r.template_fixture == FIXTURE.name]
    assert selected, "baseline fixture records missing from frozen dataset"
    return selected


def _rendered_text(artifact_content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(artifact_content)) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    return "".join(root.itertext())


def _run_pipeline(tmp_path: Path, records) -> tuple[str, str]:
    project_service, ctx, project, baseline, _ = _prepare_project(tmp_path)
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "files"))
    service = DocumentGenerationService(project_service, store)
    content = FIXTURE.read_bytes()
    service.register_renderer_policy(RendererPolicy(renderer_policy_id="policy-render", version="1"))
    service.register_harness_policy(HarnessPolicy(
        harness_policy_id="deterministic-writer", version="1", status="approved",
        writer_provider_id="deterministic_evidence_writer",
        max_steps=60, max_retrieval_rounds=12,
    ))
    schema = service.register_document_schema(DocumentSchema(
        document_schema_id=SCHEMA_ID, version="1",
        document_type="hardware_design_spec", status="approved", execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id=record.field_id,
            label={"rated_current": "额定电流", "insulation_class": "绝缘等级", "test_voltage": "耐压值"}[record.field_id],
            retrieval_policy_id=f"retrieval-{record.field_id}",
            verification_policy_id=f"verify-{record.field_id}",
            authoring_policy="managed_writer",
        ) for record in records],
    ))
    template = service.register_template(
        TemplateVersion(
            template_version_id=TEMPLATE_VERSION_ID, template_id="rated-current-review",
            format="xlsx", content_hash=hashlib.sha256(content).hexdigest(),
            template_schema_id=SCHEMA_ID, template_schema_version="1",
            renderer_policy_id="policy-render",
        ),
        content,
        regions=[WorkbookRegionSchema(
            region_id=f"region-{record.field_id}", sheet_name="Review",
            locator={"cell": f"B{index + 2}"}, role="semantic_draft",
            write_policy="validated_draft", allow_nonempty_overwrite=True,
        ) for index, record in enumerate(records)],
        bindings=[TemplateUnitBinding(
            binding_id=f"binding-{record.field_id}", template_schema_id=SCHEMA_ID,
            template_schema_version="1", semantic_unit_type="field",
            semantic_unit_id=record.field_id,
            target_region_ids=[f"region-{record.field_id}"],
        ) for record in records],
    )
    service.approve_template(template.template_version_id, "admin")
    order = service.create_document_work_order(
        ctx, project_id=project.project_id, baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=schema.document_schema_id, document_schema_version=schema.version,
        harness_policy_id="deterministic-writer",
    )
    labels_by_field = {"rated_current": "额定电流", "insulation_class": "绝缘等级", "test_voltage": "耐压值"}
    evidence_by_field = {
        records[0].field_id: f"{labels_by_field[records[0].field_id]}为10 A。",
        records[1].field_id: f"{labels_by_field[records[1].field_id]}为Class F。",
    }

    def retrieve(requirement, attempt, query_override=None):
        value = evidence_by_field.get(requirement.semantic_unit_id.split(":", 1)[-1])
        if value is None:
            return RetrievalOutcome(
                requirement_id=requirement.requirement_id, status="success_empty",
                evidences=[], query_fingerprint=f"q-{requirement.requirement_id}",
                applied_source_set_snapshot_id=order.source_set_snapshot_id,
                applied_region_policy_versions={"policy-a": "1"},
            )
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id, status="success_with_hits",
            evidences=[EvidenceEnvelope(
                id=f"ev-{requirement.requirement_id}", content=value,
                project_id=project.project_id, source_version_id="version-a",
                processing_artifact_id="processing-a",
            )],
            query_fingerprint=f"q-{requirement.requirement_id}",
            applied_source_set_snapshot_id=order.source_set_snapshot_id,
            applied_region_policy_versions={"policy-a": "1"},
        )

    artifact = service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)
    content_bytes = store.read_artifact_content(artifact.artifact_id)
    return hashlib.sha256(content_bytes).hexdigest(), _rendered_text(content_bytes)


def test_baseline_fixture_is_stable_across_three_pipeline_runs(tmp_path):
    digests = []
    for attempt in range(3):
        workdir = tmp_path / f"run-{attempt}"
        workdir.mkdir()
        digest, _ = _run_pipeline(workdir, _fixture_records())
        digests.append(digest)
    assert len(set(digests)) == 1, f"baseline runs diverged: {digests}"


def test_baseline_fixture_renders_expected_values(tmp_path):
    artifact_hash, rendered = _run_pipeline(tmp_path, _fixture_records())
    assert "10 A" in rendered
    assert "Class F" in rendered
