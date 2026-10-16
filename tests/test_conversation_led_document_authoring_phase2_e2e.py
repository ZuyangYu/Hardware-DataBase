from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from xml.etree import ElementTree as ET

import pytest

import src.settings
from src.agents.claim_evidence import RetrievalOutcome
from src.document_authoring.models import (
    DocumentSchema,
    HarnessPolicy,
    KnowledgeBaseSourceSnapshot,
    RendererPolicy,
    TemplateUnitBinding,
    TemplateVersion,
    WorkbookRegionSchema,
    WorkbookTableColumnSchema,
    WorkbookTableSchema,
)
from src.document_authoring.conflicts.models import ConflictDecision
from src.document_authoring.planning.models import DocumentPlan, OutputSpec
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import (
    DeterministicEvidenceWriter,
    ManagedWriter,
)
from src.pipelines.document_rag.schemas import EvidenceEnvelope, RequestContext
from src.projects.service import ProjectService
from src.projects.store import ProjectStore

from tests.document_gating_env import pin_deterministic_document_gating  # noqa: F401
from tests.test_document_authoring_p2a import _xlsx_template
from tests.test_document_planning_contracts import _output_payload, _plan_payload


def _fixture_plan(snapshot: KnowledgeBaseSourceSnapshot, template_hash: str) -> tuple[OutputSpec, DocumentPlan]:
    spec_payload = _output_payload()
    spec_payload["status"] = "accepted"
    spec = OutputSpec.model_validate(spec_payload)
    payload = _plan_payload(
        status="accepted",
        source_snapshot_id=snapshot.source_set_snapshot_id,
        source_snapshot_hash=snapshot.content_hash,
        output_spec_hash=spec.content_hash,
        layout_contract={
            "kind": "template",
            "template_version_id": "template-001",
            "template_schema_id": "schema-001",
            "template_schema_version": "1",
            "bindings": {"cover": ["cover-cell"], "pins": ["pins-table"]},
        },
        output_spec_summary={"template_content_hash": template_hash},
        render_spec={
            "format": "xlsx",
            "expected_sheets": ["Review"],
            "allowlisted_cells": [
                "Review!A1",
                "Review!A10", "Review!B10", "Review!C10",
                "Review!A11", "Review!B11", "Review!C11",
            ],
            "expected_bindings": {
                "cover": ["Review!A1"],
                "pins": [
                    "Review!A10", "Review!B10", "Review!C10",
                    "Review!A11", "Review!B11", "Review!C11",
                ],
            },
        },
    )
    return spec, DocumentPlan.model_validate(payload)


def _make_fixture(
    tmp_path, monkeypatch, *, missing_second_row: bool = False,
    with_dependency: bool = False,
):
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_EXECUTION_ENABLED", True)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", {"tenant-a"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES", {"icd"}, raising=False)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS", {"xlsx"}, raising=False)
    ctx = RequestContext(
        user_id="author",
        tenant_id="tenant-a",
        kb_permissions={"hw:ADAS": "write"},
        metadata={"resource_department_id": "hw", "kb_id": "kb-a"},
    )
    authoring_store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts")
    )
    service = DocumentGenerationService(
        ProjectService(ProjectStore(str(tmp_path / "projects.db"))),
        authoring_store,
    )
    content = _xlsx_template()
    template_hash = hashlib.sha256(content).hexdigest()
    service.register_renderer_policy(RendererPolicy(
        renderer_policy_id="renderer-001", version="1",
    ))
    service.register_harness_policy(HarnessPolicy(
        harness_policy_id="harness-001", version="1", status="approved",
        writer_provider_id="deterministic_evidence_writer", max_parallel_units=2,
        max_retrieval_rounds=8, max_retrieval_attempts_per_unit=1,
    ))
    service.register_document_schema(DocumentSchema(
        document_schema_id="schema-001", version="1", document_type="icd",
        status="approved", execution_mode="internal_harness",
    ))
    template = service.register_template(
        TemplateVersion(
            template_version_id="template-001", template_id="icd", format="xlsx",
            content_hash=template_hash, template_schema_id="schema-001",
            template_schema_version="1", renderer_policy_id="renderer-001",
        ),
        content,
        regions=[WorkbookRegionSchema(
            region_id="cover-cell", sheet_name="Review", locator={"cell": "A1"},
            role="evidence_derived", write_policy="deterministic_only",
            allow_nonempty_overwrite=True,
        )],
        bindings=[TemplateUnitBinding(
            binding_id="cover-binding", template_schema_id="schema-001",
            template_schema_version="1", semantic_unit_type="section",
            semantic_unit_id="cover", target_region_ids=["cover-cell"],
        ), TemplateUnitBinding(
            binding_id="pins-binding", template_schema_id="schema-001",
            template_schema_version="1", semantic_unit_type="field",
            semantic_unit_id="pins", target_region_ids=["pins-table"],
            table_schema=WorkbookTableSchema(
                table_region_id="pins-table", semantic_unit_id="pins",
                sheet_name="Review", header_row=9, first_data_row=10,
                last_template_row=11, style_source_row=10, max_output_rows=2,
                columns=[
                    WorkbookTableColumnSchema(column_id="connector", label="Connector", column_letter="A"),
                    WorkbookTableColumnSchema(column_id="pin", label="Pin", column_letter="B"),
                    WorkbookTableColumnSchema(column_id="signal", label="Signal", column_letter="C"),
                ],
                expected_row_keys=["J1:1", "J1:2"],
                required_columns=["connector", "pin", "signal"],
                row_order="declared",
            ),
        )],
    )
    service.approve_template(template.template_version_id, actor_id="template-admin")

    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="tenant-a", knowledge_base_name="ADAS", source_names=["design.pdf"],
        created_by="author",
    )
    authoring_store.create_knowledge_base_source_snapshot(snapshot)
    spec, plan = _fixture_plan(snapshot, template_hash)
    if with_dependency:
        plan_payload = plan.model_dump(mode="json")
        plan_payload["dependency_edges"] = [
            {"upstream_task_id": "task-cover", "downstream_task_id": "task-pins"},
        ]
        plan_payload["unit_tasks"][1]["dependencies"] = ["task-cover"]
        plan_payload["plan_hash"] = None
        plan = DocumentPlan.model_validate(plan_payload)
    authoring_store.planning.create_output_spec(
        spec, tenant_id="tenant-a", user_id="author",
    )
    authoring_store.planning.create_plan(
        plan, tenant_id="tenant-a", user_id="author",
    )
    order = service._create_frozen_work_order(
        ctx,
        scope_type="knowledge_base",
        snapshot=snapshot,
        knowledge_base_name="ADAS",
        template_version_id=template.template_version_id,
        document_schema_id="schema-001",
        document_schema_version="1",
        harness_policy_id="harness-001",
        execution_mode="internal_harness",
        output_spec_id=spec.output_spec_id,
        output_spec_version=spec.version,
        output_spec_hash=spec.content_hash,
        document_plan_id=plan.document_plan_id,
        document_plan_version=plan.version,
        document_plan_hash=plan.plan_hash,
    )

    def retrieve(requirement, attempt, query_override=None):
        del attempt, query_override
        if requirement.semantic_unit_id == "field:cover":
            evidences = [EvidenceEnvelope(
                id="ev-cover", content="Cover title: ADAS ICD", source_name="design.pdf",
                metadata={"knowledge_base_name": "ADAS"},
            )]
        else:
            rows = [
                ("J1:1", "1", "CANH"),
                ("J1:2", "2", "CANL"),
            ]
            if missing_second_row:
                rows = rows[:1]
            evidences = [EvidenceEnvelope(
                id=f"ev-{row_key.replace(':', '-')}",
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
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits",
            evidences=evidences,
            query_fingerprint="query",
            applied_source_set_snapshot_id=snapshot.source_set_snapshot_id,
        )

    return service, ctx, authoring_store, order, plan, retrieve


def test_accepted_plan_runs_full_document_pipeline_and_binds_all_hashes(tmp_path, monkeypatch):
    service, ctx, store, order, plan, retrieve = _make_fixture(tmp_path, monkeypatch)

    artifact = service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)

    assert artifact.stage == "review_candidate"
    run = store.get_harness_run(artifact.run_id)
    assert run is not None
    assert run.execution_route == "plan_dag"
    assert run.document_plan_hash == plan.plan_hash
    assert run.document_model_hash
    assert run.pre_render_review_hash
    assert run.post_render_review_hash
    assert run.release_decision_hash
    # The deterministic gates allow release, but automatic publication is
    # disabled in this smoke fixture, so the candidate waits at Gate 2.
    assert run.release_status == "pending"
    validation = store.get_validation_report(artifact.validation_report_id)
    assert validation is not None and validation.status == "passed"
    assert run.current_node == "complete"
    manifest = store.get_run_manifest(run.run_manifest_id)
    assert manifest is not None
    assert manifest.execution_route == "plan_dag"
    assert manifest.document_plan_hash == plan.plan_hash
    assert manifest.task_graph_hash == run.task_graph_hash
    assert manifest.document_model_hash == run.document_model_hash
    assert manifest.pre_render_review_hash == run.pre_render_review_hash
    assert manifest.post_render_review_hash == run.post_render_review_hash
    assert manifest.artifact_hash == artifact.content_hash
    assert manifest.release_decision_hash == run.release_decision_hash

    drafts = store.list_unit_drafts(artifact.run_id)
    assert {draft.unit_id for draft in drafts} == {"field:cover", "field:pins"}
    table = next(draft for draft in drafts if draft.unit_id == "field:pins")
    assert table.typed_value is not None
    assert [row.row_key for row in table.typed_value.rows] == ["J1:1", "J1:2"]
    assert len(store.list_evidence_entries(artifact.run_id)) == 3
    assert len(store.list_execution_events(artifact.run_id)) >= 5

    with zipfile.ZipFile(io.BytesIO(store.read_artifact_content(artifact.artifact_id))) as package:
        root = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))
    rendered = "".join(root.itertext())
    assert "ADAS ICD" in rendered
    assert "CANH" in rendered
    assert "CANL" in rendered


def test_required_missing_row_blocks_plan_release_before_auto_publish(tmp_path, monkeypatch):
    service, ctx, store, order, plan, retrieve = _make_fixture(
        tmp_path, monkeypatch, missing_second_row=True,
    )

    artifact = service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)

    assert artifact.stage == "review_candidate"
    persisted_order = store.get_work_order(order.work_order_id)
    assert persisted_order is not None
    assert persisted_order.status == "blocked"
    report = store.get_validation_report(artifact.validation_report_id)
    assert report is not None
    assert report.status == "requires_human"
    assert any(issue.get("code") == "plan_release_blocked" for issue in report.issues)
    run = store.get_harness_run(artifact.run_id)
    assert run is not None and run.release_status == "blocked"
    assert run.document_plan_hash == plan.plan_hash


def test_plan_run_restarts_from_receipts_without_duplicate_writer_or_business_facts(
    tmp_path, monkeypatch,
):
    service, ctx, store, order, plan, retrieve = _make_fixture(tmp_path, monkeypatch)
    writer = ManagedWriter(DeterministicEvidenceWriter())
    writer_calls: list[str] = []
    original_generate = writer.generate

    def counting_generate(request):
        writer_calls.append(request.unit_id)
        return original_generate(request)

    writer.generate = counting_generate
    failed_once = {"value": True}
    retrieval_calls: list[str] = []

    def flaky_retrieve(requirement, attempt, query_override=None):
        retrieval_calls.append(requirement.semantic_unit_id)
        if requirement.semantic_unit_id == "field:pins" and failed_once["value"]:
            failed_once["value"] = False
            raise RuntimeError("simulated worker restart")
        return retrieve(requirement, attempt, query_override)

    with pytest.raises(RuntimeError, match="simulated worker restart"):
        service.run_internal_harness(
            ctx, order.work_order_id, retrieve=flaky_retrieve, writer=writer,
        )

    runs = store.list_harness_runs(order.work_order_id)
    assert len(runs) == 1
    failed_run = runs[0]
    assert failed_run.status == "failed"
    committed_before = store.list_node_execution_receipts(
        failed_run.harness_run_id, node_name="plan_node", status="committed",
    )
    assert {receipt.unit_id for receipt in committed_before} == {
        "preflight", "unit:task-cover",
    }
    # A committed unit receipt must have a durable draft before a worker can
    # be replaced; otherwise a restart would have to call the writer again.
    assert {draft.unit_id for draft in store.list_unit_drafts(failed_run.harness_run_id)} == {
        "field:cover",
    }

    artifact = service.resume_internal_harness(
        ctx, failed_run.harness_run_id, retrieve=flaky_retrieve, writer=writer,
    )

    assert artifact.stage == "review_candidate"
    assert len(store.list_harness_runs(order.work_order_id)) == 1
    receipts = store.list_node_execution_receipts(
        failed_run.harness_run_id, node_name="plan_node", status="committed",
    )
    assert len(receipts) == 8
    assert len({receipt.unit_id for receipt in receipts}) == 8
    assert writer_calls == ["field:cover", "field:pins"]
    assert retrieval_calls.count("field:cover") == 1
    assert retrieval_calls.count("field:pins") == 2
    assert len(store.list_unit_drafts(failed_run.harness_run_id)) == 2
    assert len(store.list_artifacts(order.work_order_id)) == 1
    events = store.list_execution_events(failed_run.harness_run_id)
    assert sum(event.event_type == "run_started" for event in events) == 2


def test_plan_route_requires_the_accepted_output_spec_before_any_retrieval(
    tmp_path, monkeypatch,
):
    service, ctx, store, order, _plan, retrieve = _make_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM document_output_specs "
            "WHERE output_spec_id = ? AND version = ?",
            (order.output_spec_id, order.output_spec_version),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["status"] = "proposed"
        connection.execute(
            "UPDATE document_output_specs SET status = ?, payload_json = ? "
            "WHERE output_spec_id = ? AND version = ?",
            ("proposed", json.dumps(payload), order.output_spec_id, order.output_spec_version),
        )

    retrieval_calls: list[str] = []

    def recording_retrieve(requirement, attempt, query_override=None):
        retrieval_calls.append(requirement.semantic_unit_id)
        return retrieve(requirement, attempt, query_override)

    with pytest.raises(ValueError, match="accepted OutputSpec"):
        service.run_internal_harness(
            ctx, order.work_order_id, retrieve=recording_retrieve,
        )

    assert retrieval_calls == []
    assert store.list_harness_runs(order.work_order_id) == []


def test_plan_route_fails_closed_when_scope_is_not_allowlisted(tmp_path, monkeypatch):
    service, ctx, store, order, _plan, retrieve = _make_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", {"tenant-other"})

    with pytest.raises(ValueError, match="allowlist"):
        service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)

    assert store.list_harness_runs(order.work_order_id) == []


def test_conflict_decision_invalidates_affected_and_downstream_receipts_on_resume(
    tmp_path, monkeypatch,
):
    """A decision changes the review input, so a restarted worker recomputes its closure."""
    service, ctx, store, order, _plan, retrieve = _make_fixture(
        tmp_path, monkeypatch, with_dependency=True,
    )
    writer = ManagedWriter(DeterministicEvidenceWriter())
    writer_calls: list[str] = []
    original_generate = writer.generate

    def counting_generate(request):
        writer_calls.append(request.unit_id)
        return original_generate(request)

    writer.generate = counting_generate

    def conflicting_retrieve(requirement, attempt, query_override=None):
        if requirement.semantic_unit_id == "field:cover":
            return RetrievalOutcome(
                requirement_id=requirement.requirement_id,
                status="success_with_hits",
                evidences=[
                    EvidenceEnvelope(
                        id="ev-cover-5v", content="Cover title: ADAS ICD", source_name="design.pdf",
                        metadata={
                            "knowledge_base_name": "ADAS",
                            "knowledge_source": {"kb_id": 1, "role": "primary"},
                            "effective_scope": "project:ADAS",
                            "source_content_hash": "sha256:cover-a",
                            "source_version_id": "cover-a",
                            "normalized_claim": {
                                "claim_id": "cover-voltage-a", "field_key": "input_voltage",
                                "subject": "power module", "predicate": "requires",
                                "value": "5", "unit": "V", "scope_key": "project:ADAS",
                            },
                        },
                    ),
                    EvidenceEnvelope(
                        id="ev-cover-12v", content="Cover title: ADAS ICD", source_name="design.pdf",
                        metadata={
                            "knowledge_base_name": "ADAS",
                            "knowledge_source": {"kb_id": 2, "role": "reference"},
                            "effective_scope": "project:ADAS",
                            "source_content_hash": "sha256:cover-b",
                            "source_version_id": "cover-b",
                            "normalized_claim": {
                                "claim_id": "cover-voltage-b", "field_key": "input_voltage",
                                "subject": "power module", "predicate": "requires",
                                "value": "12", "unit": "V", "scope_key": "project:ADAS",
                            },
                        },
                    ),
                ],
                query_fingerprint="query-cover-conflict",
                applied_source_set_snapshot_id=order.source_set_snapshot_id,
            )
        return retrieve(requirement, attempt, query_override)

    # Discover the conflict during finalization so all plan-node receipts are
    # durable before the worker enters its human wait state.
    service.harness_runtime.conflict_reviewer = None
    first_artifact = service.run_internal_harness(
        ctx, order.work_order_id, retrieve=conflicting_retrieve, writer=writer,
    )
    run = store.get_harness_run(first_artifact.run_id)
    assert run is not None and run.status == "waiting_human"
    before = store.list_node_execution_receipts(
        run.harness_run_id, node_name="plan_node", status="committed",
    )
    assert {receipt.unit_id for receipt in before} == {
        "preflight", "unit:task-cover", "unit:task-pins",
        "aggregate", "pre-render-review", "render", "post-render-review", "release",
    }
    case = service.conflict_store.list_cases(
        tenant_id="tenant-a", work_order_id=order.work_order_id,
    )[0]
    decision = ConflictDecision(
        conflict_id=case.conflict_id,
        action="choose_claim",
        selected_claim_id=case.claims[0].claim_id,
        requires_human=True,
        blocking=False,
    )
    service.conflict_store.submit_decision(
        case.conflict_id,
        tenant_id="tenant-a",
        actor_id="author",
        expected_fingerprint=case.fingerprint,
        expected_snapshot_hash=case.snapshot_hash,
        expected_evidence_set_hash=case.evidence_set_hash,
        expected_version=case.row_version,
        decision=decision,
        idempotency_key="resume-after-review",
    )

    # A fresh worker gets the normal conflict reviewer and resumes the same
    # HarnessRun. The changed decision fingerprint invalidates cover and the
    # dependent pins unit; no stale writer receipt can bypass that review.
    service.harness_runtime.conflict_reviewer = service.prepare_conflict_writer_request
    service.resume_internal_harness(
        ctx, run.harness_run_id, retrieve=conflicting_retrieve, writer=writer,
    )

    assert writer_calls.count("field:cover") == 2
    assert writer_calls.count("field:pins") == 2
    after = store.list_node_execution_receipts(
        run.harness_run_id, node_name="plan_node", status="committed",
    )
    assert len(after) > len(before)
    assert len({receipt.input_fingerprint for receipt in after if receipt.unit_id == "unit:task-cover"}) == 2
    assert len({receipt.input_fingerprint for receipt in after if receipt.unit_id == "unit:task-pins"}) == 2
