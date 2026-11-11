"""Shared document-search orchestration for MCP and in-process agents.

Both adapters provide scoped file rows, chunk loading and semantic retrieval;
this module owns the policy that must not drift between them:

* supported modes and exact-term detection
* fast lexical routing with a confidence gate
* semantic + lexical file-level fusion
* raw-file filename fallback
* structured-file skip reporting

Adapters stay thin: they format the outcome for MCP or for the agent tool
payload, but they do not re-implement ranking or mode selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from src.services.knowledge_retrieval import (
    fuse_results,
    grep_chunks,
    lexical_search,
    looks_like_exact_term,
    raw_file_matches,
    should_use_semantic,
)

SUPPORTED_MODES = ("auto", "exact", "hybrid")
SUPPORTED_KINDS = ("document", "table", "circuit", "all")

_STRUCTURED_REASON = "use query_table/query_circuit for structured sources"
_RAW_REASON = "raw file has no parsed chunks; use download_kb_file(include_text=true)"


class DocumentSearchBackend(Protocol):
    """Adapter surface consumed by :func:`search_documents`."""

    def list_files(self) -> list[dict[str, Any]]:
        """Return file rows already filtered to the caller's KB/directory scope."""

    def load_chunks(self, file_id: str) -> list[dict[str, Any]]:
        """Return parsed chunks for one file (empty for non-document rows)."""

    def semantic_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """Return semantic chunk rows in the shared result shape."""


@dataclass(frozen=True)
class DocumentSearchRequest:
    query: str
    top_k: int = 8
    mode: str = "auto"
    kind: str = "document"
    file_id: str = ""


@dataclass
class DocumentSearchOutcome:
    mode: str
    route: str
    results: list[dict[str, Any]] = field(default_factory=list)
    raw_file_matches: list[dict[str, Any]] = field(default_factory=list)
    files_searched: int = 0
    chunks_searched: int = 0
    skipped_files: list[dict[str, str]] = field(default_factory=list)
    semantic_results: int = 0
    lexical_results: int = 0
    semantic_error: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "route": self.route,
            "results": self.results,
            "result_count": len(self.results),
            "raw_file_matches": self.raw_file_matches,
            "files_searched": self.files_searched,
            "chunks_searched": self.chunks_searched,
            "skipped_files": self.skipped_files,
            "semantic_results": self.semantic_results,
            "lexical_results": self.lexical_results,
            "semantic_error": self.semantic_error,
            "note": self.note,
        }


def _row_file_id(row: dict[str, Any]) -> str:
    return str(row.get("file_id") or row.get("id") or "")


def _row_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("file_name") or "")


def _row_relative_path(row: dict[str, Any]) -> str:
    return str(row.get("relative_path") or _row_name(row))


def _matches_file_id(row: dict[str, Any], file_id: str) -> bool:
    if not file_id:
        return True
    wanted = str(file_id)
    return (
        _row_file_id(row) == wanted
        or _row_name(row) == wanted
        or _row_relative_path(row) == wanted
    )


def _structured_skip(row: dict[str, Any]) -> dict[str, str]:
    processor = str(row.get("processor_kind") or row.get("kind") or "")
    return {
        "file_id": _row_file_id(row),
        "file_name": _row_relative_path(row),
        "kind": processor,
        "reason": _RAW_REASON if processor in {"raw", "raw_file"} else _STRUCTURED_REASON,
    }


def search_documents(
    backend: DocumentSearchBackend,
    request: DocumentSearchRequest,
) -> DocumentSearchOutcome:
    """Run one document search with the shared MCP/agent policy."""

    needle = str(request.query or "").strip()
    if not needle:
        raise ValueError("query is required")
    mode = str(request.mode or "auto").strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ValueError("mode must be one of: auto, exact, hybrid")
    kind = str(request.kind or "document").strip().lower()
    if kind not in SUPPORTED_KINDS:
        kind = "document"
    top_k = max(1, min(int(request.top_k or 8), 50))
    file_id = str(request.file_id or "").strip()

    rows = [
        row
        for row in backend.list_files()
        if isinstance(row, dict) and _matches_file_id(row, file_id)
    ]

    if kind in {"table", "circuit"}:
        return DocumentSearchOutcome(
            mode=kind,
            route=kind,
            skipped_files=[
                _structured_skip(row)
                for row in rows
                if _kind_matches(row, kind)
            ],
            note=f"kind={kind} is not a document chunk stream",
        )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    skipped: list[dict[str, str]] = []
    files_searched = 0
    chunks_searched = 0
    for row in rows:
        processor = str(row.get("processor_kind") or row.get("kind") or "")
        if kind == "document" and processor and processor != "ragflow":
            continue
        if kind == "all" and processor and processor != "ragflow":
            skipped.append(_structured_skip(row))
            continue
        fid = _row_file_id(row)
        if not fid:
            continue
        try:
            chunks = list(backend.load_chunks(fid) or [])
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill search
            skipped.append(
                {
                    "file_id": fid,
                    "file_name": _row_relative_path(row),
                    "kind": processor,
                    "reason": str(exc)[:200],
                }
            )
            continue
        grouped[(fid, _row_relative_path(row))] = chunks
        files_searched += 1
        chunks_searched += len(chunks)

    exact = mode == "exact" or (
        mode == "auto" and looks_like_exact_term(needle)
    )
    if exact:
        results = grep_chunks(grouped, [needle], max_hits=top_k)
        return DocumentSearchOutcome(
            mode="exact",
            route="exact",
            results=results,
            raw_file_matches=raw_file_matches(rows, needle, limit=5),
            files_searched=files_searched,
            chunks_searched=chunks_searched,
            skipped_files=skipped,
            note="literal grep over parsed document chunks",
        )

    candidate_k = min(50, max(top_k * 3, 12))
    lexical = lexical_search(
        grouped, needle, top_k=candidate_k, file_id=file_id
    )
    semantic_rows: list[dict[str, Any]] = []
    semantic_error = ""
    if mode == "auto" and not should_use_semantic(lexical):
        used = "auto"
        route = "lexical"
        results = lexical
    else:
        used = "auto" if mode == "auto" else "hybrid"
        route = "hybrid"
        try:
            semantic_rows = list(backend.semantic_search(needle, candidate_k) or [])
        except Exception as exc:  # noqa: BLE001 - auto falls back to lexical
            semantic_error = str(exc)[:300]
        results = fuse_results(
            semantic_rows, lexical, query=needle, top_k=top_k
        )
    return DocumentSearchOutcome(
        mode=used,
        route=route,
        results=results,
        raw_file_matches=raw_file_matches(rows, needle, limit=5),
        files_searched=files_searched,
        chunks_searched=chunks_searched,
        skipped_files=skipped,
        semantic_results=len(semantic_rows),
        lexical_results=len(lexical),
        semantic_error=semantic_error,
        note=(
            "auto uses fast lexical routing and escalates to semantic fusion "
            "only when lexical confidence is low; hybrid always fuses"
        ),
    )


def _kind_matches(row: dict[str, Any], kind: str) -> bool:
    processor = str(row.get("processor_kind") or row.get("kind") or "")
    if kind == "table":
        return "spreadsheet" in processor
    if kind == "circuit":
        return "circuit" in processor
    return True


__all__ = [
    "DocumentSearchBackend",
    "DocumentSearchOutcome",
    "DocumentSearchRequest",
    "SUPPORTED_KINDS",
    "SUPPORTED_MODES",
    "search_documents",
]
