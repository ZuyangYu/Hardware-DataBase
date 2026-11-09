"""HDB MCP bridge.

Exposes the platform to external agents (dcode/OpenCode/Claude Code) over
MCP.  The tools are thin HTTP clients so the bridge never bypasses the
platform's auth, knowledge-scope and turn governance.

Run over stdio:

    HDB_API_BASE=http://127.0.0.1:8002/api/v1 \
    HDB_API_TOKEN=... \
    uv run python -m src.mcp_bridge.server

Tools:
- list_kbs / list_kb_files / read_file_chunks
- search_knowledge_base (stateless chunk hits with locators; no session)
- list_spreadsheets / list_circuit_designs / query_circuit
- create_drawio_file (compact block/edge spec -> native .drawio XML)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import os
import re
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hdb-knowledge")

_API_BASE = os.getenv("HDB_API_BASE", "http://127.0.0.1:8002/api/v1").rstrip("/")
_TOKEN = os.getenv("HDB_API_TOKEN", "").strip()
_API_KEY = os.getenv("HDB_API_KEY", "").strip()
_MCP_TOKEN = os.getenv("HDB_MCP_TOKEN", "").strip()
_USERNAME = os.getenv("HDB_USERNAME", "").strip()
_PASSWORD = os.getenv("HDB_PASSWORD", "")

_MAX_TOOL_TEXT = 20_000
_MAX_ROWS = 50
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_LOGIN_USER: dict[str, Any] = {}
_DRAWIO_EDGE_STYLE = (
    "edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;"
    "jettySize=auto;html=1;endArrow=block;endFill=1;"
    "jumpStyle=arc;jumpSize=10;"
)
_DRAWIO_BLOCK_STYLE = (
    "rounded=0;fillColor=#DAE8FC;strokeColor=#6C8EBF;"
    "fontSize=13;whiteSpace=wrap;"
)
_DRAWIO_ZONE_STYLE = (
    "rounded=0;fillColor=#F5F5F5;strokeColor=#666666;"
    "verticalAlign=top;fontSize=14;fontStyle=1;whiteSpace=wrap;"
)
_DRAWIO_TEXT_STYLE = (
    "text;html=1;align=center;verticalAlign=middle;"
    "fontSize=25;fontColor=#000000;whiteSpace=wrap;"
)

_http: httpx.Client | None = None


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
    return _http


def _token() -> str:
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    if _MCP_TOKEN:
        response = _client().post(
            f"{_API_BASE}/mcp/session",
            headers={"X-HDB-MCP-Token": _MCP_TOKEN},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"MCP session exchange failed: {response.status_code} {response.text[:200]}"
            )
        payload = response.json()
        _TOKEN = str(payload["token"])
        user = payload.get("user")
        if isinstance(user, dict):
            _LOGIN_USER.update(user)
        return _TOKEN
    if _USERNAME and _PASSWORD:
        response = _client().post(
            f"{_API_BASE}/login",
            json={"username": _USERNAME, "password": _PASSWORD},
        )
        response.raise_for_status()
        payload = response.json()
        _TOKEN = str(payload["token"])
        user = payload.get("user")
        if isinstance(user, dict):
            _LOGIN_USER.update(user)
        return _TOKEN
    raise RuntimeError(
        "HDB_API_TOKEN or HDB_USERNAME/HDB_PASSWORD is required for the MCP bridge"
    )


def _headers() -> dict[str, str]:
    if _MCP_TOKEN:
        return {"Authorization": f"Bearer {_token()}"}
    if _API_KEY:
        return {"X-HDB-API-Key": _API_KEY}
    return {"Authorization": f"Bearer {_token()}"}


def _request(method: str, path: str, **kwargs: Any) -> Any:
    global _TOKEN
    response = _client().request(
        method, f"{_API_BASE}{path}", headers=_headers(), **kwargs
    )
    if response.status_code == 401 and _MCP_TOKEN:
        # The short-lived exchange token may have expired; refresh once.
        _TOKEN = ""
        response = _client().request(
            method, f"{_API_BASE}{path}", headers=_headers(), **kwargs
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"HDB API {response.status_code}: {response.text[:300]}"
        )
    return response.json() if response.content else {}


def _kb_path(kb_name: str) -> str:
    return quote(str(kb_name or ""), safe="")


def _clip(value: Any, limit: int = _MAX_TOOL_TEXT) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


def _visible_kb(kb_name: str) -> dict[str, Any]:
    name = str(kb_name or "").strip()
    for row in _request("GET", "/kbs"):
        if isinstance(row, dict) and row.get("name") == name:
            if not row.get("permission"):
                raise RuntimeError(f"no permission for knowledge base: {name}")
            return row
    raise RuntimeError(f"knowledge base not visible to this account: {name}")


def _department_id(kb: dict[str, Any]) -> str:
    return str(kb.get("department_id") or _LOGIN_USER.get("department_id") or "")


@mcp.tool()
def list_kbs() -> list[dict[str, Any]]:
    """List knowledge bases visible to the configured account."""
    return _request("GET", "/kbs")


@mcp.tool()
def list_kb_files(kb_name: str) -> list[dict[str, Any]]:
    """List source files in one knowledge base."""
    rows = _request("GET", f"/kbs/{_kb_path(kb_name)}/files")
    return [
        {
            "id": row.get("id", ""),
            "name": row.get("name", ""),
            "status": row.get("status", ""),
            "processor_kind": row.get("processor_kind", ""),
            "dataset_kind": row.get("dataset_kind", ""),
        }
        for row in rows
        if isinstance(row, dict)
    ]


@mcp.tool()
def read_file_chunks(
    kb_name: str,
    file_id: str,
    max_chunks: int = 20,
    start_index: int = 0,
    query: str = "",
) -> dict[str, Any]:
    """Read parsed chunks for one source file (chunk -> document spans).

    ``start_index`` positions at a chunk index (all chunks are served by the
    API; this slice is bridge-side).  ``query`` filters chunks whose content
    contains the needle (case-insensitive) after slicing.
    """
    data = _request(
        "GET",
        f"/kbs/{_kb_path(kb_name)}/files/{quote(str(file_id), safe='')}/chunks",
    )
    raw_chunks = data.get("chunks") or []
    chunk_count = int(data.get("chunk_count") or len(raw_chunks))
    start = max(0, int(start_index or 0))
    needle = str(query or "").strip().casefold()
    limit = max(1, min(int(max_chunks or 20), 100))
    picked = []
    for chunk in raw_chunks:
        if not isinstance(chunk, dict):
            continue
        index = int(chunk.get("index") or 0)
        if index < start:
            continue
        content = str(chunk.get("content") or "")
        if needle and needle not in content.casefold():
            continue
        picked.append(
            {
                "index": chunk.get("index"),
                "content": _clip(content, 4_000),
                "metadata": chunk.get("metadata") or {},
            }
        )
        if len(picked) >= limit:
            break
    return {
        "document_id": data.get("document_id", ""),
        "file_name": data.get("file_name", ""),
        "chunk_count": chunk_count,
        "backend": data.get("backend", ""),
        "start_index": start,
        "query": needle,
        "chunks": picked,
    }


_CHUNK_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_CHUNK_CACHE_TTL = 900.0


def _tokenize_query(text: str) -> list[str]:
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


def _snippet_around(content: str, tokens: list[str], width: int = 260) -> str:
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


def _score_chunk(content: str, tokens: list[str]) -> float:
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


def _load_chunks(kb_name: str, file_id: str, refresh: bool) -> dict[str, Any]:
    key = (str(kb_name), str(file_id))
    now = time.monotonic()
    cache = _CHUNK_CACHE.get(key)
    if cache and not refresh and now - float(cache.get("loaded") or 0.0) < _CHUNK_CACHE_TTL:
        return cache
    data = _request(
        "GET",
        f"/kbs/{_kb_path(kb_name)}/files/{quote(str(file_id), safe='')}/chunks",
    )
    chunks = [
        {
            "index": int(row.get("index") or 0),
            "content": _clip(row.get("content"), 4_000),
            "metadata": row.get("metadata") or {},
            "rag_id": row.get("rag_id") or (row.get("metadata") or {}).get("ragflow_chunk_id", ""),
        }
        for row in (data.get("chunks") or [])
        if isinstance(row, dict)
    ]
    fresh = {
        "loaded": now,
        "chunks": chunks,
        "total": int(data.get("chunk_count") or len(chunks)),
        "backend": data.get("backend", ""),
    }
    if len(_CHUNK_CACHE) > 40:
        oldest = sorted(_CHUNK_CACHE.items(), key=lambda item: item[1].get("loaded") or 0.0)[:8]
        for stale_key, _ in oldest:
            _CHUNK_CACHE.pop(stale_key, None)
    _CHUNK_CACHE[key] = fresh
    return fresh


@mcp.tool()
def search_knowledge_base(
    kb_name: str,
    query: str,
    top_k: int = 8,
    file_id: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    """Stateless lexical search over one KB's parsed chunks (no session).

    Returns chunk locators (file / index / rag_id) with snippet + score for
    the calling agent to reason over and verify with
    ``hdb_read_file_chunks``.  The chunk index is cached per file (~15 min
    TTL, ``refresh=True`` forces a rebuild); ``file_id`` narrows the search.
    Composed narrative answers live in the web /conversations/turns pipeline, not in this bridge's toolset.
    """
    needle = str(query or "").strip()
    if not needle:
        raise ValueError("query is required")
    rows = _request("GET", f"/kbs/{_kb_path(kb_name)}/files")
    tokens = _tokenize_query(needle)
    if not tokens:
        raise ValueError("query has no searchable tokens")
    results: list[dict[str, Any]] = []
    files_searched = 0
    chunks_searched = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("id") or "")
        fid_name = str(row.get("name") or "")
        if file_id and fid != file_id and fid_name != file_id:
            continue
        cache = _load_chunks(kb_name, fid, refresh)
        files_searched += 1
        for chunk in cache.get("chunks") or []:
            chunks_searched += 1
            content = str(chunk.get("content") or "")
            score = _score_chunk(content, tokens)
            if score <= 0:
                continue
            results.append(
                {
                    "file_id": fid,
                    "file_name": fid_name,
                    "chunk_index": chunk.get("index"),
                    "rag_id": chunk.get("rag_id") or "",
                    "score": score,
                    "snippet": _snippet_around(content, tokens),
                }
            )
    results.sort(key=lambda row: row.get("score") or 0.0, reverse=True)
    top_k_clamped = max(1, min(int(top_k or 8), _MAX_ROWS))
    return {
        "kb_name": kb_name,
        "query": needle,
        "results": results[:top_k_clamped],
        "result_count": len(results[:top_k_clamped]),
        "files_searched": files_searched,
        "chunks_searched": chunks_searched,
        "cache_files": len(_CHUNK_CACHE),
        "note": "lexical score over raw chunks; verify hits with hdb_read_file_chunks(file_id, start_index)",
    }


@mcp.tool()
def download_kb_file(
    kb_name: str,
    file_id: str,
    max_bytes: int = 20 * 1024 * 1024,
) -> dict[str, Any]:
    """Download one original source file as base64 (requires KB read access).

    Large files are rejected rather than silently truncated.  The caller is
    responsible for decoding ``content_base64`` and saving it locally.
    """
    kb = _visible_kb(kb_name)
    from src import settings as app_settings
    from src.api.context import build_context_for_user
    from src.core.app_pipeline import AppPipeline
    from src.core.auth import AuthService

    auth = AuthService()
    username = str(_LOGIN_USER.get("username") or _USERNAME or "").strip()
    user = auth.get_user_by_username(username) if username else None
    if user is None:
        # API-key deployments carry no session identity: fall back to the
        # knowledge base owner resolved from the KB registration.
        owner_id = kb.get("owner_user_id")
        try:
            user = auth.get_user_by_id(int(owner_id))
        except (TypeError, ValueError):
            user = None
    if user is None:
        raise RuntimeError(
            "cannot resolve the configured user for file download "
            "(HDB_USERNAME/HDB_PASSWORD per user, or a KB owner_user_id fallback)"
        )
    ctx = build_context_for_user(user, kb_name, auth=auth)
    infos = AppPipeline().list_file_infos(kb_name, ctx=ctx)
    info = next(
        (
            item
            for item in infos
            if str(getattr(item, "id", "")) == str(file_id)
            or str(getattr(item, "name", "")) == str(file_id)
        ),
        None,
    )
    if info is None:
        raise RuntimeError(f"file not found: {file_id}")
    local_path = str(getattr(info, "local_path", "") or "")
    if not local_path:
        raise RuntimeError(f"file has no local archive: {file_id}")
    department = _department_id(kb)
    base = os.path.abspath(
        os.path.join(
            str(app_settings.STORAGE_DIR),
            "pipeline_archives",
            "departments",
            department,
            "kbs",
            kb_name,
        )
    )
    path = local_path if os.path.isabs(local_path) else os.path.join(base, local_path)
    path = os.path.abspath(path)
    if path != base and base not in path:
        raise RuntimeError("file path escapes the knowledge-base archive")
    if not os.path.isfile(path):
        raise RuntimeError(f"file archive is missing: {file_id}")
    limit = max(1, min(int(max_bytes or 20 * 1024 * 1024), 100 * 1024 * 1024))
    size = os.path.getsize(path)
    if size > limit:
        raise ValueError(f"file is {size} bytes; max_bytes={limit}")
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"file exceeds max_bytes={limit}")
    return {
        "file_id": str(getattr(info, "id", file_id)),
        "filename": str(getattr(info, "name", file_id)),
        "size_bytes": len(data),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


@mcp.tool()
def list_tables(kb_name: str) -> dict[str, Any]:
    """List materialized SQL tables and their schemas for one knowledge base."""
    kb = _visible_kb(kb_name)
    from src.agents.tools.spreadsheet_tools import _load_sql_registry
    from src.services.spreadsheet_index_service import SpreadsheetIndexService

    db_path = SpreadsheetIndexService().db_path(
        _department_id(kb), kb_name, create=False
    )
    tables = _load_sql_registry(db_path)
    return {
        "kb_name": kb_name,
        "department_id": _department_id(kb),
        "table_count": len(tables),
        "tables": tables,
    }


@mcp.tool()
def query_table(kb_name: str, sql: str, limit: int = 200) -> dict[str, Any]:
    """Run one read-only SELECT against the materialized spreadsheet tables.

    The SQL is parsed with sqlglot, restricted to registered tables, capped at
    200 rows and executed against a read-only SQLite connection.
    """
    kb = _visible_kb(kb_name)
    from src.agents.tools.spreadsheet_tools import (
        _execute_readonly_sql,
        _load_sql_registry,
        _validate_readonly_sql,
    )
    from src.services.spreadsheet_index_service import SpreadsheetIndexService

    db_path = SpreadsheetIndexService().db_path(
        _department_id(kb), kb_name, create=False
    )
    registry = _load_sql_registry(db_path)
    allowed = {entry["table_name"]: entry for entry in registry}
    ast, error = _validate_readonly_sql(str(sql or ""), set(allowed))
    if error:
        return {
            "error": error,
            "tables": [
                {
                    "table_name": entry["table_name"],
                    "document_name": entry.get("document_name", ""),
                    "sheet_name": entry.get("sheet_name", ""),
                }
                for entry in registry
            ],
        }
    records, exec_error = _execute_readonly_sql(db_path, ast.sql(dialect="sqlite"))
    if exec_error:
        return {"error": exec_error}
    rows = records or []
    limit = max(1, min(int(limit or 200), 200))
    return {
        "kb_name": kb_name,
        "sql": ast.sql(dialect="sqlite"),
        "tables": sorted(allowed),
        "columns": list(rows[0].keys()) if rows else [],
        "row_count": len(rows),
        "rows": rows[:limit],
        "truncated": len(rows) > limit,
    }


@mcp.tool()
def list_assets(kb_name: str) -> list[dict[str, Any]]:
    """List reviewed hardware assets in one knowledge base."""
    return _request("GET", f"/kbs/{_kb_path(kb_name)}/assets")


@mcp.tool()
def get_asset(kb_name: str, asset_id: str) -> dict[str, Any]:
    """Read one hardware asset with its evidence."""
    return _request(
        "GET", f"/kbs/{_kb_path(kb_name)}/assets/{quote(str(asset_id), safe='')}"
    )


@mcp.tool()
def list_doc_assets(kb_name: str) -> list[dict[str, Any]]:
    """List document assets (reviewable document versions) in one knowledge base."""
    return _request("GET", f"/kbs/{_kb_path(kb_name)}/doc-assets")


@mcp.tool()
def get_doc_asset(kb_name: str, asset_id: str) -> dict[str, Any]:
    """Read one document asset."""
    return _request(
        "GET", f"/kbs/{_kb_path(kb_name)}/doc-assets/{quote(str(asset_id), safe='')}"
    )


@mcp.tool()
def upload_file(
    kb_name: str,
    filename: str,
    content_base64: str,
    source_group: str = "",
) -> dict[str, Any]:
    """Upload one file into a knowledge base (requires write permission).

    ``content_base64`` is the standard base64 encoding of the file bytes.  The
    platform applies the caller's write permission, audit and parsing pipeline;
    this is a real write, not a proposal.
    """
    if not str(filename or "").strip():
        raise ValueError("filename is required")
    try:
        data = base64.b64decode(str(content_base64 or ""), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("content_base64 is not valid base64") from exc
    if not data:
        raise ValueError("file content is empty")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ValueError("file content exceeds the 100 MiB bridge limit")
    files = {"files": (str(filename), data, "application/octet-stream")}
    form = {"source_group": source_group} if str(source_group or "").strip() else {}
    return _request(
        "POST", f"/kbs/{_kb_path(kb_name)}/files", files=files, data=form
    )


@mcp.tool()
def delete_file(kb_name: str, filename: str, confirm: bool = False) -> dict[str, Any]:
    """Delete one file from a knowledge base (requires write permission).

    Deletion is destructive.  Pass ``confirm=true`` only after the user has
    explicitly approved it; without confirmation this tool returns a dry-run
    response and performs no write.
    """
    name = str(filename or "").strip()
    if not name:
        raise ValueError("filename is required")
    if not confirm:
        return {
            "deleted": False,
            "dry_run": True,
            "filename": name,
            "message": "pass confirm=true to delete this file",
        }
    return _request(
        "DELETE", f"/kbs/{_kb_path(kb_name)}/files/{quote(name, safe='')}"
    )


@mcp.tool()
def list_spreadsheets(kb_name: str) -> dict[str, Any]:
    """List structured spreadsheet sources in one knowledge base."""
    return _request("GET", f"/kbs/{_kb_path(kb_name)}/structured/spreadsheets")


@mcp.tool()
def list_circuit_designs(kb_name: str) -> dict[str, Any]:
    """List circuit/netlist designs in one knowledge base."""
    return _request("GET", f"/kbs/{_kb_path(kb_name)}/structured/circuit-designs")


@mcp.tool()
def query_circuit(
    kb_name: str,
    query: str,
    design_id: str = "",
    limit: int = _MAX_ROWS,
    mode: str = "both",
    include_summary: bool = False,
    include_modules: bool = False,
) -> dict[str, Any]:
    """Search one circuit design by instance/refdes or net/signal name.

    ``mode`` selects the search plane: ``both`` (default), ``nets``, or
    ``instances``.  The bulky design summary and per-page module roll-up are
    opt-in via ``include_summary`` / ``include_modules`` (default off) — keep
    the default for repeated point probing.  If ``design_id`` is empty the
    first design returned by the knowledge base is used.
    """
    if not design_id:
        designs = (
            _request("GET", f"/kbs/{_kb_path(kb_name)}/structured/circuit-designs")
            .get("designs")
            or []
        )
        if not designs:
            return {
                "design_id": "",
                "query": query,
                "instances": [],
                "nets": [],
                "modules": [],
                "summary": {},
                "error": "no circuit design found",
            }
        design_id = str(designs[0].get("design_id") or "")
    plane = str(mode or "both").strip().lower()
    if plane not in {"both", "nets", "instances"}:
        plane = "both"
    pairs: list[tuple[str, str]] = []
    if plane in {"both", "nets"}:
        pairs.append(("net_query", query))
    if plane in {"both", "instances"}:
        pairs.append(("instance_query", query))
    params = urlencode(pairs)
    data = _request(
        "GET",
        f"/kbs/{_kb_path(kb_name)}/structured/circuit-designs/"
        f"{quote(design_id, safe='')}?{params}",
    )
    limit = max(1, min(int(limit or _MAX_ROWS), 200))
    return {
        "design_id": design_id,
        "query": query,
        "mode": plane,
        "summary": (data.get("summary") or {}) if include_summary else {},
        "modules": (data.get("modules") or [])[:limit] if include_modules else [],
        "instances": (data.get("instances") or [])[:limit],
        "nets": (data.get("nets") or [])[:limit],
        "cross_references": (data.get("cross_references") or [])[:limit],
    }


def _safe_number(value: Any, *, field: str, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field} must be finite")
    return number


def _safe_id(value: Any, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
    if not cleaned or cleaned in {"0", "1"}:
        cleaned = fallback
    return cleaned[:80]


def _safe_style(value: Any, default: str) -> str:
    raw = str(value or default).replace("\r", "").replace("\n", "")
    return html.escape(raw.replace('"', "'"), quote=True)


def _drawio_filename(value: Any) -> str:
    base = os.path.basename(str(value or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]+", "_", base).strip("._")
    if not base:
        base = "diagram"
    if not base.lower().endswith(".drawio"):
        base += ".drawio"
    return base[:120]


def _block_rect(block: dict[str, Any]) -> tuple[float, float, float, float]:
    x = _safe_number(block.get("x"), field="block.x")
    y = _safe_number(block.get("y"), field="block.y")
    w = _safe_number(block.get("w", block.get("width", 160)), field="block.w")
    h = _safe_number(block.get("h", block.get("height", 80)), field="block.h")
    return x, y, w, h


def _block_center(block: dict[str, Any]) -> tuple[float, float]:
    x, y, w, h = _block_rect(block)
    return x + w / 2, y + h / 2


def _side_for(block: dict[str, Any], point: tuple[float, float]) -> str:
    cx, cy = _block_center(block)
    dx, dy = point[0] - cx, point[1] - cy
    if abs(dx) >= abs(dy):
        return "R" if dx >= 0 else "L"
    return "B" if dy >= 0 else "T"


def _anchor(block: dict[str, Any], side: str, offset: float = 0.0) -> tuple[float, float]:
    x, y, w, h = _block_rect(block)
    if side == "L":
        return x, max(y + 8, min(y + h - 8, y + h / 2 + offset))
    if side == "R":
        return x + w, max(y + 8, min(y + h - 8, y + h / 2 + offset))
    if side == "T":
        return max(x + 8, min(x + w - 8, x + w / 2 + offset)), y
    return max(x + 8, min(x + w - 8, x + w / 2 + offset)), y + h


def _auto_points(
    source_side: str,
    target_side: str,
    source: tuple[float, float],
    target: tuple[float, float],
) -> list[tuple[float, float]]:
    if source_side in {"L", "R"} and target_side in {"L", "R"}:
        mid_x = (source[0] + target[0]) / 2
        return [(mid_x, source[1]), (mid_x, target[1])]
    if source_side in {"T", "B"} and target_side in {"T", "B"}:
        mid_y = (source[1] + target[1]) / 2
        return [(source[0], mid_y), (target[0], mid_y)]
    if source_side in {"L", "R"}:
        return [(target[0], source[1])]
    return [(source[0], target[1])]


def _drawio_xml(
    filename: str,
    blocks: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None,
    zones: list[dict[str, Any]] | None,
    labels: list[dict[str, Any]] | None,
) -> str:
    if not blocks:
        raise ValueError("blocks must not be empty")
    cells: list[str] = []
    ids: dict[str, str] = {}
    used: set[str] = set()
    max_x = 1200.0
    max_y = 800.0

    def _vertex(cell_id: str, item: dict[str, Any], default_style: str) -> None:
        nonlocal max_x, max_y
        x = _safe_number(item.get("x"), field=f"{cell_id}.x")
        y = _safe_number(item.get("y"), field=f"{cell_id}.y")
        w = _safe_number(item.get("w", item.get("width", 160)), field=f"{cell_id}.w")
        h = _safe_number(item.get("h", item.get("height", 80)), field=f"{cell_id}.h")
        if w <= 0 or h <= 0:
            raise ValueError(f"{cell_id} width/height must be positive")
        max_x = max(max_x, x + w)
        max_y = max(max_y, y + h)
        label = html.escape(str(item.get("label", item.get("value", ""))), quote=True)
        style = _safe_style(item.get("style"), default_style)
        cells.append(
            f'<mxCell id="{html.escape(cell_id, quote=True)}" value="{label}" '
            f'style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" '
            'as="geometry"/></mxCell>'
        )

    for index, zone in enumerate(zones or []):
        cell_id = _safe_id(zone.get("id"), f"zone{index + 1}")
        if cell_id in used:
            raise ValueError(f"duplicate id: {cell_id}")
        used.add(cell_id)
        _vertex(cell_id, zone, _DRAWIO_ZONE_STYLE)

    for index, block in enumerate(blocks):
        cell_id = _safe_id(block.get("id"), f"b{index + 1}")
        if cell_id in used:
            raise ValueError(f"duplicate id: {cell_id}")
        used.add(cell_id)
        ids[cell_id] = cell_id
        _vertex(cell_id, block, _DRAWIO_BLOCK_STYLE)

    block_specs: dict[str, dict[str, Any]] = {}
    for index, block in enumerate(blocks):
        cell_id = _safe_id(block.get("id"), f"b{index + 1}")
        block_specs[cell_id] = block

    edge_layouts: list[dict[str, Any]] = []
    side_counts: dict[tuple[str, str], int] = {}
    for index, edge in enumerate(edges or []):
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source not in ids or target not in ids:
            raise ValueError(
                f"edge {index + 1} source/target must reference a block id: "
                f"{source!r} -> {target!r}"
            )
        source_block = block_specs[source]
        target_block = block_specs[target]
        raw_points = edge.get("points") or []
        normalized_points: list[tuple[float, float]] = []
        for point_index, point in enumerate(raw_points):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"edge {index + 1} point {point_index + 1} must be [x, y]")
            normalized_points.append(
                (
                    _safe_number(point[0], field=f"edge{index + 1}.point.x"),
                    _safe_number(point[1], field=f"edge{index + 1}.point.y"),
                )
            )
        if normalized_points:
            source_side = _side_for(source_block, normalized_points[0])
            target_side = _side_for(target_block, normalized_points[-1])
        else:
            source_side = _side_for(source_block, _block_center(target_block))
            target_side = _side_for(target_block, _block_center(source_block))
        side_counts[(source, source_side)] = side_counts.get((source, source_side), 0) + 1
        side_counts[(target, target_side)] = side_counts.get((target, target_side), 0) + 1
        edge_layouts.append(
            {
                "index": index,
                "edge": edge,
                "source": source,
                "target": target,
                "source_block": source_block,
                "target_block": target_block,
                "source_side": source_side,
                "target_side": target_side,
                "points": normalized_points,
            }
        )

    side_seen: dict[tuple[str, str], int] = {}
    for layout in edge_layouts:
        index = layout["index"]
        edge = layout["edge"]
        source = layout["source"]
        target = layout["target"]
        source_block = layout["source_block"]
        target_block = layout["target_block"]
        source_side = layout["source_side"]
        target_side = layout["target_side"]
        source_key = (source, source_side)
        target_key = (target, target_side)
        source_index = side_seen.get(source_key, 0)
        target_index = side_seen.get(target_key, 0)
        side_seen[source_key] = source_index + 1
        side_seen[target_key] = target_index + 1
        source_offset = (source_index - (side_counts[source_key] - 1) / 2) * 24
        target_offset = (target_index - (side_counts[target_key] - 1) / 2) * 24
        source_point = _anchor(source_block, source_side, source_offset)
        target_point = _anchor(target_block, target_side, target_offset)
        points = list(layout["points"])
        if points:
            if source_side in {"L", "R"}:
                points[0] = (points[0][0], source_point[1])
            else:
                points[0] = (source_point[0], points[0][1])
            if target_side in {"L", "R"}:
                points[-1] = (points[-1][0], target_point[1])
            else:
                points[-1] = (target_point[0], points[-1][1])
        else:
            points = _auto_points(
                source_side, target_side, source_point, target_point
            )
        deduped: list[tuple[float, float]] = []
        for point in points:
            if not deduped or point != deduped[-1]:
                deduped.append(point)
        point_xml = ""
        if deduped:
            rendered = [
                f'<mxPoint x="{px:g}" y="{py:g}"/>' for px, py in deduped
            ]
            point_xml = f'<Array as="points">{"".join(rendered)}</Array>'
        label = html.escape(str(edge.get("label") or ""), quote=True)
        style = _safe_style(edge.get("style"), _DRAWIO_EDGE_STYLE)
        if label:
            style = style.rstrip(";") + ";labelBackgroundColor=#ffffff;"
        cells.append(
            f'<mxCell id="e{index + 1}" value="{label}" style="{style}" '
            f'edge="1" parent="1">'
            f'<mxGeometry relative="1" as="geometry">'
            f'<mxPoint x="{source_point[0]:g}" y="{source_point[1]:g}" as="sourcePoint"/>'
            f'<mxPoint x="{target_point[0]:g}" y="{target_point[1]:g}" as="targetPoint"/>'
            f'{point_xml}</mxGeometry></mxCell>'
        )

    for index, label in enumerate(labels or []):
        cell_id = _safe_id(label.get("id"), f"t{index + 1}")
        if cell_id in used:
            raise ValueError(f"duplicate id: {cell_id}")
        used.add(cell_id)
        x = _safe_number(label.get("x"), field=f"{cell_id}.x")
        y = _safe_number(label.get("y"), field=f"{cell_id}.y")
        w = _safe_number(label.get("w", label.get("width", 120)), field=f"{cell_id}.w")
        h = _safe_number(label.get("h", label.get("height", 36)), field=f"{cell_id}.h")
        text = html.escape(str(label.get("text", label.get("label", ""))), quote=True)
        cells.append(
            f'<mxCell id="{html.escape(cell_id, quote=True)}" value="{text}" '
            f'style="{_safe_style(label.get("style"), _DRAWIO_TEXT_STYLE)}" '
            f'vertex="1" parent="1">'
            f'<mxGeometry x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" '
            'as="geometry"/></mxCell>'
        )

    page_name = html.escape(filename.removesuffix(".drawio"), quote=True)
    page_width = int(max(1200.0, max_x + 80))
    page_height = int(max(800.0, max_y + 80))
    return (
        '<mxfile host="hdb-mcp-bridge" agent="hdb-mcp-bridge" version="1.0">'
        f'<diagram id="hdb-mcp-diagram" name="{page_name}">'
        f'<mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" '
        f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        f'pageWidth="{page_width}" pageHeight="{page_height}" math="0" shadow="0">'
        '<root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        + "".join(cells)
        + "</root></mxGraphModel></diagram></mxfile>"
    )


@mcp.tool()
def create_drawio_file(
    filename: str,
    blocks: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
    zones: list[dict[str, Any]] | None = None,
    labels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a native .drawio document from a compact block/edge spec.

    ``blocks`` items accept ``id``, ``label``, ``x``, ``y``, ``w``, ``h`` and
    an optional ``style``.  ``edges`` items accept ``source``/``target`` block
    ids, optional ``label``, ``points`` (\\[x, y\\] waypoints) and ``style``.
    ``zones`` and ``labels`` use the same geometry fields as blocks.

    This tool only builds XML; it never writes to the host filesystem.  The
    caller is responsible for saving ``content`` as ``filename``.
    """
    safe_filename = _drawio_filename(filename)
    content = _drawio_xml(safe_filename, blocks, edges, zones, labels)
    encoded = content.encode("utf-8")
    if len(encoded) > 5 * 1024 * 1024:
        raise ValueError("drawio content exceeds the 5 MiB limit")
    return {
        "filename": safe_filename,
        "media_type": "application/vnd.jgraph.mxfile",
        "size_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "content": content,
    }


def main() -> None:
    """Start the MCP bridge in either stdio or remote streamable-http mode.

    HDB_MCP_TRANSPORT=stdio (default) keeps the host-spawned local process;
    streamable-http exposes the same tools over HTTP at /mcp for remote
    registration (scripts/hdb_mcp_http.sh, platform plugins page).
    """
    transport = os.getenv("HDB_MCP_TRANSPORT", "stdio").strip().lower()
    host = os.getenv("HDB_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("HDB_MCP_PORT", "9100"))
    if transport in {"http", "streamable-http", "streamable_http"}:
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")  # endpoint: http://<host>:<port>/mcp
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
