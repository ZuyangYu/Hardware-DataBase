"""Shared lexical/grep primitives for the HDB knowledge tools.

Both the external MCP bridge and the in-process agent use these helpers, so a
query returns the same chunks, snippets and locators regardless of which
adapter the caller uses.  Transport, auth and chunk loading stay in the
adapters; only the retrieval contract lives here.
"""

from __future__ import annotations

import fnmatch
import math
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


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def _corpus_idf(
    chunks_by_file: dict[tuple[str, str], list[dict[str, Any]]],
    tokens: list[str],
) -> dict[str, float]:
    """Smoothed IDF so rare domain terms outrank repeated generic words."""

    contents = [
        str(chunk.get("content") or "").casefold()
        for chunks in chunks_by_file.values()
        for chunk in chunks
    ]
    total = max(1, len(contents))
    return {
        token: math.log((total + 1) / (sum(1 for text in contents if token in text) + 1)) + 1.0
        for token in tokens
    }


def _weighted_chunk_score(
    content: str,
    tokens: list[str],
    idf: dict[str, float],
    *,
    relative_path: str = "",
) -> float:
    """IDF + length-normalised term score with title/path boosts."""

    lowered = content.casefold()
    length = max(1, len(lowered))
    score = 0.0
    for token in tokens:
        hits = lowered.count(token)
        if hits <= 0:
            continue
        score += (1.0 + math.log(hits)) * idf.get(token, 1.0)
        if token.isascii() and len(token) >= 4:
            score += 0.3 * idf.get(token, 1.0)
    # Long generic documents must not win just because they repeat words.
    score /= 1.0 + math.log(max(1.0, length / 500.0))
    heading = lowered[:240]
    path = str(relative_path or "").casefold()
    for token in tokens:
        if token in heading:
            score += 0.6 * idf.get(token, 1.0)
        if token in path:
            score += 0.4 * idf.get(token, 1.0)
    return round(score, 6)


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
    """Rank chunks by IDF-weighted lexical overlap; same shape as MCP search."""
    tokens = tokenize_query(query)
    if not tokens:
        return []
    idf = _corpus_idf(chunks_by_file, tokens)
    results: list[dict[str, Any]] = []
    for (fid, fname), chunks in chunks_by_file.items():
        if file_id and fid != str(file_id) and fname != str(file_id):
            continue
        for chunk in chunks:
            content = str(chunk.get("content") or "")
            score = _weighted_chunk_score(
                content, tokens, idf, relative_path=fname
            )
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


def _row_file_key(row: dict[str, Any]) -> str:
    return str(row.get("file_id") or row.get("file_name") or "")


def _best_rows_by_file(
    rows: list[dict[str, Any]],
) -> dict[str, tuple[int, dict[str, Any]]]:
    """Keep the best-ranked chunk per file for score fusion."""

    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for rank, row in enumerate(rows, 1):
        key = _row_file_key(row)
        if not key or key in best:
            continue
        best[key] = (rank, row)
    return best


def fuse_results(
    semantic_rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
    *,
    query: str = "",
    top_k: int = 8,
    lexical_weight: float | None = None,
) -> list[dict[str, Any]]:
    """Fuse semantic and lexical hits at file level.

    Chunk-level lists are first collapsed to one best chunk per file, so a
    generic document cannot occupy several top slots.  Each channel is
    max-normalised before weighting: semantic similarities and lexical
    scores live on completely different scales, so raw-score addition or pure
    rank fusion lets a mediocre semantic hit beat a much stronger lexical
    match.  Chinese technical queries lean lexical because the local corpus is
    small and highly overlapping; other queries keep semantic as the stronger
    channel.
    """

    if lexical_weight is None:
        lexical_weight = 0.6 if _contains_cjk(query) else 0.4
    lexical_weight = max(0.0, min(1.0, float(lexical_weight)))
    semantic_weight = 1.0 - lexical_weight
    semantic_best = _best_rows_by_file(semantic_rows)
    lexical_best = _best_rows_by_file(lexical_rows)
    semantic_max = max(
        (float(row.get("score") or 0.0) for _, row in semantic_best.values()),
        default=0.0,
    )
    lexical_max = max(
        (float(row.get("score") or 0.0) for _, row in lexical_best.values()),
        default=0.0,
    )
    fused: list[tuple[float, dict[str, Any]]] = []
    for key in set(semantic_best) | set(lexical_best):
        semantic_rank, semantic_row = semantic_best.get(key, (0, {}))
        lexical_rank, lexical_row = lexical_best.get(key, (0, {}))
        semantic_score = float(semantic_row.get("score") or 0.0)
        lexical_score = float(lexical_row.get("score") or 0.0)
        score = 0.0
        if semantic_max > 0:
            score += semantic_weight * (semantic_score / semantic_max)
        if lexical_max > 0:
            score += lexical_weight * (lexical_score / lexical_max)
        row = dict(semantic_row or lexical_row)
        row["score"] = round(score, 6)
        row["semantic_rank"] = semantic_rank or None
        row["lexical_rank"] = lexical_rank or None
        row["semantic_score"] = semantic_score
        row["lexical_score"] = lexical_score
        fused.append((score, row))
    fused.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in fused[: max(1, min(int(top_k or 8), 200))]]


_RAW_PROCESSORS = {"raw", "raw_file"}
_RAW_STATUSES = {"raw", "raw_archived"}


AUTO_SEMANTIC_MARGIN = 0.15
AUTO_SEMANTIC_MIN_SCORE = 1.0


def lexical_margin(rows: list[dict[str, Any]]) -> float:
    """File-level Top1/Top2 lexical score margin in ``[0, 1]``."""

    best = _best_rows_by_file(rows)
    scores = sorted(
        (float(row.get("score") or 0.0) for _, row in best.values()),
        reverse=True,
    )
    if not scores:
        return 0.0
    if len(scores) == 1:
        return 1.0
    top1, top2 = scores[0], scores[1]
    if top1 <= 0:
        return 0.0
    return round((top1 - top2) / top1, 6)


def should_use_semantic(
    rows: list[dict[str, Any]],
    *,
    margin: float = AUTO_SEMANTIC_MARGIN,
    min_score: float = AUTO_SEMANTIC_MIN_SCORE,
) -> bool:
    """Decide whether ``auto`` should escalate to semantic retrieval.

    Lexical retrieval is much cheaper; only ambiguous or low-confidence
    results justify paying the RAGFlow round-trip.
    """

    if not rows:
        return True
    best = _best_rows_by_file(rows)
    top_score = max(
        (float(row.get("score") or 0.0) for _, row in best.values()),
        default=0.0,
    )
    if top_score < min_score:
        return True
    return lexical_margin(rows) < margin


def raw_file_matches(
    rows: list[dict[str, Any]],
    query: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find unparsed/raw files by filename or relative path.

    This is a discovery fallback, not content search: raw files have no
    chunks.  Callers should hand the returned ``file_id`` to their original
    file download/read tool.
    """

    tokens = tokenize_query(query)
    if not tokens:
        return []
    matches: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        processor = str(row.get("processor_kind") or row.get("kind") or "").casefold()
        status = str(row.get("status") or "").casefold()
        if processor not in _RAW_PROCESSORS and status not in _RAW_STATUSES:
            continue
        name = str(row.get("name") or row.get("file_name") or "")
        relative_path = str(row.get("relative_path") or name)
        haystack = f"{name} {relative_path}".casefold()
        hits = [token for token in tokens if token in haystack]
        if not hits:
            continue
        score = len(hits) / max(1, len(tokens))
        matches.append(
            (
                score,
                {
                    "file_id": str(row.get("file_id") or row.get("id") or ""),
                    "file_name": name,
                    "relative_path": relative_path,
                    "directory_id": int(row.get("directory_id") or 0),
                    "status": status,
                    "processor_kind": processor,
                    "matched_by": "filename",
                    "score": round(score, 4),
                },
            )
        )
    matches.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in matches[: max(1, int(limit or 5))]]


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


def list_files(
    records: list[Any],
    pattern: str = "**/*",
    *,
    path_for: Any = None,
) -> list[dict[str, Any]]:
    """List KB records using virtual ``/kb/<name>/<relative path>`` paths.

    ``path_for`` is an optional callback that resolves a record's
    directory-aware relative path.  Without it, the legacy filename-only path
    is kept for callers that predate the directory tree.
    """

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
        relative_path = ""
        if callable(path_for):
            try:
                relative_path = str(path_for(record) or "")
            except Exception:
                relative_path = ""
        relative_path = relative_path or name
        kb = str(getattr(record, "kb_name", "") or "kb")
        safe_relative = relative_path.replace("/", "_").replace(chr(92), "_")
        path = f"/kb/{kb}/{safe_relative}"
        if not (
            fnmatch.fnmatch(name.casefold(), raw.casefold())
            or fnmatch.fnmatch(relative_path.casefold(), raw.casefold())
            or fnmatch.fnmatch(path.casefold(), raw.casefold())
        ):
            continue
        rows.append(
            {
                "path": path,
                "file_id": str(getattr(record, "id", "") or ""),
                "name": name,
                "relative_path": relative_path,
                "directory_id": int(getattr(record, "directory_id", 0) or 0),
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
    "fuse_results",
    "lexical_margin",
    "should_use_semantic",
    "raw_file_matches",
    "grep_chunks",
    "read_chunks",
    "list_files",
]
