from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import src.settings
from src.agents.claim_evidence import InformationRequirement
from src.agents.schemas import Evidence
from src.core.app_pipeline import AppPipeline
from src.document_authoring.service import DocumentGenerationService
from src.pipelines.document_rag.schemas import RequestContext


class _AttachmentRetrieval:
    def __init__(self):
        self.calls: list[dict] = []

    def search(self, query, *, asset_ids, limit, context_token_budget=None):
        self.calls.append({"query": query, "asset_ids": list(asset_ids), "limit": limit})
        return SimpleNamespace(
            chunks=[SimpleNamespace(
                asset_id="asset-1",
                part_id="part-1",
                ordinal=1,
                part_type="text",
                text_content="U1 connects to CAN_RX",
                locator={"page": 2},
                metadata={},
                score=0.9,
                backend="fts5",
            )],
        )


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="alice",
        tenant_id="tenant-a",
        metadata={"resource_department_id": "7"},
        kb_permissions={"7:hardware": "read"},
    )


def _requirement() -> InformationRequirement:
    return InformationRequirement(
        requirement_id="requirement-1",
        semantic_unit_id="field-1",
        claim_type="attribute",
        subject="CAN_RX",
    )


def _ref() -> dict:
    return {
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


def test_document_retriever_uses_attachment_provider_without_calling_kb():
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock()
    pipeline.backend.retrieve.return_value = [
        SimpleNamespace(
            id="kb-1",
            content="must not be used",
            source_name="spec.pdf",
            score=0.8,
            metadata={},
            backend="ragflow",
            retriever="standard",
        )
    ]
    pipeline.document_generation = DocumentGenerationService
    pipeline.spreadsheet_service = None
    attachment_retrieval = _AttachmentRetrieval()

    retrieve = pipeline._knowledge_base_retriever(
        _ctx(),
        "hardware",
        ["spec.pdf"],
        source_set_snapshot_id="snapshot-1",
        source_scope="attachment_only",
        attachment_refs=[_ref()],
        attachment_retrieval=attachment_retrieval,
    )
    outcome = retrieve(_requirement(), 0)

    pipeline.backend.retrieve.assert_not_called()
    assert attachment_retrieval.calls == [
        {"query": "CAN_RX", "asset_ids": ["asset-1"], "limit": src.settings.FINAL_TOP_K}
    ]
    assert [item.metadata["source_type"] for item in outcome.evidences] == [
        "chat_attachment"
    ]


def test_document_retriever_combines_frozen_kb_and_attachment_evidence():
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock()
    pipeline.backend.retrieve.return_value = [
        SimpleNamespace(
            id="kb-1",
            content="CAN_RX is defined by the specification",
            source_name="spec.pdf",
            score=0.8,
            metadata={},
            backend="ragflow",
            retriever="standard",
        )
    ]
    pipeline.document_generation = DocumentGenerationService
    pipeline.spreadsheet_service = None
    attachment_retrieval = _AttachmentRetrieval()

    retrieve = pipeline._knowledge_base_retriever(
        _ctx(),
        "hardware",
        ["spec.pdf"],
        source_set_snapshot_id="snapshot-1",
        source_scope="attachment_and_knowledge_base",
        attachment_refs=[_ref()],
        attachment_retrieval=attachment_retrieval,
    )
    outcome = retrieve(_requirement(), 0)

    pipeline.backend.retrieve.assert_called_once()
    assert [item.metadata["source_type"] for item in outcome.evidences] == [
        "knowledge_base",
        "chat_attachment",
    ]


def test_combined_document_retrieval_does_not_widen_an_empty_kb_snapshot():
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock()
    pipeline.backend.retrieve.return_value = [
        SimpleNamespace(
            id="kb-1",
            content="must not be used",
            source_name="unfrozen.pdf",
            score=0.8,
            metadata={},
            backend="ragflow",
            retriever="standard",
        )
    ]
    pipeline.document_generation = DocumentGenerationService
    pipeline.spreadsheet_service = None

    retrieve = pipeline._knowledge_base_retriever(
        _ctx(),
        "hardware",
        [],
        source_set_snapshot_id="snapshot-1",
        source_scope="attachment_and_knowledge_base",
        attachment_refs=[_ref()],
        attachment_retrieval=_AttachmentRetrieval(),
    )
    outcome = retrieve(_requirement(), 0)

    pipeline.backend.retrieve.assert_not_called()
    assert [item.metadata["source_type"] for item in outcome.evidences] == [
        "chat_attachment"
    ]


def test_attachment_only_icd_preflight_does_not_query_kb_or_kb_circuit_index():
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock()
    pipeline.backend.retrieve.return_value = []
    pipeline.list_file_infos = Mock(return_value=[])
    pipeline.circuit_service = Mock()
    pipeline.circuit_service.list_pin_mapping_evidence.return_value = []
    pipeline.document_generation = Mock()
    order = SimpleNamespace(
        work_order_id="wo-icd",
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        target_format="xlsx",
        source_scope_snapshot="attachment_only",
        attachment_refs_snapshot=[_ref()],
    )
    pipeline.document_generation.create_knowledge_base_work_order.return_value = order
    pipeline.document_generation.resolve_source_snapshot.return_value = SimpleNamespace(
        source_set_snapshot_id="snapshot-1",
        source_names=[],
    )
    pipeline.document_generation.store.read_template_content.return_value = b""
    pipeline.document_generation._schema.return_value = SimpleNamespace(
        document_type="icd",
        fields=[SimpleNamespace(
            label="Connector pin definition",
            description="",
            query_terms=["connector J7 pinout"],
            subject_aliases=[],
            value_schema={},
        )],
    )
    pipeline.document_generation.prepare_icd_scope_review.return_value = SimpleNamespace(
        pending_count=1,
        exceptions=[],
    )

    result = pipeline.prepare_knowledge_base_document_generation(
        _ctx(),
        knowledge_base_name="hardware",
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        source_scope="attachment_only",
        attachment_refs=[_ref()],
    )

    assert result["stage"] == "scope_review_required"
    pipeline.backend.retrieve.assert_not_called()
    pipeline.circuit_service.list_pin_mapping_evidence.assert_not_called()


def test_knowledge_base_outcome_accepts_only_declared_attachment_provenance():
    attachment = Evidence(
        id="att-1",
        content="U1 connects to CAN_RX",
        source_name="board.edf",
        content_kind="text",
        processor_kind="local_attachment:fts5",
        metadata={"source_type": "chat_attachment", "attachment_id": "att-1"},
    )

    outcome = DocumentGenerationService.build_knowledge_base_retrieval_outcome(
        "hardware",
        ["spec.pdf"],
        [attachment],
        requirement_id="requirement-1",
        source_set_snapshot_id="snapshot-1",
        attachment_ids=["att-1"],
    )

    assert outcome.status == "success_with_hits"
    assert outcome.evidences[0].metadata["source_type"] == "chat_attachment"

    with pytest.raises(PermissionError, match="attachment"):
        DocumentGenerationService.build_knowledge_base_retrieval_outcome(
            "hardware",
            ["spec.pdf"],
            [attachment.model_copy(update={
                "metadata": {"source_type": "chat_attachment", "attachment_id": "att-foreign"},
            })],
            attachment_ids=["att-1"],
        )


def test_existing_work_order_rebuilds_retriever_from_persisted_source_scope_snapshot():
    pipeline = object.__new__(AppPipeline)
    order = SimpleNamespace(
        work_order_id="wo-1",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        source_scope_snapshot="attachment_only",
        attachment_refs_snapshot=[_ref()],
    )
    snapshot = SimpleNamespace(
        source_set_snapshot_id="snapshot-1",
        source_names=["spec.pdf"],
    )
    pipeline.document_generation = Mock()
    pipeline.document_generation.store.get_work_order.return_value = order
    pipeline.document_generation.resolve_source_snapshot.return_value = snapshot
    pipeline.document_generation.get_icd_scope_review.return_value = None
    pipeline.document_generation.run_internal_harness.return_value = "completed"
    pipeline._knowledge_base_retriever = Mock(return_value="retrieve")

    result = pipeline.continue_knowledge_base_document_generation(_ctx(), "wo-1")

    assert result == "completed"
    pipeline._knowledge_base_retriever.assert_called_once_with(
        _ctx(),
        "hardware",
        ["spec.pdf"],
        source_set_snapshot_id="snapshot-1",
        icd_scope_review=None,
        source_scope="attachment_only",
        attachment_refs=[_ref()],
    )


def test_attachment_only_work_order_does_not_require_a_live_kb_source_list():
    pipeline = object.__new__(AppPipeline)
    pipeline.list_file_infos = Mock(return_value=[])
    pipeline.document_generation = Mock()
    pipeline.document_generation.create_knowledge_base_work_order.return_value = SimpleNamespace(
        work_order_id="wo-attachment-only",
        source_scope_snapshot="attachment_only",
    )

    order = pipeline.create_knowledge_base_document_work_order(
        _ctx(),
        knowledge_base_name="hardware",
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        source_scope="attachment_only",
        attachment_refs=[_ref()],
    )

    assert order.work_order_id == "wo-attachment-only"
    pipeline.list_file_infos.assert_not_called()
    pipeline.document_generation.create_knowledge_base_work_order.assert_called_once()
    assert pipeline.document_generation.create_knowledge_base_work_order.call_args.kwargs[
        "source_names"
    ] == []
