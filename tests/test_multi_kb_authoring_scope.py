"""R3 T8/T9: authoring adapters retain the frozen multi-source boundary."""

from __future__ import annotations

from src.agents.schemas import Evidence
from src.document_authoring.evidence import KnowledgeBaseEvidenceProvider
from src.document_authoring.models import KnowledgeBaseSourceSnapshot
from tests.test_multi_kb_store_contract import snapshot


def test_source_snapshot_can_persist_multi_source_roles_and_versions():
    result = KnowledgeBaseSourceSnapshot.create(
        tenant_id="default",
        knowledge_base_name="KB101",
        source_names=["spec.pdf", "standard.pdf"],
        created_by="1",
        source_snapshot_kind="multi_knowledge_v1",
        sources=[
            {"kb_id": 101, "kb_name": "KB101", "role": "primary", "source_name": "spec.pdf", "local_record_id": 7, "source_content_hash": "a" * 64, "index_revision": 2},
            {"kb_id": 205, "kb_name": "KB205", "role": "standard", "source_name": "standard.pdf", "local_record_id": 9, "source_content_hash": "b" * 64, "index_revision": 3},
        ],
    )
    assert result.source_snapshot_kind == "multi_knowledge_v1"
    assert {item["kb_id"] for item in result.sources} == {101, 205}
    assert len(result.content_hash) == 64


def test_authoring_kb_provider_delegates_to_scoped_multi_service():
    scope = snapshot(1)
    calls = []

    class Service:
        def search(self, **kwargs):
            calls.append(kwargs)
            return type("Outcome", (), {
                "evidence": [
                    Evidence(
                        id="kb205:chunk",
                        content="reference evidence",
                        source_name="standard.pdf",
                        content_kind="document_text",
                        processor_kind="ragflow",
                        metadata={
                            "kb_id": 205,
                            "knowledge_source": scope.sources[1].model_dump(mode="json"),
                        },
                    )
                ]
            })()

    provider = KnowledgeBaseEvidenceProvider(
        object(),
        top_k=4,
        multi_kb_service=Service(),
        knowledge_scope=scope,
        retrieval_budget=object(),
    )
    rows = provider.retrieve(
        query="voltage",
        source_scope="knowledge_base_only",
        attachment_ids=[],
        kb_name="KB101",
        ctx=type("Context", (), {"has_kb_permission": lambda *_: True})(),
    )
    assert calls and calls[0]["snapshot"] is scope
    assert rows[0].metadata["knowledge_base_name"] == "KB205"
