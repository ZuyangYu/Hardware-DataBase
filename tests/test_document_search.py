from __future__ import annotations

import pytest

from src.services.document_search import (
    DocumentSearchRequest,
    search_documents,
)


class _Backend:
    def __init__(self, rows, chunks, semantic_rows=None):
        self.rows = rows
        self.chunks = chunks
        self.semantic_rows = semantic_rows or []
        self.semantic_calls = 0

    def list_files(self):
        return self.rows

    def load_chunks(self, file_id):
        return self.chunks.get(file_id, [])

    def semantic_search(self, query, top_k):
        self.semantic_calls += 1
        return self.semantic_rows


def _row(file_id, name, path, processor="ragflow", status="parsed"):
    return {
        "id": file_id,
        "name": name,
        "relative_path": path,
        "directory_id": 0,
        "processor_kind": processor,
        "status": status,
    }


def _chunk(content):
    return {"index": 0, "content": content, "metadata": {}, "rag_id": ""}


def test_auto_uses_fast_lexical_path_when_confidence_is_high():
    backend = _Backend(
        rows=[_row("f1", "spec.md", "docs/spec.md")],
        chunks={"f1": [_chunk("X1900 连接器 UBD 供电 9~16V")]},
    )
    outcome = search_documents(
        backend, DocumentSearchRequest(query="UBD 供电", top_k=5)
    )
    assert outcome.mode == "auto"
    assert outcome.route == "lexical"
    assert outcome.results
    assert backend.semantic_calls == 0


def test_auto_escalates_to_semantic_when_lexical_is_ambiguous():
    rows = [
        _row("f1", "a.md", "docs/a.md"),
        _row("f2", "b.md", "docs/b.md"),
    ]
    backend = _Backend(
        rows=rows,
        chunks={
            "f1": [_chunk("电源 树 说明")],
            "f2": [_chunk("电源 树 说明")],
        },
        semantic_rows=[
            {
                "file_id": "f2",
                "file_name": "docs/b.md",
                "chunk_index": 0,
                "rag_id": "r2",
                "locator": {"chunk_index": 0},
                "score": 0.9,
                "snippet": "电源树",
            }
        ],
    )
    outcome = search_documents(
        backend, DocumentSearchRequest(query="电源树", top_k=5)
    )
    assert outcome.mode == "auto"
    assert outcome.route == "hybrid"
    assert backend.semantic_calls == 1
    assert outcome.semantic_results == 1


def test_hybrid_forces_semantic_and_exact_skips_it():
    backend = _Backend(
        rows=[_row("f1", "spec.md", "docs/spec.md")],
        chunks={"f1": [_chunk("X1900 连接器 UBD 供电")]},
        semantic_rows=[],
    )
    hybrid = search_documents(
        backend, DocumentSearchRequest(query="UBD 供电", mode="hybrid", top_k=5)
    )
    assert hybrid.mode == "hybrid"
    assert hybrid.route == "hybrid"
    assert backend.semantic_calls == 1

    exact = search_documents(
        backend, DocumentSearchRequest(query="X1900", mode="exact", top_k=5)
    )
    assert exact.mode == "exact"
    assert exact.route == "exact"
    assert backend.semantic_calls == 1


def test_raw_fallback_and_removed_mode_rejection():
    backend = _Backend(
        rows=[
            _row("f1", "spec.md", "docs/spec.md"),
            _row(
                "r1",
                "fw.py",
                "scripts/fw.py",
                processor="raw_file",
                status="raw_archived",
            ),
        ],
        chunks={"f1": [_chunk("unrelated")]},
    )
    outcome = search_documents(
        backend, DocumentSearchRequest(query="fw.py", top_k=5)
    )
    assert outcome.mode == "exact"
    assert outcome.raw_file_matches[0]["file_id"] == "r1"
    assert outcome.raw_file_matches[0]["matched_by"] == "filename"

    with pytest.raises(ValueError, match="auto, exact, hybrid"):
        search_documents(
            backend, DocumentSearchRequest(query="x", mode="semantic")
        )
