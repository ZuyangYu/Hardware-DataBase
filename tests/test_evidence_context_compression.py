"""Context compression keeps citation identity and never rewrites text."""

from __future__ import annotations

from src.agents.schemas import Evidence
from src.agents.tools.runtime import compress_evidence_for_context, format_evidence_for_llm


def _item(index: int) -> Evidence:
    return Evidence(
        id=f"e-{index}",
        content=f"原始证据 {index} " + "X" * 1000,
        source_name=f"doc-{index}.docx",
        content_kind="document_text",
        processor_kind="ragflow",
        locator={"chunk_id": f"chunk-{index}", "page": index},
        metadata={"citation_number": index, "content_hash": f"hash-{index}"},
    )


def test_compression_keeps_citation_id_source_locator_and_hash():
    items = [_item(index) for index in range(1, 5)]
    full = format_evidence_for_llm(items)
    compressed = compress_evidence_for_context(items, max_bytes=2000)

    assert len(compressed.encode("utf-8")) <= 2100
    assert len(compressed) < len(full)
    assert 'compressed="true"' in compressed
    for index in range(1, 5):
        assert f'id="{index}"' in compressed
        assert f"doc-{index}.docx" in compressed
        assert f"chunk-{index}" in compressed
    assert "完整原文可按 evidence id 回读" in compressed


def test_short_attachment_source_is_kept_intact_under_pressure():
    from src.agents.schemas import Evidence

    short = Evidence(
        id="att-bom",
        content="# 上一代 BOM\n" + "| 电源 | LM61460-Q1 |\n| 以太网 | DP83TG720S-Q1 |\n| MCU | TC377 |\n" + "尾行标记 END-OF-BOM",
        source_name="previous_bom.md",
        content_kind="text",
        processor_kind="local_attachment",
        locator={"line_start": 1, "line_end": 8},
        metadata={
            "citation_number": 1,
            "source_type": "attachment",
            "attachment_id": "att-1",
        },
    )
    long_items = [_item(index) for index in range(2, 12)]
    compressed = compress_evidence_for_context([short, *long_items], max_bytes=6000)
    assert "DP83TG720S-Q1" in compressed
    assert "尾行标记 END-OF-BOM" in compressed
    assert 'compressed="true"' in compressed


def test_small_context_is_rendered_unchanged():
    items = [_item(1)]
    full = format_evidence_for_llm(items)
    assert compress_evidence_for_context(items, max_bytes=100000) == full
