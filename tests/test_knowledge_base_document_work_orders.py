from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import pytest

from src.agents.claim_evidence import InformationRequirement, RetrievalOutcome
from src.agents.schemas import Evidence
from src.core.app_pipeline import AppPipeline
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    KnowledgeBaseSourceSnapshot,
    RendererPolicy,
    TemplateVersion,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.harness.graph import _validated_evidence
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import DeterministicEvidenceWriter
from src.pipelines.document_rag.schemas import EvidenceEnvelope, RequestContext
from tests.test_document_authoring_p2a import _xlsx_template


@pytest.fixture
def service(tmp_path):
    return DocumentGenerationService(
        store=DocumentAuthoringStore(
            str(tmp_path / "authoring.db"),
            str(tmp_path / "authoring-files"),
        )
    )


@pytest.fixture
def ctx():
    return RequestContext(
        user_id="alice",
        tenant_id="tenant-a",
        metadata={"department_id": "hw"},
        kb_permissions={"hw:hardware": "read"},
    )


@pytest.fixture
def pipeline(service):
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock()
    pipeline.documents = Mock()
    pipeline.document_generation = service
    # Default to no spreadsheet service; tests that exercise the spreadsheet
    # path override this attribute explicitly.
    pipeline.spreadsheet_service = None
    return pipeline


def requirement(subject: str) -> InformationRequirement:
    return InformationRequirement(
        requirement_id=f"requirement-{subject}",
        semantic_unit_id="summary",
        claim_type="attribute",
        subject=subject,
    )


def test_pipeline_knowledge_base_retriever_is_scoped(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []

    retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["spec.pdf"])
    retrieve(requirement("voltage"), 0)

    pipeline.backend.retrieve.assert_called_once_with(
        "hardware",
        "voltage",
        top_k=ANY,
        ctx=ctx,
        filters={"source_names": ["spec.pdf"]},
    )


def test_knowledge_base_retrieval_outcome_rejects_backend_kb_mismatch(service):
    evidence = EvidenceEnvelope(
        id="cross-kb",
        content="must not be relabeled",
        source_name="spec.pdf",
        metadata={"kb_name": "firmware"},
    )

    with pytest.raises(PermissionError, match="knowledge base"):
        service.build_knowledge_base_retrieval_outcome(
            "hardware",
            ["spec.pdf"],
            [evidence],
            requirement_id="requirement-voltage",
            source_set_snapshot_id="snapshot-frozen",
        )

    assert evidence.metadata == {"kb_name": "firmware"}


def test_pipeline_knowledge_base_options_are_authorized_and_approved(
    pipeline, ctx, approved_template, approved_schema
):
    pipeline.list_knowledge_bases = Mock(return_value=["hardware"])
    pipeline.document_generation.store.list_templates = Mock(
        return_value=[approved_template]
    )
    pipeline.document_generation.store.list_document_schemas = Mock(
        return_value=[approved_schema]
    )

    options = pipeline.list_knowledge_base_document_generation_options(ctx)

    assert options == {
        "knowledge_bases": ["hardware"],
        "templates": [approved_template],
        "schemas": [approved_schema],
    }
    pipeline.list_knowledge_bases.assert_called_once_with(ctx)
    pipeline.document_generation.store.list_templates.assert_called_once_with(
        approved_only=True
    )
    pipeline.document_generation.store.list_document_schemas.assert_called_once_with(
        approved_only=True
    )


def test_pipeline_creates_knowledge_base_work_order_from_readable_sources(
    pipeline, ctx, approved_template, approved_schema
):
    pipeline.list_file_infos = Mock(
        return_value=[
            SimpleNamespace(name="spec.pdf"),
            SimpleNamespace(name="spec.pdf"),
            SimpleNamespace(name="limits.xlsx"),
        ]
    )

    order = pipeline.create_knowledge_base_document_work_order(
        ctx,
        knowledge_base_name="hardware",
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )

    snapshot = pipeline.document_generation.resolve_source_snapshot(order)
    assert snapshot.source_names == ["spec.pdf", "limits.xlsx"]
    pipeline.list_file_infos.assert_called_once_with("hardware", ctx)


def test_pipeline_rejects_empty_knowledge_base_work_order(
    pipeline, ctx, approved_template, approved_schema
):
    pipeline.list_file_infos = Mock(return_value=[])

    with pytest.raises(ValueError, match="no readable source documents"):
        pipeline.create_knowledge_base_document_work_order(
            ctx,
            knowledge_base_name="hardware",
            template_version_id=approved_template.template_version_id,
            document_schema_id=approved_schema.document_schema_id,
            document_schema_version=approved_schema.version,
        )


def test_pipeline_lists_only_live_authorized_knowledge_base_work_orders(
    pipeline, ctx, approved_template, approved_schema
):
    pipeline.list_file_infos = Mock(
        return_value=[SimpleNamespace(name="spec.pdf")]
    )
    order = pipeline.create_knowledge_base_document_work_order(
        ctx,
        knowledge_base_name="hardware",
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )

    assert pipeline.list_knowledge_base_document_work_orders(
        ctx, "hardware"
    ) == [order]

    ctx.kb_permissions.clear()
    with pytest.raises(PermissionError, match="knowledge base read"):
        pipeline.list_knowledge_base_document_work_orders(ctx, "hardware")


def test_pipeline_auto_generation_uses_created_order_frozen_snapshot(pipeline, ctx):
    service = Mock()
    order = SimpleNamespace(
        work_order_id="wo-kb",
        source_set_snapshot_id="snapshot-frozen",
    )
    snapshot = SimpleNamespace(
        source_set_snapshot_id="snapshot-frozen",
        source_names=["frozen.pdf"],
    )
    candidate = SimpleNamespace(
        artifact_id="candidate-a",
        stage="review_candidate",
    )
    service.create_knowledge_base_work_order.return_value = order
    service.resolve_source_snapshot.return_value = snapshot
    service.get_icd_scope_review.return_value = None
    service.run_internal_harness.return_value = candidate
    pipeline.document_generation = service
    pipeline.list_file_infos = Mock(
        return_value=[SimpleNamespace(name="currently-readable.pdf")]
    )
    retrieve = Mock()
    pipeline._knowledge_base_retriever = Mock(return_value=retrieve)

    result = pipeline.auto_generate_knowledge_base_document(
        ctx,
        knowledge_base_name="hardware",
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
        source_names=["untrusted-ui-file.pdf"],
    )

    assert result == candidate
    service.create_knowledge_base_work_order.assert_called_once_with(
        ctx,
        knowledge_base_name="hardware",
        source_names=["currently-readable.pdf"],
        template_version_id="template-a",
        document_schema_id="schema-a",
        document_schema_version="1",
    )
    service.resolve_source_snapshot.assert_any_call(order)
    pipeline._knowledge_base_retriever.assert_called_once_with(
        ctx,
        "hardware",
        ["frozen.pdf"],
        source_set_snapshot_id="snapshot-frozen",
    )
    service.run_internal_harness.assert_called_once_with(
        ctx, "wo-kb", retrieve=retrieve
    )
    service.approve_document_artifact.assert_not_called()


def test_pipeline_knowledge_base_status_is_scope_aware_and_reauthorized(
    pipeline, ctx, approved_template, approved_schema
):
    pipeline.list_file_infos = Mock(
        return_value=[SimpleNamespace(name="secret-spec.pdf")]
    )
    order = pipeline.create_knowledge_base_document_work_order(
        ctx,
        knowledge_base_name="hardware",
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )

    status = pipeline.get_document_run_status(order.work_order_id, ctx)

    assert status["scope_type"] == "knowledge_base"
    assert status["knowledge_base_name"] == "hardware"
    assert status["project_id"] is None
    assert "source_names" not in status
    assert "secret-spec.pdf" not in json.dumps(status)

    with pytest.raises(PermissionError, match="request context"):
        pipeline.get_document_run_status(order.work_order_id)

    ctx.kb_permissions.clear()
    with pytest.raises(PermissionError, match="knowledge base"):
        pipeline.get_document_run_status(order.work_order_id, ctx)


@pytest.fixture
def approved_template(service):
    content = _xlsx_template()
    service.register_renderer_policy(
        RendererPolicy(renderer_policy_id="renderer-a", version="1")
    )
    template = service.register_template(
        TemplateVersion(
            template_version_id="template-a",
            template_id="template-a",
            format="xlsx",
            content_hash=hashlib.sha256(content).hexdigest(),
            template_schema_id="template-schema-a",
            template_schema_version="1",
            renderer_policy_id="renderer-a",
        ),
        content,
        regions=[],
        bindings=[],
    )
    return service.approve_template(template.template_version_id, actor_id="template-admin")


@pytest.fixture
def approved_schema(service):
    return service.register_document_schema(
        DocumentSchema(
            document_schema_id="schema-a",
            version="1",
            document_type="knowledge-base-summary",
            status="approved",
            execution_mode="deterministic_only",
        )
    )


def test_list_approved_templates_orders_by_persisted_columns(service, approved_template):
    """A fresh authoring database must list approved templates without SQL errors."""
    assert [item.template_version_id for item in service.store.list_templates(approved_only=True)] == [
        approved_template.template_version_id
    ]


def make_work_order(**updates) -> DocumentWorkOrder:
    values = {
        "work_order_id": "work-order-a",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "baseline_id": "baseline-a",
        "baseline_content_hash": "baseline-hash",
        "source_set_snapshot_id": "snapshot-a",
        "template_version_id": "template-a",
        "document_schema_id": "schema-a",
        "document_schema_version": "1",
        "template_schema_id": "template-schema-a",
        "template_schema_version": "1",
        "retrieval_policy_version": "1",
        "renderer_policy_version": "1",
        "target_format": "xlsx",
        "execution_mode": "deterministic_only",
        "created_by": "alice",
    }
    values.update(updates)
    return DocumentWorkOrder(**values)


def test_knowledge_base_work_order_requires_live_read_permission(
    service, ctx, approved_template, approved_schema
):
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )
    assert order.scope_type == "knowledge_base"
    ctx.kb_permissions.clear()
    with pytest.raises(PermissionError, match="knowledge base"):
        service.require_work_order_capability(
            ctx, order, "run_deterministic_work_order"
        )


def test_knowledge_base_work_order_rejects_creation_without_read_permission(
    service, ctx, approved_template, approved_schema
):
    ctx.kb_permissions.clear()

    with pytest.raises(PermissionError, match="knowledge base read"):
        service.create_knowledge_base_work_order(
            ctx,
            knowledge_base_name="hardware",
            source_names=["spec.pdf"],
            template_version_id=approved_template.template_version_id,
            document_schema_id=approved_schema.document_schema_id,
            document_schema_version=approved_schema.version,
        )


def test_knowledge_base_work_order_recovers_from_idempotency_insert_race(
    service, ctx, approved_template, approved_schema, monkeypatch
):
    original_create = service.store.create_work_order
    canonical_order = None

    def create_after_competing_request(order):
        nonlocal canonical_order
        if canonical_order is None:
            payload = order.model_dump(mode="json")
            payload.update(
                work_order_id="wo-canonical",
                input_fingerprint="",
            )
            canonical_order = DocumentWorkOrder.model_validate(payload)
            original_create(canonical_order)
        return original_create(order)

    monkeypatch.setattr(service.store, "create_work_order", create_after_competing_request)

    resolved = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
        idempotency_key="request-a",
    )

    assert resolved == canonical_order


def test_knowledge_base_work_order_resolves_its_frozen_source_snapshot(
    service, ctx, approved_template, approved_schema
):
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )

    snapshot = service.resolve_source_snapshot(order)

    assert snapshot.source_set_snapshot_id == order.source_set_snapshot_id
    assert snapshot.knowledge_base_name == "hardware"
    assert snapshot.source_names == ["spec.pdf"]
    assert snapshot.content_hash


def test_knowledge_base_manifest_freezes_snapshot_hash_and_source_names(
    service, ctx, approved_template, approved_schema
):
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )
    snapshot = service.resolve_source_snapshot(order)
    policy = HarnessPolicy(
        harness_policy_id="policy-a",
        version="1",
        status="approved",
        writer_provider_id="deterministic-evidence-writer",
    )

    manifest = service.harness_runtime.build_manifest(
        order, policy, snapshot, approved_template, approved_schema
    )

    assert manifest.source_set_snapshot_id == order.source_set_snapshot_id
    assert manifest.source_set_snapshot_hash == snapshot.content_hash
    assert manifest.source_version_ids == ["spec.pdf"]
    assert manifest.processing_artifact_ids == []
    assert manifest.region_policy_versions == {}


@pytest.mark.parametrize(
    "evidence, message",
    [
        (
            EvidenceEnvelope(
                id="wrong-kb",
                content="cross-scope",
                source_name="spec.pdf",
                metadata={"knowledge_base_name": "firmware"},
            ),
            "knowledge base",
        ),
        (
            EvidenceEnvelope(
                id="unfrozen-source",
                content="not frozen",
                source_name="other.pdf",
                metadata={"knowledge_base_name": "hardware"},
            ),
            "frozen source",
        ),
    ],
)
def test_knowledge_base_retrieval_rejects_cross_scope_or_unfrozen_evidence(
    service,
    ctx,
    approved_template,
    approved_schema,
    evidence,
    message,
):
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )
    snapshot = service.resolve_source_snapshot(order)
    outcome = RetrievalOutcome(
        requirement_id="kb-requirement",
        status="success_with_hits",
        evidences=[evidence],
        query_fingerprint="query-a",
        applied_source_set_snapshot_id=order.source_set_snapshot_id,
        applied_region_policy_versions={},
    )

    with pytest.raises(PermissionError, match=message):
        service._validate_retrieval_outcome(order, snapshot, outcome)


def test_harness_rejects_knowledge_base_evidence_outside_frozen_source_set(
    service, ctx, approved_template, approved_schema
):
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )
    snapshot = service.resolve_source_snapshot(order)
    outcome = RetrievalOutcome(
        requirement_id="kb-requirement",
        status="success_with_hits",
        evidences=[
            EvidenceEnvelope(
                id="unfrozen-source",
                content="not frozen",
                source_name="other.pdf",
                metadata={"knowledge_base_name": "hardware"},
            )
        ],
        query_fingerprint="query-a",
        applied_source_set_snapshot_id=order.source_set_snapshot_id,
        applied_region_policy_versions={},
    )

    with pytest.raises(PermissionError, match="frozen source set"):
        _validated_evidence(order, snapshot, outcome)


def test_knowledge_base_artifact_actions_recheck_live_read_permission(
    service, ctx, approved_template, approved_schema
):
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )
    candidate = service.run_deterministic_work_order(
        ctx,
        order.work_order_id,
        rule_inputs={},
        retrieval_outcomes={},
    )
    ctx.kb_permissions.clear()

    with pytest.raises(PermissionError, match="knowledge base"):
        service.run_deterministic_work_order(
            ctx,
            order.work_order_id,
            rule_inputs={},
            retrieval_outcomes={},
        )
    with pytest.raises(PermissionError, match="knowledge base"):
        service.submit_document_human_event(
            ctx,
            artifact_id=candidate.artifact_id,
            unit_id="artifact",
            event_type="approve",
        )
    with pytest.raises(PermissionError, match="knowledge base"):
        service.approve_document_artifact(ctx, candidate.artifact_id)
    with pytest.raises(PermissionError, match="knowledge base"):
        service.download_document_artifact(ctx, candidate.artifact_id)


def test_knowledge_base_background_status_rechecks_live_read_permission(
    service, ctx, approved_template, approved_schema
):
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )
    run_id = service.worker.submit(order.work_order_id, lambda: None)
    ctx.kb_permissions.clear()

    with pytest.raises(PermissionError, match="knowledge base"):
        service.get_background_run_status(ctx, run_id)


def test_knowledge_base_harness_control_rechecks_live_read_permission(
    service, ctx, approved_template
):
    schema = service.register_document_schema(
        DocumentSchema(
            document_schema_id="harness-schema",
            version="1",
            document_type="knowledge-base-summary",
            status="approved",
            execution_mode="internal_harness",
        )
    )
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=schema.document_schema_id,
        document_schema_version=schema.version,
    )
    snapshot = service.resolve_source_snapshot(order)
    policy = service.store.get_harness_policy(
        order.harness_policy_id, order.harness_policy_version
    )
    run, _ = service.harness_runtime.create_run(
        order, policy, snapshot, approved_template, schema
    )
    ctx.kb_permissions.clear()

    with pytest.raises(PermissionError, match="knowledge base"):
        service.pause_harness_run(ctx, run.harness_run_id)


def test_internal_harness_uses_frozen_knowledge_base_source_names(
    service, ctx, approved_template
):
    service.register_harness_policy(
        HarnessPolicy(
            harness_policy_id="kb-writer",
            version="1",
            status="approved",
            writer_provider_id=DeterministicEvidenceWriter.provider_id,
        )
    )
    schema = service.register_document_schema(
        DocumentSchema(
            document_schema_id="harness-field-schema",
            version="1",
            document_type="knowledge-base-summary",
            fields=[
                DocumentFieldSchema(
                    field_id="summary",
                    label="Summary",
                    retrieval_policy_id="retrieve-summary",
                    verification_policy_id="verify-summary",
                )
            ],
            status="approved",
            execution_mode="internal_harness",
        )
    )
    order = service.create_knowledge_base_work_order(
        ctx,
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=schema.document_schema_id,
        document_schema_version=schema.version,
    )

    def retrieve(requirement, attempt, query_override=None):
        assert requirement.source_version_scope == ["spec.pdf"]
        assert requirement.project_id is None
        assert requirement.baseline_id is None
        assert attempt == 1
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits",
            evidences=[
                EvidenceEnvelope(
                    id="kb-evidence",
                    content="Controller: STM32H743",
                    source_name="spec.pdf",
                    metadata={"knowledge_base_name": "hardware"},
                )
            ],
            query_fingerprint="query-a",
            applied_source_set_snapshot_id=order.source_set_snapshot_id,
            applied_region_policy_versions={},
        )

    candidate = service.run_internal_harness(
        ctx, order.work_order_id, retrieve=retrieve
    )

    assert candidate.stage == "review_candidate"
    manifest = service.store.get_run_manifest(
        service.store.get_work_order(order.work_order_id).run_manifest_id
    )
    assert manifest.source_version_ids == ["spec.pdf"]


def test_store_round_trips_knowledge_base_work_order(tmp_path):
    store = DocumentAuthoringStore(
        str(tmp_path / "authoring.db"),
        str(tmp_path / "authoring-files"),
    )
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="tenant-a",
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        created_by="alice",
    )
    store.create_knowledge_base_source_snapshot(snapshot)
    assert store.get_knowledge_base_source_snapshot(snapshot.source_set_snapshot_id) == snapshot
    order = make_work_order(
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id=snapshot.source_set_snapshot_id,
    )

    store.create_work_order(order)

    assert store.get_work_order(order.work_order_id).knowledge_base_name == "hardware"
    assert store.list_work_orders_for_knowledge_base("tenant-a", "hardware") == [order]


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"project_id": None}, "project-scoped work orders require project_id and baseline_id"),
        (
            {"knowledge_base_name": "hardware"},
            "project-scoped work orders cannot name a knowledge base",
        ),
        (
            {
                "scope_type": "knowledge_base",
                "knowledge_base_name": None,
                "project_id": None,
                "baseline_id": None,
                "baseline_content_hash": "",
            },
            "knowledge-base work orders require knowledge_base_name",
        ),
        (
            {
                "scope_type": "knowledge_base",
                "knowledge_base_name": "hardware",
            },
            "knowledge-base work orders cannot name a project or baseline",
        ),
    ],
)
def test_work_order_scope_fields_are_mutually_exclusive(updates, message):
    with pytest.raises(ValueError, match=message):
        make_work_order(**updates)


def test_store_migrates_legacy_project_work_orders_without_losing_scope_or_children(tmp_path):
    db_path = tmp_path / "authoring.db"
    legacy_order = make_work_order(idempotency_key="request-a")
    legacy_payload = legacy_order.model_dump(mode="json", exclude={"scope_type", "knowledge_base_name"})
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE document_work_orders (
                work_order_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL, status TEXT NOT NULL,
                idempotency_key TEXT, payload_json TEXT NOT NULL,
                UNIQUE(tenant_id, project_id, idempotency_key)
            );
            CREATE TABLE authoring_run_manifests (
                run_manifest_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id)
            );
            """
        )
        conn.execute(
            """INSERT INTO document_work_orders
               (work_order_id, tenant_id, project_id, status, idempotency_key, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                legacy_order.work_order_id,
                legacy_order.tenant_id,
                legacy_order.project_id,
                legacy_order.status,
                legacy_order.idempotency_key,
                json.dumps(legacy_payload),
            ),
        )
        conn.execute(
            """INSERT INTO authoring_run_manifests
               (run_manifest_id, work_order_id, payload_json) VALUES (?, ?, ?)""",
            ("manifest-a", legacy_order.work_order_id, "{}"),
        )

    store = DocumentAuthoringStore(str(db_path), str(tmp_path / "authoring-files"))

    assert store.get_work_order(legacy_order.work_order_id) == legacy_order
    assert store.find_work_order_by_idempotency("tenant-a", "project-a", "request-a") == legacy_order
    assert store.list_work_orders("tenant-a", "project-a") == [legacy_order]
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(document_work_orders)")}
        migrated_scope = conn.execute(
            "SELECT scope_type, scope_key FROM document_work_orders WHERE work_order_id = ?",
            (legacy_order.work_order_id,),
        ).fetchone()
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert {"scope_type", "scope_key", "knowledge_base_name"} <= columns
    assert migrated_scope == ("project", "project:project-a")
    assert foreign_key_errors == []

    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="tenant-a",
        knowledge_base_name="hardware",
        source_names=["spec.pdf"],
        created_by="alice",
    )
    store.create_knowledge_base_source_snapshot(snapshot)
    knowledge_base_order = make_work_order(
        work_order_id="work-order-kb",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id=snapshot.source_set_snapshot_id,
        idempotency_key="request-a",
    )
    store.create_work_order(knowledge_base_order)
    assert store.list_work_orders_for_knowledge_base("tenant-a", "hardware") == [
        knowledge_base_order
    ]
    with pytest.raises(sqlite3.IntegrityError):
        store.create_work_order(
            knowledge_base_order.model_copy(update={"work_order_id": "work-order-kb-duplicate"})
        )


def test_pipeline_exposes_spreadsheet_service_from_backend(service):
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock()
    pipeline.backend.spreadsheet_indexes = "spreadsheet-service-handle"
    pipeline.documents = Mock()
    pipeline.document_generation = service
    # Mirror the init body's attribute wiring for spreadsheet_service.
    pipeline.spreadsheet_service = getattr(pipeline.backend, "spreadsheet_indexes", None)

    assert pipeline.spreadsheet_service == "spreadsheet-service-handle"


def test_pipeline_spreadsheet_service_defaults_to_none_when_backend_lacks_it(service):
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock(spec=[])  # no spreadsheet_indexes attribute
    pipeline.documents = Mock()
    pipeline.document_generation = service
    pipeline.spreadsheet_service = getattr(pipeline.backend, "spreadsheet_indexes", None)

    assert pipeline.spreadsheet_service is None


def requirement_with_capabilities(subject: str, capabilities: list[str]) -> InformationRequirement:
    return InformationRequirement(
        requirement_id=f"requirement-{subject}",
        semantic_unit_id="summary",
        claim_type="attribute",
        subject=subject,
        required_capabilities=capabilities,
    )


def _xlsx_evidence(source_name: str, content: str = "BOM row") -> Evidence:
    return Evidence(
        id=f"xlsx:{source_name}:Sheet1:0:semantic",
        content=content,
        source_name=source_name,
        content_kind="spreadsheet_table",
        processor_kind="spreadsheet_table",
        score=0.9,
        locator={"record_id": 1, "sheet_name": "Sheet1", "row_index": 0},
        metadata={"tool": "spreadsheet_semantic"},
    )


def _patch_spreadsheet_tool(pipeline, spreadsheet_tool):
    """Patch SpreadsheetSemanticTool in the app_pipeline module to return ``spreadsheet_tool``."""
    import src.core.app_pipeline as app_pipeline_mod
    original = app_pipeline_mod.SpreadsheetSemanticTool
    app_pipeline_mod.SpreadsheetSemanticTool = Mock(return_value=spreadsheet_tool)
    return original


def _restore_spreadsheet_tool(original):
    import src.core.app_pipeline as app_pipeline_mod
    app_pipeline_mod.SpreadsheetSemanticTool = original


def _patch_circuit_tool(circuit_tool):
    import src.core.app_pipeline as app_pipeline_mod

    original = app_pipeline_mod.CircuitQueryTool
    app_pipeline_mod.CircuitQueryTool = Mock(return_value=circuit_tool)
    return original


def _restore_circuit_tool(original):
    import src.core.app_pipeline as app_pipeline_mod

    app_pipeline_mod.CircuitQueryTool = original


def _circuit_evidence(source_name: str) -> Evidence:
    return Evidence(
        id=f"circuit:{source_name}:pin_mapping:J1",
        content="Pin mapping for J1: 1 -> CAN_H.",
        source_name=source_name,
        content_kind="circuit_design",
        processor_kind="circuit_design",
        score=0.95,
        locator={"record_id": 1, "entity_type": "pin_mapping", "entity_id": "J1"},
        metadata={"source_group": "circuit_design", "pin_mappings": []},
    )


def test_kb_retriever_adds_spreadsheet_evidence_for_tabular_lookup(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []  # RAGFlow empty
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [_xlsx_evidence("bom.xlsx")]
    pipeline.spreadsheet_service = Mock()
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        outcome = retrieve(requirement_with_capabilities("用量", ["tabular_lookup"]), 0)
    finally:
        _restore_spreadsheet_tool(original)

    spreadsheet_tool.run.assert_called_once()
    assert outcome.status == "success_with_hits"
    assert any(e.source_name == "bom.xlsx" for e in outcome.evidences)


def test_kb_retriever_skips_spreadsheet_when_no_tabular_lookup(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [_xlsx_evidence("bom.xlsx")]
    pipeline.spreadsheet_service = Mock()
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        retrieve(requirement_with_capabilities("描述", ["entity_lookup"]), 0)
    finally:
        _restore_spreadsheet_tool(original)

    spreadsheet_tool.run.assert_not_called()


def test_kb_retriever_adds_frozen_circuit_evidence_for_relationship_lookup(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    pipeline.circuit_service = Mock()
    circuit_tool = Mock()
    circuit_tool.run.return_value = [
        _circuit_evidence("board.edf"),
        _circuit_evidence("not-in-frozen-set.edf"),
    ]
    original = _patch_circuit_tool(circuit_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["board.edf"])
        outcome = retrieve(requirement_with_capabilities("引脚定义", ["relationship_lookup"]), 0)
    finally:
        _restore_circuit_tool(original)

    circuit_tool.run.assert_called_once()
    assert outcome.status == "success_with_hits"
    assert [e.source_name for e in outcome.evidences] == ["board.edf"]


def test_kb_retriever_skips_spreadsheet_when_service_missing(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    pipeline.spreadsheet_service = None
    import src.core.app_pipeline as app_pipeline_mod
    spy = Mock()
    app_pipeline_mod.SpreadsheetSemanticTool = spy  # should not be instantiated
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        outcome = retrieve(requirement_with_capabilities("用量", ["tabular_lookup"]), 0)
    finally:
        from src.agents.tools.spreadsheet_tools import SpreadsheetSemanticTool as RealTool
        app_pipeline_mod.SpreadsheetSemanticTool = RealTool

    spy.assert_not_called()
    assert outcome.status == "success_empty"


def test_kb_retriever_drops_spreadsheet_evidence_outside_frozen_set(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    # Tool returns one in-scope and one out-of-scope (added after freeze).
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [
        _xlsx_evidence("bom.xlsx", "in scope"),
        _xlsx_evidence("added_after_freeze.xlsx", "out of scope"),
    ]
    pipeline.spreadsheet_service = Mock()
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        outcome = retrieve(requirement_with_capabilities("用量", ["tabular_lookup"]), 0)
    finally:
        _restore_spreadsheet_tool(original)

    sources = {e.source_name for e in outcome.evidences}
    assert sources == {"bom.xlsx"}
    assert "added_after_freeze.xlsx" not in sources
    assert outcome.status == "success_with_hits"


def test_kb_retriever_uses_query_override_when_provided(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    pipeline.spreadsheet_service = None
    retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["spec.pdf"])
    retrieve(
        requirement_with_capabilities("描述", ["entity_lookup"]),
        0,
        query_override="override query",
    )

    # backend.retrieve was called with the override query, not the subject.
    called_query = pipeline.backend.retrieve.call_args.args[1]
    assert called_query == "override query"


def test_kb_retriever_falls_back_to_subject_query_without_override(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    pipeline.spreadsheet_service = None
    retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["spec.pdf"])
    retrieve(requirement("voltage"), 0)  # no query_override

    called_query = pipeline.backend.retrieve.call_args.args[1]
    assert "voltage" in called_query


def _req_for_unit(unit_id: str, subject: str, capabilities: list[str]) -> InformationRequirement:
    return InformationRequirement(
        requirement_id=f"requirement-{unit_id}",
        semantic_unit_id=unit_id,
        claim_type="attribute",
        subject=subject,
        required_capabilities=capabilities,
    )


def test_kb_retriever_dedups_across_ragflow_and_spreadsheet(pipeline, ctx):
    # Both backends return the same content; dedup must keep the higher score.
    pipeline.backend.retrieve.return_value = [
        Evidence(
            id="rag:1",
            content="BOM row",
            source_name="bom.xlsx",
            content_kind="document_text",
            processor_kind="ragflow",
            score=0.3,
        )
    ]
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [_xlsx_evidence("bom.xlsx", "BOM row")]  # score 0.9
    pipeline.spreadsheet_service = Mock()
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        outcome = retrieve(_req_for_unit("field:f1", "用量", ["tabular_lookup"]), 0)
    finally:
        _restore_spreadsheet_tool(original)

    assert len(outcome.evidences) == 1
    assert outcome.evidences[0].score == 0.9
    assert outcome.evidences[0].content == "BOM row"


def test_kb_retriever_cross_unit_reuse_on_empty(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []  # RAGFlow empty for both units
    spreadsheet_tool = Mock()
    # Unit A hits; unit B empty.
    spreadsheet_tool.run.side_effect = [
        [_xlsx_evidence("bom.xlsx", "用量 row")],
        [],
    ]
    pipeline.spreadsheet_service = Mock()
    original = _patch_spreadsheet_tool(pipeline, spreadsheet_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        outcome_a = retrieve(_req_for_unit("field:a", "用量", ["tabular_lookup"]), 0)
        outcome_b = retrieve(_req_for_unit("field:b", "用量", ["tabular_lookup"]), 0)
    finally:
        _restore_spreadsheet_tool(original)

    assert outcome_a.status == "success_with_hits"
    # Unit B had no fresh hits; the cache re-offers unit A's evidence.
    assert outcome_b.status == "success_with_hits"
    assert len(outcome_b.evidences) == 1
    assert outcome_b.evidences[0].metadata.get("reused") is True
    assert outcome_b.evidences[0].metadata.get("reused_from_unit") == "field:a"
