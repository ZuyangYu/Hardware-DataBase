from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from src.agents.claim_evidence import RetrievalOutcome
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
from test_document_authoring_p2a import _xlsx_template


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

    def retrieve(requirement, attempt):
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
