from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.agents.claim_evidence import InformationRequirement
from src.agents.state import Evidence
from src.core.app_pipeline import AppPipeline
from src.pipelines.document_rag.schemas import RequestContext
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
from src.projects.retrieval import ProjectEvidenceRetrievalService
from src.projects.service import ProjectService
from src.projects.store import ProjectStore


def _prepare_xlsx_project(tmp_path: Path):
    """Build a project whose frozen source is an .xlsx document titled bom.xlsx."""
    project_store = ProjectStore(str(tmp_path / "projects.db"))
    service = ProjectService(project_store)
    ctx = RequestContext(
        user_id="alice",
        tenant_id="tenant-a",
        roles=["user"],
        kb_permissions={"hw:hardware": "read"},
        metadata={"department_id": "hw"},
    )
    project = Project(
        project_id="project-a", tenant_id="tenant-a", department_id="hw", name="ADAS",
    )
    service.create_project(ctx, project)
    project_store.add_principal_binding(ProjectPrincipalBinding(
        binding_id="member-a", tenant_id="tenant-a", project_id=project.project_id,
        principal_type="user", principal_id="alice", project_role="project_admin",
    ))
    asset = project_store.create_source_asset(SourceAsset(
        asset_id="asset-bom", tenant_id="tenant-a", original_file_name="bom.xlsx",
        content_hash="bom-content", content_kind="spreadsheet_table", parser_kind="xlsx",
        processing_status="ready",
    ))
    document = project_store.create_logical_document(LogicalDocument(
        document_id="document-bom", tenant_id="tenant-a", title="bom.xlsx",
        document_role="bom", owner_department_id="hw",
        metadata={"kb_name": "hardware"},
    ))
    version = project_store.create_source_version(SourceVersion(
        version_id="version-bom", tenant_id="tenant-a", document_id=document.document_id,
        asset_id=asset.asset_id, revision="A", approval_status="released",
    ))
    processing = project_store.create_processing_artifact(ProcessingArtifact(
        artifact_id="processing-bom", tenant_id="tenant-a", asset_id=asset.asset_id,
        processor_kind="spreadsheet_table", processor_version="1",
        content_fingerprint="parse-bom", status="ready",
    ))
    project_store.add_project_source_binding(ProjectSourceBinding(
        binding_id="binding-bom", tenant_id="tenant-a", project_id=project.project_id,
        version_id=version.version_id, usage_type="project_fact",
    ))
    project_store.add_region_policy(SourceRegionPolicy(
        region_policy_id="policy-bom", source_version_id=version.version_id,
        processing_artifact_id=processing.artifact_id, locator={"sheet": "Sheet1"},
        region_type="project_fact", allowed_evidence_uses=["review"], decision="allow",
        approved_by="reviewer",
    ))
    baseline = project_store.create_baseline(ProjectBaseline(
        baseline_id="baseline-bom", tenant_id="tenant-a", project_id=project.project_id,
        name="Release A", status="approved", items=[BaselineItem(
            baseline_item_id="item-bom", config_item_key="bom",
            source_role="bom", source_version_id=version.version_id,
        )],
    ))
    snapshot = service.create_source_set_snapshot(
        ctx, work_order_id="work-bom", project_id=project.project_id,
        baseline_id=baseline.baseline_id,
    )
    return service, ctx, project, version, processing, snapshot


def _build_pipeline(service, backend=None) -> AppPipeline:
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = backend or Mock()
    pipeline.projects = service
    pipeline.project_retrieval = ProjectEvidenceRetrievalService(service)
    pipeline.spreadsheet_service = None
    pipeline.document_generation = Mock()
    return pipeline


def _patch_spreadsheet_tool(pipeline, spreadsheet_tool):
    import src.core.app_pipeline as app_pipeline_mod

    original = app_pipeline_mod.SpreadsheetSemanticTool
    app_pipeline_mod.SpreadsheetSemanticTool = Mock(return_value=spreadsheet_tool)
    return original


def _restore_spreadsheet_tool(original):
    import src.core.app_pipeline as app_pipeline_mod

    app_pipeline_mod.SpreadsheetSemanticTool = original


def _xlsx_evidence(content: str = "BOM row", source_name: str = "bom.xlsx", score: float = 0.9) -> Evidence:
    return Evidence(
        id=f"xlsx:{source_name}:Sheet1:0:semantic",
        content=content,
        source_name=source_name,
        content_kind="spreadsheet_table",
        processor_kind="spreadsheet_table",
        score=score,
        locator={"record_id": 1, "sheet_name": "Sheet1", "row_index": 0},
        metadata={"tool": "spreadsheet_semantic"},
    )


def _req(subject: str, caps=None, roles=None, unit_id="field:f1") -> InformationRequirement:
    return InformationRequirement(
        requirement_id="req-1",
        semantic_unit_id=unit_id,
        claim_type="attribute",
        subject=subject,
        required_capabilities=caps or [],
        preferred_source_roles=roles or [],
    )


def test_project_retriever_adds_spreadsheet_for_tabular_lookup(tmp_path: Path):
    service, ctx, project, version, processing, snapshot = _prepare_xlsx_project(tmp_path)
    backend = Mock()
    backend.retrieve.return_value = []  # RAGFlow empty
    pipeline = _build_pipeline(service, backend=backend)
    pipeline.spreadsheet_service = Mock()
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [_xlsx_evidence("用量 row", "bom.xlsx")]
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._project_retriever(ctx, project.project_id, snapshot.source_set_snapshot_id)
        outcome = retrieve(_req("用量", ["tabular_lookup"]), 0)
    finally:
        _restore_spreadsheet_tool(original)

    spreadsheet_tool.run.assert_called_once()
    assert outcome.status == "success_with_hits"
    assert len(outcome.evidences) == 1
    ev = outcome.evidences[0]
    # Spreadsheet evidence is bound to the frozen version + artifact so it
    # passes the per-version scope validation in ProjectEvidenceRetrievalService.
    assert ev.source_version_id == version.version_id
    assert ev.processing_artifact_id == processing.artifact_id
    assert ev.source_name == "bom.xlsx"
    assert ev.document_role == "bom"
    # No filter_unsupported: scope validation passed.
    assert all(so.status != "filter_unsupported" for so in outcome.source_outcomes)


def test_project_retriever_skips_spreadsheet_without_tabular_lookup(tmp_path: Path):
    service, ctx, project, version, processing, snapshot = _prepare_xlsx_project(tmp_path)
    backend = Mock()
    backend.retrieve.return_value = []
    pipeline = _build_pipeline(service, backend=backend)
    pipeline.spreadsheet_service = Mock()
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [_xlsx_evidence("用量 row", "bom.xlsx")]
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._project_retriever(ctx, project.project_id, snapshot.source_set_snapshot_id)
        retrieve(_req("描述", ["entity_lookup"]), 0)
    finally:
        _restore_spreadsheet_tool(original)

    spreadsheet_tool.run.assert_not_called()


def test_project_retriever_skips_spreadsheet_when_service_missing(tmp_path: Path):
    service, ctx, project, version, processing, snapshot = _prepare_xlsx_project(tmp_path)
    backend = Mock()
    backend.retrieve.return_value = []
    pipeline = _build_pipeline(service, backend=backend)
    pipeline.spreadsheet_service = None
    import src.core.app_pipeline as app_pipeline_mod

    spy = Mock()
    app_pipeline_mod.SpreadsheetSemanticTool = spy
    try:
        retrieve = pipeline._project_retriever(ctx, project.project_id, snapshot.source_set_snapshot_id)
        outcome = retrieve(_req("用量", ["tabular_lookup"]), 0)
    finally:
        from src.agents.tools.spreadsheet_tools import SpreadsheetSemanticTool as RealTool

        app_pipeline_mod.SpreadsheetSemanticTool = RealTool

    spy.assert_not_called()
    assert outcome.status == "success_empty"


def test_project_retriever_role_boost_for_preferred_source_roles(tmp_path: Path):
    service, ctx, project, version, processing, snapshot = _prepare_xlsx_project(tmp_path)
    backend = Mock()
    # RAGFlow returns one envelope; its role matches the preferred role.
    from src.pipelines.document_rag.schemas import EvidenceEnvelope

    backend.retrieve.return_value = [EvidenceEnvelope(
        id="rag:1", content="用量 row", source_name="bom.xlsx",
        score=0.4, document_role="bom",
    )]
    pipeline = _build_pipeline(service, backend=backend)
    pipeline.spreadsheet_service = None
    retrieve = pipeline._project_retriever(ctx, project.project_id, snapshot.source_set_snapshot_id)
    outcome = retrieve(_req("用量", ["document_claim_lookup"], roles=["bom"]), 0)

    assert outcome.status == "success_with_hits"
    assert outcome.evidences[0].score == pytest.approx(0.6, abs=1e-9)
    assert outcome.evidences[0].metadata.get("preferred_source_role_match") == "bom"


def test_project_retriever_dedups_ragflow_and_spreadsheet(tmp_path: Path):
    service, ctx, project, version, processing, snapshot = _prepare_xlsx_project(tmp_path)
    from src.pipelines.document_rag.schemas import EvidenceEnvelope

    backend = Mock()
    # Same content from both backends; dedup keeps the higher score.
    backend.retrieve.return_value = [EvidenceEnvelope(
        id="rag:1", content="用量 row", source_name="bom.xlsx", score=0.3,
    )]
    pipeline = _build_pipeline(service, backend=backend)
    pipeline.spreadsheet_service = Mock()
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [_xlsx_evidence("用量 row", "bom.xlsx", score=0.9)]
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._project_retriever(ctx, project.project_id, snapshot.source_set_snapshot_id)
        outcome = retrieve(_req("用量", ["tabular_lookup"]), 0)
    finally:
        _restore_spreadsheet_tool(original)

    contents = [e.content for e in outcome.evidences]
    assert contents == ["用量 row"]  # deduped to one
    assert outcome.evidences[0].score == pytest.approx(0.9, abs=1e-9)


def test_project_retriever_cross_unit_reuse_on_empty(tmp_path: Path):
    service, ctx, project, version, processing, snapshot = _prepare_xlsx_project(tmp_path)
    backend = Mock()
    backend.retrieve.return_value = []  # RAGFlow empty for both units
    pipeline = _build_pipeline(service, backend=backend)
    pipeline.spreadsheet_service = Mock()
    spreadsheet_tool = Mock()
    # Unit A hits via spreadsheet; unit B empty.
    spreadsheet_tool.run.side_effect = [
        [_xlsx_evidence("用量 row", "bom.xlsx")],
        [],
    ]
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._project_retriever(ctx, project.project_id, snapshot.source_set_snapshot_id)
        outcome_a = retrieve(_req("用量", ["tabular_lookup"], unit_id="field:a"), 0)
        outcome_b = retrieve(_req("用量", ["tabular_lookup"], unit_id="field:b"), 0)
    finally:
        _restore_spreadsheet_tool(original)

    assert outcome_a.status == "success_with_hits"
    # Unit B had no fresh hits; the cache re-offers unit A's evidence.
    assert outcome_b.status == "success_with_hits"
    assert len(outcome_b.evidences) == 1
    assert outcome_b.evidences[0].metadata.get("reused") is True
    assert outcome_b.evidences[0].metadata.get("reused_from_unit") == "field:a"
