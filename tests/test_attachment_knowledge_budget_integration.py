"""T0 regression: attachment prefetch must not consume the KB evidence slot."""

from __future__ import annotations

from types import SimpleNamespace

import src.settings as settings
from src.agents.schemas import Evidence
from src.agents.tools.runtime import ToolRuntime


def _runtime():
    return ToolRuntime(
        kb_name="KB5",
        ctx=None,
        knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
        source_scope="attachment_and_knowledge_base",
        question="对比附件 HSI 和知识库 HSI 的管脚 2 电压",
        evidence_budget=30,
    )


def _attachment(index: int) -> Evidence:
    return Evidence(
        id=f"att-{index}", content=f"attachment chunk {index} " + "A" * 800,
        source_name="user-hsi.docx", content_kind="text",
        processor_kind="local_attachment:lexical",
        metadata={"source_type": "attachment", "attachment_id": "att-1", "asset_id": "asset-1"},
        locator={"heading_path": ["HSI"], "paragraph": index},
    )


def _kb() -> Evidence:
    return Evidence(
        id="kb-1", content="知识库 HSI 管脚 2 电压为 5V。" + "K" * 300,
        source_name="HSI.docx", content_kind="document_text", processor_kind="ragflow",
        metadata={"source_type": "knowledge_base_document", "kb_id": 5, "local_record_id": 9},
        locator={"chunk_id": "chunk-1"},
    )


def test_baseline_shared_item_budget_can_reject_a_successful_kb_candidate(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_EVIDENCE_MAX_ITEMS", 2)
    monkeypatch.setattr(settings, "MULTI_KB_CONTEXT_MAX_TOKENS", 10**6)
    monkeypatch.setattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES", 10**6)
    runtime = _runtime()

    # Baseline behaviour: the old prefetch registered every attachment chunk
    # as formal evidence before the model ran.
    numbers = runtime.add_evidence([_attachment(index) for index in range(2)])
    assert numbers == [1, 2]

    kb_number = runtime.add_evidence([_kb()])
    assert kb_number == [None]
    assert runtime.last_rejection_reasons["item_limit"] == 1
    # The source returned a qualified row; only the shared admission budget
    # blocked it.  This is not a "knowledge base has no content" fact.
    assert runtime.last_rejection_reasons["byte_limit"] == 0
    assert runtime.last_rejection_reasons["token_limit"] == 0


def test_deferred_prefetch_leaves_the_kb_slot_available(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_EVIDENCE_MAX_ITEMS", 3)
    monkeypatch.setattr(settings, "MULTI_KB_CONTEXT_MAX_TOKENS", 10**6)
    monkeypatch.setattr(settings, "MULTI_KB_TURN_EVIDENCE_MAX_BYTES", 10**6)
    runtime = _runtime()

    # Candidate-only prefetch never calls add_evidence.
    assert runtime.evidence == []
    assert runtime.add_evidence([_kb()]) == [1]
    assert runtime.evidence[0].metadata["source_type"] == "knowledge_base_document"


def test_rejection_reasons_distinguish_limits():
    runtime = _runtime()
    runtime.last_rejection_reasons = {
        "call_quota": 0, "item_limit": 0, "byte_limit": 0,
        "token_limit": 0, "source_budget_exhausted": 0,
        "global_budget_exhausted": 0,
    }
    assert set(runtime.last_rejection_reasons) >= {
        "token_limit", "byte_limit", "item_limit", "call_quota",
        "source_budget_exhausted", "global_budget_exhausted",
    }
