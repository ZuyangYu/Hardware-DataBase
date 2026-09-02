"""Task 3: confirmed GenerationBrief passthrough to WriterRequest."""

from __future__ import annotations

import hashlib

from src.agents.claim_evidence import InformationRequirement
from src.document_authoring.harness.graph import (
    build_writer_request,
    build_writer_system_prompt,
)
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessRun,
)
from src.document_authoring.writers.provider import WriterRequest


def _work_order(generation_brief: dict) -> DocumentWorkOrder:
    return DocumentWorkOrder.model_validate({
        "work_order_id": "wo-1", "tenant_id": "t1", "scope_type": "project",
        "project_id": "p-1", "baseline_id": "b-1", "baseline_content_hash": "bh",
        "source_set_snapshot_id": "snap-1", "template_version_id": "tv-1",
        "document_schema_id": "ds-1", "document_schema_version": "1",
        "template_schema_id": "ts-1", "template_schema_version": "1",
        "retrieval_policy_version": "1", "renderer_policy_version": "1",
        "target_format": "xlsx", "execution_mode": "internal_harness",
        "created_by": "admin", "harness_policy_id": "hp-1", "harness_policy_version": "1",
        "generation_brief": generation_brief,
    })


def _schema(allow_derivation: bool = False, missing_policy: str = "mark_tbd") -> DocumentSchema:
    return DocumentSchema.model_validate({
        "document_schema_id": "ds-1", "version": "1", "document_type": "spec",
        "status": "approved", "execution_mode": "internal_harness",
        "fields": [DocumentFieldSchema.model_validate({
            "field_id": "rated_current", "label": "额定电流",
            "retrieval_policy_id": "r-1", "verification_policy_id": "v-1",
            "allow_derivation": allow_derivation, "missing_policy": missing_policy,
        })],
    })


def _requirement() -> InformationRequirement:
    return InformationRequirement.model_validate({
        "requirement_id": "req-1", "semantic_unit_id": "field:rated_current",
        "claim_type": "requirement", "subject": "额定电流", "predicate": "review",
        "retrieval_query_terms": ["额定电流"],
    })


def _run() -> HarnessRun:
    return HarnessRun(harness_run_id="run-1", work_order_id="wo-1", run_manifest_id="rm-1")


def _build(order, schema) -> WriterRequest:
    return build_writer_request(
        work_order=order, harness_run=_run(), unit_id="field:rated_current",
        schema=schema, requirement=_requirement(), evidence=[{"id": "e1", "content": "x"}],
        prompt_version="1",
    )


def test_confirmed_brief_policies_reach_writer_request():
    order = _work_order({
        "confirmed": True, "missing_data_policy": "mark_tbd", "inference_policy": "forbid",
    })
    request = _build(order, _schema())
    kinds = {item["kind"]: item["policy"] for item in request.missing_or_conflicts}
    assert kinds["missing_data_policy"] == "mark_tbd"
    assert kinds["effective_missing_policy"] == "mark_tbd"
    assert request.allowed_derivations == []


def test_forbidden_inference_yields_no_derivations_even_when_field_allows():
    order = _work_order({
        "confirmed": True, "missing_data_policy": "mark_tbd", "inference_policy": "forbid",
    })
    request = _build(order, _schema(allow_derivation=True))
    assert request.allowed_derivations == []


def test_allowed_inference_requires_schema_intersection():
    order = _work_order({
        "confirmed": True, "missing_data_policy": "mark_tbd", "inference_policy": "allow_labeled",
    })
    assert _build(order, _schema()).allowed_derivations == []
    allowed = _build(order, _schema(allow_derivation=True)).allowed_derivations
    assert allowed == [{"kind": "labeled_inference", "policy": "allow_labeled"}]


def test_brief_is_global_ceiling_over_field_policy():
    order = _work_order({
        "confirmed": True, "missing_data_policy": "mark_tbd", "inference_policy": "forbid",
    })
    request = _build(order, _schema(missing_policy="optional"))
    kinds = {item["kind"]: item["policy"] for item in request.missing_or_conflicts}
    assert kinds["effective_missing_policy"] == "mark_tbd"


def test_legacy_orders_without_brief_keep_old_behaviour():
    order = _work_order({})
    request = _build(order, _schema())
    assert request.missing_or_conflicts == []
    assert request.allowed_derivations == []
    assert build_writer_system_prompt(request) == ""


def test_system_prompt_uses_fixed_boundaries_and_canonical_values_only():
    order = _work_order({
        "confirmed": True, "missing_data_policy": "mark_tbd", "inference_policy": "forbid",
    })
    request = _build(order, _schema())
    prompt = build_writer_system_prompt(request)
    assert prompt.startswith("<<USER_CONSTRAINTS>>\n")
    assert prompt.endswith("\n<<END_USER_CONSTRAINTS>>")
    assert "mark_tbd" in prompt
    assert "额定电流" not in prompt
    assert "标记未提供" not in prompt


# ── coordinator-level block_generation gate ──────────────────────────────────


def _block_pipeline(tmp_path, monkeypatch, *, block_brief: bool):
    from tests.test_document_authoring_p2a import _prepare_project
    from src.document_authoring.service import DocumentGenerationService
    from src.document_authoring.work_order_store import DocumentAuthoringStore
    from src.agents.claim_evidence import RetrievalOutcome
    from src.pipelines.document_rag.schemas import EvidenceEnvelope

    monkeypatch.setattr("src.settings.DOCUMENT_AUTO_PUBLISH_VERIFIED", True)
    project_service, ctx, project, baseline, _ = _prepare_project(tmp_path)
    store = DocumentAuthoringStore(str(tmp_path / "a.db"), str(tmp_path / "files"))
    service = DocumentGenerationService(project_service, store)
    template_content = _xlsx_template_bytes()
    from src.document_authoring.models import (
        HarnessPolicy, RendererPolicy, TemplateVersion, TemplateUnitBinding, WorkbookRegionSchema,
    )
    service.register_renderer_policy(RendererPolicy(renderer_policy_id="policy-a", version="1"))
    service.register_harness_policy(HarnessPolicy(
        harness_policy_id="deterministic-writer", version="1", status="approved",
        writer_provider_id="deterministic_evidence_writer", max_steps=40, max_retrieval_rounds=6,
    ))
    schema = service.register_document_schema(DocumentSchema.model_validate({
        "document_schema_id": "ds-1", "version": "1", "document_type": "spec",
        "status": "approved", "execution_mode": "internal_harness",
        "fields": [DocumentFieldSchema.model_validate({
            "field_id": "rated_current", "label": "额定电流",
            "retrieval_policy_id": "r-1", "verification_policy_id": "v-1",
        })],
    }))
    template = service.register_template(
        TemplateVersion(
            template_version_id="tv-1", template_id="review", format="xlsx",
            content_hash=hashlib.sha256(template_content).hexdigest(),
            template_schema_id="ts-1", template_schema_version="1",
            renderer_policy_id="policy-a",
        ),
        template_content,
        regions=[WorkbookRegionSchema(
            region_id="region-rated", sheet_name="Review", locator={"cell": "B2"},
            role="semantic_draft", write_policy="validated_draft", allow_nonempty_overwrite=True,
        )],
        bindings=[TemplateUnitBinding(
            binding_id="bind-rated", template_schema_id="ts-1", template_schema_version="1",
            semantic_unit_type="field", semantic_unit_id="rated_current",
            target_region_ids=["region-rated"],
        )],
    )
    service.approve_template(template.template_version_id, "admin")
    brief = {
        "scope": {"revision": "当前发布版本"},
        "missing_data_policy": "block_generation" if block_brief else "mark_tbd",
        "inference_policy": "forbid",
        "confirmed": True,
    }
    order = service.create_document_work_order(
        ctx, project_id=project.project_id, baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=schema.document_schema_id, document_schema_version=schema.version,
        harness_policy_id="deterministic-writer", generation_brief=brief,
    )

    def retrieve(requirement, attempt, query_override=None):
        has_data = not block_brief
        evidences = [EvidenceEnvelope(
            id="ev-1", content="额定电流为10 A。", project_id=project.project_id,
            source_version_id="version-a", processing_artifact_id="processing-a",
        )] if has_data else []
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits" if has_data else "success_empty",
            evidences=evidences, query_fingerprint="q",
            applied_source_set_snapshot_id=order.source_set_snapshot_id,
            applied_region_policy_versions={"policy-a": "1"},
        )

    artifact = service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)
    return service, store, order, artifact


def _xlsx_template_bytes() -> bytes:
    import io
    import zipfile
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
 <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Field</t></is></c><c r="B1" t="inlineStr"><is><t>Value</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>rated_current</t></is></c><c r="B2"/></row></sheetData>
</worksheet>''',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_block_generation_blocks_auto_publish_on_unresolved_required_missing(tmp_path, monkeypatch):
    service, store, order, artifact = _block_pipeline(tmp_path, monkeypatch, block_brief=True)
    persisted = store.get_work_order(order.work_order_id)
    assert persisted.status == "blocked"
    report = store.get_validation_report(artifact.validation_report_id)
    codes = {issue.get("code") for issue in report.issues}
    assert "block_generation_unresolved_missing" in codes
    assert artifact.stage == "review_candidate"
    stages = [a.stage for a in store.list_artifacts(order.work_order_id)]
    assert "approved_release" not in stages


def test_block_generation_does_not_block_data_complete_runs(tmp_path, monkeypatch):
    service, store, order, artifact = _block_pipeline(tmp_path, monkeypatch, block_brief=False)
    persisted = store.get_work_order(order.work_order_id)
    assert persisted.status != "blocked"
    report = store.get_validation_report(artifact.validation_report_id)
    codes = {issue.get("code") for issue in report.issues}
    assert "block_generation_unresolved_missing" not in codes
