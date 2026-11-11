from __future__ import annotations

from src.services.knowledge_retrieval import (
    fuse_results,
    lexical_search,
    raw_file_matches,
)


def _chunk(index: int, content: str) -> dict:
    return {"index": index, "content": content, "metadata": {}, "rag_id": ""}


def test_lexical_search_prefers_specific_document_over_generic_repetition():
    grouped = {
        ("f-generic", "workflow.md"): [
            _chunk(0, "电源 计算 表 电源 计算 表 " * 20),
        ],
        ("f-specific", "03-power-calc.md"): [
            _chunk(
                0,
                "# 电源计算表\n## 耗流数据三源铁律\n"
                "每个电流值只允许三个来源：芯片手册、设计指南、用户确认记录。",
            ),
        ],
    }
    results = lexical_search(grouped, "耗流数据三源铁律", top_k=5)
    assert results
    assert results[0]["file_id"] == "f-specific"


def test_fuse_results_dedupes_files_and_keeps_best_chunk_per_file():
    semantic = [
        {"file_id": "f-generic", "file_name": "SKILL.md", "chunk_index": 0, "score": 0.9},
        {"file_id": "f-specific", "file_name": "03-power-calc.md", "chunk_index": 0, "score": 0.5},
        {"file_id": "f-generic", "file_name": "SKILL.md", "chunk_index": 1, "score": 0.8},
        {"file_id": "f-generic", "file_name": "SKILL.md", "chunk_index": 2, "score": 0.7},
    ]
    lexical = [
        {"file_id": "f-specific", "file_name": "03-power-calc.md", "chunk_index": 0, "score": 12.0},
        {"file_id": "f-generic", "file_name": "SKILL.md", "chunk_index": 0, "score": 4.0},
    ]
    fused = fuse_results(semantic, lexical, query="电源计算表 三源铁律", top_k=5)
    assert fused[0]["file_id"] == "f-specific"
    assert len({row["file_id"] for row in fused}) == len(fused)
    assert fused[0]["lexical_rank"] == 1


def test_raw_file_matches_filename_or_path_but_not_content():
    rows = [
        {
            "id": "ragflow:118",
            "name": "_audit.py",
            "relative_path": "hd_sloution/scripts/checks/_audit.py",
            "status": "raw_archived",
            "processor_kind": "raw_file",
            "directory_id": 30,
        },
        {
            "id": "ragflow:79",
            "name": "SKILL.md",
            "relative_path": "hd_sloution/SKILL.md",
            "status": "parsed",
            "processor_kind": "ragflow",
            "directory_id": 27,
        },
    ]
    by_name = raw_file_matches(rows, "_audit.py")
    assert by_name and by_name[0]["file_id"] == "ragflow:118"
    assert by_name[0]["matched_by"] == "filename"

    by_path = raw_file_matches(rows, "scripts/checks/_audit")
    assert by_path and by_path[0]["file_id"] == "ragflow:118"

    # Content-only needles are intentionally not searched by this fallback.
    assert raw_file_matches(rows, "label_path_dist") == []
