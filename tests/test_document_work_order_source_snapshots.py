from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

from src.agents.tools.document_authoring_tools import DocumentAuthoringToolset
from src.document_authoring.chat_context import DocumentContextInput, build_document_context
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.models import DocumentWorkOrder, compute_input_fingerprint_v2
from src.attachments.models import AttachmentRef
from src.pipelines.document_rag.schemas import RequestContext


def _work_order(**updates) -> DocumentWorkOrder:
    values = {
        "work_order_id": "wo-snapshot",
        "tenant_id": "tenant-a",
        "scope_type": "knowledge_base",
        "knowledge_base_name": "hardware",
        "knowledge_base_id": "kb-7",
        "project_id": None,
        "baseline_id": None,
        "baseline_content_hash": "",
        "source_set_snapshot_id": "sources-1",
        "template_version_id": "template-1",
        "document_schema_id": "schema-1",
        "document_schema_version": "1",
        "template_schema_id": "template-schema-1",
        "template_schema_version": "1",
        "retrieval_policy_version": "1",
        "renderer_policy_version": "1",
        "target_format": "xlsx",
        "execution_mode": "deterministic_only",
        "requested_executor": "deterministic_only",
        "input_fingerprint_version": 2,
        "created_by": "alice",
    }
    values.update(updates)
    return DocumentWorkOrder(**values)


def test_new_source_scope_snapshots_are_bound_to_v2_fingerprint():
    attachment = {
        "attachment_id": "att-1",
        "asset_id": "asset-1",
        "session_id": 42,
        "filename": "board.edf",
        "media_type": "application/x-edif",
        "extension": ".edf",
        "size_bytes": 100,
        "sha256": "source-hash",
        "usage_hint": "data",
        "parse_status": "ready",
    }
    with_snapshot = _work_order(
        source_scope_snapshot="attachment_and_knowledge_base",
        attachment_refs_snapshot=[attachment],
        kb_scope_snapshot={
            "tenant_id": "tenant-a",
            "knowledge_base_name": "hardware",
            "resource_department_id": "7",
            "knowledge_base_id": "kb-7",
        },
    )
    without_snapshot = _work_order()

    assert with_snapshot.attachment_refs_snapshot == [attachment]
    assert with_snapshot.kb_scope_snapshot["knowledge_base_name"] == "hardware"
    assert with_snapshot.input_fingerprint == compute_input_fingerprint_v2(with_snapshot)
    assert with_snapshot.input_fingerprint != without_snapshot.input_fingerprint


def test_legacy_work_order_payload_without_empty_snapshot_fields_keeps_fingerprint():
    legacy = _work_order(
        input_fingerprint_version=1,
        requested_executor=None,
        source_scope_snapshot="",
        attachment_refs_snapshot=[],
        kb_scope_snapshot={},
    )
    payload = legacy.model_dump(mode="json")
    payload.pop("source_scope_snapshot")
    payload.pop("attachment_refs_snapshot")
    payload.pop("kb_scope_snapshot")

    restored = DocumentWorkOrder.model_validate(payload)

    assert restored.input_fingerprint == legacy.input_fingerprint


def test_attachment_ref_snapshot_accepts_dataclass_refs_as_jsonable_values():
    ref = AttachmentRef(
        attachment_id="att-1",
        asset_id="asset-1",
        session_id=42,
        filename="board.edf",
        media_type="application/x-edif",
        extension=".edf",
        size_bytes=100,
        sha256="source-hash",
        usage_hint="data",
        parse_status="ready",
    )

    order = _work_order(
        source_scope_snapshot="attachment_only",
        attachment_refs_snapshot=[asdict(ref)],
    )

    assert order.attachment_refs_snapshot[0]["attachment_id"] == "att-1"


def test_document_toolset_forwards_runtime_source_scope_and_attachment_refs(tmp_path):
    ctx = RequestContext(
        user_id="alice",
        tenant_id="tenant-a",
        session_id="chat-1",
        metadata={"resource_department_id": "7"},
        kb_permissions={"7:hardware": "write"},
    )
    context = build_document_context(
        DocumentContextInput(
            analysis_id="analysis-1",
            template_version_id="template-1",
            knowledge_base_name="hardware",
            client_request_id="request-1",
        ),
        ctx=ctx,
        expected_kb="hardware",
    )
    ref = AttachmentRef(
        attachment_id="att-1",
        asset_id="asset-1",
        session_id=42,
        filename="board.edf",
        media_type="application/x-edif",
        extension=".edf",
        size_bytes=100,
        sha256="source-hash",
        usage_hint="data",
        parse_status="ready",
    )

    class Pipeline:
        def __init__(self):
            self.kwargs = None

        def create_knowledge_base_document_work_order(self, _ctx, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(work_order_id="wo-1", execution_mode="deterministic_only")

    pipeline = Pipeline()
    toolset = DocumentAuthoringToolset(
        pipeline=pipeline,
        ctx=ctx,
        context=context,
        chat_session_id="chat-1",
        job_store=DocumentAuthoringJobStore(str(tmp_path / "jobs.db")),
        attachment_refs=[ref],
        source_scope="attachment_and_knowledge_base",
    )

    result = toolset.create_document_work_order(
        document_schema_id="schema-1",
        document_schema_version="1",
        execution_mode="deterministic_only",
    )

    assert result.status == "succeeded"
    assert pipeline.kwargs["source_scope"] == "attachment_and_knowledge_base"
    assert pipeline.kwargs["attachment_refs"] == [ref]
