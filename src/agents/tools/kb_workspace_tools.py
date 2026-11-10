"""File-shaped KB tools: glob, grep and read over the parsed KB text.

The chunk source is the same ``DocumentManager`` used by the public
``/kbs/{kb}/files/{id}/chunks`` endpoint (fallback: the runner's rag backend),
so grep hits here match what the MCP bridge exposes to external agents.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from typing import Any

from src.agents.tools.grep_tools import _scoped_documents

_MAX_READ_CHARS = 20_000
_MAX_GREP_HITS = 50
_PARSE_DEADLINE_SECONDS = 20.0


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _doc_name(record: Any) -> str:
    return str(
        getattr(record, "original_file_name", "")
        or getattr(record, "document_name", "")
        or getattr(record, "id", "")
        or "未命名文档"
    )


def _doc_path(record: Any) -> str:
    name = _doc_name(record).replace("/", "_").replace("\\", "_")
    kb = str(getattr(record, "kb_name", "") or "kb")
    return f"/kb/{kb}/{name}"


def _match_path(record: Any, path: str) -> bool:
    needle = str(path or "").strip()
    if not needle:
        return True
    candidate = _doc_path(record)
    if needle.startswith("/"):
        return candidate == needle
    return needle in candidate or fnmatch.fnmatch(candidate, f"*{needle}*")


def _snippet(content: str, start: int, end: int, width: int = 90) -> str:
    left = max(0, start - width)
    right = min(len(content), end + width)
    text = content[left:right].replace("\n", " ")
    return ("…" if left > 0 else "") + text + ("…" if right < len(content) else "")


def make_kb_workspace_tools(
    rt: Any,
    rag_backend: Any,
    document_store: Any,
    document_manager: Any = None,
) -> list[Any]:
    """Return kb_glob / kb_grep / kb_read bound to the current KB scope."""

    chunk_backend = document_manager or rag_backend
    chunk_cache: dict[str, list[Any]] = {}

    def _documents() -> list[Any]:
        return [
            record
            for record in _scoped_documents(rt, document_store)
            if str(getattr(record, "status", "") or "") in {"parsed", "indexed"}
        ]

    def _chunks(record: Any) -> list[Any]:
        key = str(getattr(record, "id", "") or getattr(record, "document_id", ""))
        cached = chunk_cache.get(key)
        if cached is not None:
            return cached
        chunks: list[Any] = []
        try:
            result = chunk_backend.get_parse_result(
                str(getattr(record, "kb_name", "")),
                f"ragflow:{getattr(record, 'id', 0)}",
                ctx=getattr(rt, "ctx", None),
                deadline=time.monotonic() + _PARSE_DEADLINE_SECONDS,
                should_cancel=getattr(rt, "should_cancel", None),
            )
            chunks = list(getattr(result, "chunks", []) or [])
        except Exception:
            chunks = []
        chunk_cache[key] = chunks
        return chunks

    def _run(name: str, query: str, fn):
        if rt is None:
            return fn()
        call_id = rt.next_invocation_id()
        rt.tool_started(
            call_id=call_id,
            tool_name=name,
            tool_kind="retrieval",
            query=str(query or "")[:200],
        )
        try:
            result = fn()
        except Exception as exc:
            rt.tool_result(
                call_id=call_id,
                tool_name=name,
                status="failed",
                tool_kind="retrieval",
                empty_reason=str(exc)[:200],
            )
            raise
        rt.tool_result(
            call_id=call_id,
            tool_name=name,
            status="success",
            tool_kind="retrieval",
        )
        return result

    def kb_glob(pattern: str = "**/*", max_results: int = 100) -> str:
        """列出当前知识库中的文件；pattern 支持通配符，例如 **/*TCN2*。"""

        def _glob() -> str:
            raw = str(pattern or "**/*").strip() or "**/*"
            if not any(ch in raw for ch in "*?["):
                raw = f"*{raw}*"
            rows = []
            for record in _documents():
                path = _doc_path(record)
                name = _doc_name(record)
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
                if len(rows) >= max(1, min(int(max_results or 100), 200)):
                    break
            return _json({"pattern": raw, "files": rows, "count": len(rows)})

        return _run("kb_glob", pattern, _glob)

    def kb_grep(
        patterns: list[str],
        path: str = "",
        max_hits: int = 30,
        regex: bool = False,
    ) -> str:
        """在知识库解析文本中按文件/正则搜索，返回路径、chunk/page 和上下文。

        patterns 可一次传多个位号、型号、信号名或表号；path 可限定文件。
        这是只读的 grep，不登记正式证据；需要引用时再用 evidence_read。
        """

        def _grep() -> str:
            raw = patterns if isinstance(patterns, list) else [patterns]
            values = [str(item or "").strip() for item in raw if str(item or "").strip()]
            if not values:
                return _json({"error": "patterns is required", "hits": []})
            compiled: list[re.Pattern[str]] = []
            for value in values[:12]:
                try:
                    compiled.append(
                        re.compile(value if regex else re.escape(value), re.IGNORECASE)
                    )
                except re.error as exc:
                    return _json({"error": f"invalid regex: {value}: {exc}", "hits": []})
            limit = max(1, min(int(max_hits or 30), _MAX_GREP_HITS))
            hits: list[dict[str, Any]] = []
            documents = [record for record in _documents() if _match_path(record, path)]
            for record in documents:
                for chunk in _chunks(record):
                    content = str(getattr(chunk, "content", "") or "")
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
                    metadata = dict(getattr(chunk, "metadata", {}) or {})
                    locator = {
                        key: metadata[key]
                        for key in ("page_num", "page", "sheet_name", "row_index", "chunk_index")
                        if metadata.get(key) not in (None, "", [], {})
                    }
                    if not locator:
                        locator = {"chunk_index": int(getattr(chunk, "index", 0) or 0)}
                    hits.append(
                        {
                            "path": _doc_path(record),
                            "file_id": str(getattr(record, "id", "") or ""),
                            "name": _doc_name(record),
                            "locator": locator,
                            "line": int(getattr(chunk, "index", 0) or 0),
                            "snippet": _snippet(content, first.start(), first.end()),
                            "matches": len(matches),
                        }
                    )
                    if len(hits) >= limit:
                        break
                if len(hits) >= limit:
                    break
            note_progress = getattr(rt, "note_progress", None)
            note_no_progress = getattr(rt, "note_no_progress", None)
            if hits:
                if callable(note_progress):
                    note_progress()
            elif callable(note_no_progress):
                note_no_progress()
            return _json({"patterns": values, "path": path, "hits": hits, "count": len(hits)})

        return _run("kb_grep", f"{path} | {' | '.join(str(p) for p in (patterns or []))}", _grep)

    def kb_read(path_or_id: str, start: int = 0, limit: int = 12_000) -> str:
        """读取知识库文件的解析文本；start/limit 是字符偏移和长度。"""

        def _read() -> str:
            raw = str(path_or_id or "").strip()
            if not raw:
                return _json({"error": "path is required"})
            record = next(
                (
                    item
                    for item in _documents()
                    if _match_path(item, raw)
                    or str(getattr(item, "id", "")) == raw
                ),
                None,
            )
            if record is None:
                return _json({"error": f"file not found: {raw}"})
            text = "\n\n".join(
                str(getattr(chunk, "content", "") or "")
                for chunk in _chunks(record)
                if str(getattr(chunk, "content", "") or "")
            )
            offset = max(0, int(start or 0))
            take = max(1, min(int(limit or 12_000), _MAX_READ_CHARS))
            body = text[offset : offset + take]
            next_offset = offset + len(body)
            if next_offset >= len(text):
                next_offset = None
            if body and callable(getattr(rt, "note_progress", None)):
                rt.note_progress()
            return _json(
                {
                    "path": _doc_path(record),
                    "file_id": str(getattr(record, "id", "") or ""),
                    "name": _doc_name(record),
                    "start": offset,
                    "next": next_offset,
                    "total_chars": len(text),
                    "text": body,
                }
            )

        return _run("kb_read", f"{path_or_id} @{start}", _read)

    kb_glob.__name__ = "kb_glob"
    kb_grep.__name__ = "kb_grep"
    kb_read.__name__ = "kb_read"
    return [kb_glob, kb_grep, kb_read]


__all__ = ["make_kb_workspace_tools"]
