"""Shared lexical/grep primitives for the HDB knowledge tools.

Both the external MCP bridge and the in-process agent use these helpers, so a
query returns the same chunks, snippets and locators regardless of which
adapter the caller uses.  Transport, auth and chunk loading stay in the
adapters; only the retrieval contract lives here.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

_MAX_TEXT = 20_000
_MAX_GREP_HITS = 100


def clip(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


def tokenize_query(text: str) -> list[str]:
    """ASCII words (>=2 chars) + CJK bigrams for lexical chunk scoring."""
    tokens: list[str] = []
    lowered = str(text or "").strip().casefold()
    if not lowered:
        return tokens
    for word in re.findall(r"[a-z0-9_./\-]{2,}", lowered):
        tokens.append(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) == 1:
            tokens.append(run)
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
    return list(dict.fromkeys(tokens))


def score_chunk(content: str, tokens: list[str]) -> float:
    lowered = content.casefold()
    score = 0.0
    for token in tokens:
        hits = lowered.count(token)
        if hits <= 0:
            continue
        score += 1.0 + min(hits, 8) * 0.25
        if token.isascii() and token.islower() and len(token) >= 4:
            score += 0.5  # long ascii token is a strong identity signal
    return round(score, 3)


def snippet_around(content: str, tokens: list[str], width: int = 260) -> str:
    lowered = content.casefold()
    pos = min(
        (lowered.find(token) for token in tokens if lowered.find(token) >= 0),
        default=0,
    )
    start = max(0, pos - width // 3)
    snippet = content[start : start + width]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + width < len(content) else ""
    return f"{prefix}{snippet}{suffix}"


_EXACT_TERM_RE = re.compile(
    r"^(?:[A-Za-z]{1,4}\d{2,}[A-Za-z0-9_.-]*|[A-Za-z0-9_]+(?:[-_./][A-Za-z0-9_]+)+|\d{2,})$"
)


def looks_like_exact_term(query: str) -> bool:
    """Heuristic: refdes, part number, net name or a numeric id."""
    text = str(query or "").strip()
    if not text or len(text) > 120:
        return False
    if " " in text:
        return False
    return bool(_EXACT_TERM_RE.match(text))


def _chunk_row(
    file_id: str,
    file_name: str,
    chunk: dict[str, Any],
    *,
    score: float = 0.0,
    snippet: str = "",
) -> dict[str, Any]:
    metadata = dict(chunk.get("metadata") or {})
    locator = {
        key: metadata[key]
        for key in ("page_num", "page", "sheet_name", "row_index", "chunk_index")
        if metadata.get(key) not in (None, "", [], {})
    }
    if not locator:
        locator = {"chunk_index": int(chunk.get("index") or 0)}
    return {
        "file_id": str(file_id),
        "file_name": str(file_name),
        "chunk_index": int(chunk.get("index") or 0),
        "rag_id": str(chunk.get("rag_id") or metadata.get("ragflow_chunk_id") or ""),
        "locator": locator,
        "score": score,
        "snippet": snippet,
    }


def lexical_search(
    chunks_by_file: dict[tuple[str, str], list[dict[str, Any]]],
    query: str,
    *,
    top_k: int = 8,
    file_id: str = "",
) -> list[dict[str, Any]]:
    """Rank chunks by lexical token overlap; same shape as the MCP search."""
    tokens = tokenize_query(query)
    if not tokens:
        return []
    results: list[dict[str, Any]] = []
    for (fid, fname), chunks in chunks_by_file.items():
        if file_id and fid != str(file_id) and fname != str(file_id):
            continue
        for chunk in chunks:
            content = str(chunk.get("content") or "")
            score = score_chunk(content, tokens)
            if score <= 0:
                continue
            results.append(
                _chunk_row(
                    fid,
                    fname,
                    chunk,
                    score=score,
                    snippet=snippet_around(content, tokens),
                )
            )
    results.sort(key=lambda row: row.get("score") or 0.0, reverse=True)
    return results[: max(1, min(int(top_k or 8), 200))]


def grep_chunks(
    chunks_by_file: dict[tuple[str, str], list[dict[str, Any]]],
    patterns: list[str] | str,
    *,
    path: str = "",
    max_hits: int = 30,
    regex: bool = False,
) -> list[dict[str, Any]]:
    """Regex/literal grep over parsed chunks; returns path + locator + snippet."""
    raw = patterns if isinstance(patterns, list) else [patterns]
    values = [str(item or "").strip() for item in raw if str(item or "").strip()]
    if not values:
        return []
    compiled: list[re.Pattern[str]] = []
    for value in values[:12]:
        compiled.append(re.compile(value if regex else re.escape(value), re.IGNORECASE))
    limit = max(1, min(int(max_hits or 30), _MAX_GREP_HITS))
    hits: list[dict[str, Any]] = []
    for (fid, fname), chunks in chunks_by_file.items():
        path_key = f"/kb/{fname}"
        if path and not (
            path in fname
            or fnmatch.fnmatch(fname.casefold(), f"*{path.casefold()}*")
            or path in path_key
        ):
            continue
        for chunk in chunks:
            content = str(chunk.get("content") or "")
            if not content:
                continue
            matches = [
                match
                for pattern in compiled
                for match in pattern.finditer(content)
            ]
            if not matches:
                continue
            first = min(matches, key=lambda item: item.start())
            row = _chunk_row(
                fid,
                fname,
                chunk,
                score=float(len(matches)),
                snippet=clip(
                    content[
                        max(0, first.start() - 90) : min(
                            len(content), first.end() + 90
                        )
                    ].replace("\n", " "),
                    320,
                ),
            )
            row["matches"] = len(matches)
            hits.append(row)
            if len(hits) >= limit:
                return hits
    return hits


def read_chunks(
    chunks: list[dict[str, Any]],
    *,
    start_index: int = 0,
    max_chunks: int = 20,
    query: str = "",
) -> list[dict[str, Any]]:
    """Slice/read parsed chunks, optionally filtering by a needle."""
    start = max(0, int(start_index or 0))
    needle = str(query or "").strip().casefold()
    limit = max(1, min(int(max_chunks or 20), 100))
    picked: list[dict[str, Any]] = []
    for chunk in chunks:
        index = int(chunk.get("index") or 0)
        if index < start:
            continue
        content = str(chunk.get("content") or "")
        if needle and needle not in content.casefold():
            continue
        picked.append(
            {
                "index": chunk.get("index"),
                "content": clip(content, 4_000),
                "metadata": chunk.get("metadata") or {},
                "rag_id": str(chunk.get("rag_id") or ""),
            }
        )
        if len(picked) >= limit:
            break
    return picked


def list_files(records: list[Any], pattern: str = "**/*") -> list[dict[str, Any]]:
    """List KB records using virtual ``/kb/<name>/<file>`` paths."""
    raw = str(pattern or "**/*").strip() or "**/*"
    if not any(ch in raw for ch in "*?["):
        raw = f"*{raw}*"
    rows: list[dict[str, Any]] = []
    for record in records:
        name = str(
            getattr(record, "original_file_name", "")
            or getattr(record, "document_name", "")
            or getattr(record, "id", "")
            or "未命名文档"
        )
        kb = str(getattr(record, "kb_name", "") or "kb")
        path = f"/kb/{kb}/{name.replace('/', '_').replace(chr(92), '_')}"
        if not (
            fnmatch.fnmatch(name.casefold(), raw.casefold())
            or fnmatch.fnmatch(path.casefold(), raw.casefold())
        ):
            continue
        rows.append(
            {
                "path": path,
                "file_id": str(getattr(record, "id", "") or ""),
                "name": name,
                "kind": str(getattr(record, "processor_kind", "") or ""),
                "status": str(getattr(record, "status", "") or ""),
            }
        )
        if len(rows) >= 200:
            break
    return rows


__all__ = [
    "clip",
    "tokenize_query",
    "score_chunk",
    "snippet_around",
    "looks_like_exact_term",
    "lexical_search",
    "grep_chunks",
    "read_chunks",
    "list_files",
]
