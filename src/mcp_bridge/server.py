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
"""

from __future__ import annotations

import base64
import binascii
import os
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.services.knowledge_retrieval import (
    clip as _clip,
    grep_chunks,
    lexical_search,
    looks_like_exact_term,
    read_chunks,
)


def _normalise_host(value: str) -> str:
    host = str(value or "").strip().rstrip("/")
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.split("/", 1)[0]


def _mcp_allowed_hosts() -> list[str]:
    """Host-header allow-list for the streamable-http transport.

    Loopback is always allowed.  ``HDB_MCP_HTTP_HOST`` (the public address
    the UI advertises) and ``HDB_MCP_ALLOWED_HOSTS`` (comma-separated extras)
    extend it for LAN/domain access.  A bare host gets the ``:*`` port
    wildcard; an explicit ``host:port``/IPv6 pattern is kept as-is.
    """
    patterns = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    extras = ",".join(
        [
            os.getenv("HDB_MCP_ALLOWED_HOSTS", ""),
            os.getenv("HDB_MCP_HTTP_HOST", ""),
        ]
    )
    for raw in extras.split(","):
        host = _normalise_host(raw)
        if not host:
            continue
        pattern = host if ":" in host else f"{host}:*"
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def _mcp_allowed_origins(hosts: list[str]) -> list[str]:
    origins: list[str] = []
    for host in hosts:
        for scheme in ("http", "https"):
            origin = f"{scheme}://{host}"
            if origin not in origins:
                origins.append(origin)
    return origins


_ALLOWED_HOSTS = _mcp_allowed_hosts()
mcp = FastMCP(
    "hdb-knowledge",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=_mcp_allowed_origins(_ALLOWED_HOSTS),
    ),
)

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
            "directory_id": row.get("directory_id", 0),
            "relative_path": row.get("relative_path") or row.get("name", ""),
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
    picked = read_chunks(
        [chunk for chunk in raw_chunks if isinstance(chunk, dict)],
        start_index=start,
        max_chunks=limit,
        query=needle,
    )
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
    mode: str = "auto",
    kind: str = "document",
) -> dict[str, Any]:
    """Stateless search over one KB's parsed chunks (no session).

    ``mode``: ``auto`` (default) detects exact refdes/part/net ids and uses
    literal grep, otherwise lexical ranking; ``exact`` forces literal grep;
    ``semantic``/``lexical`` uses lexical ranking on this bridge (true
    semantic retrieval lives in the platform, not the stateless bridge).
    ``kind``: ``document`` (default), ``table``, ``circuit`` or ``all``.
    Results are chunk locators (file / index / rag_id) with snippet + score;
    read the text with ``hdb_read_file_chunks``.  The chunk index is cached
    per file (~15 min TTL, ``refresh=True`` forces a rebuild).
    """
    needle = str(query or "").strip()
    if not needle:
        raise ValueError("query is required")
    rows = _request("GET", f"/kbs/{_kb_path(kb_name)}/files")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    files_searched = 0
    chunks_searched = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("id") or "")
        fid_name = str(row.get("name") or "")
        if file_id and fid != file_id and fid_name != file_id:
            continue
        processor = str(row.get("processor_kind") or "")
        wanted = str(kind or "document").strip().lower()
        if wanted == "document" and processor and processor != "ragflow":
            continue
        if wanted == "table" and "spreadsheet" not in processor:
            continue
        if wanted == "circuit" and "circuit" not in processor:
            continue
        if wanted not in {"document", "table", "circuit", "all"}:
            wanted = "document"
        cache = _load_chunks(kb_name, fid, refresh)
        grouped[(fid, fid_name)] = list(cache.get("chunks") or [])
        files_searched += 1
        chunks_searched += len(cache.get("chunks") or [])
    plane = str(mode or "auto").strip().lower()
    exact = plane == "exact" or (
        plane == "auto" and looks_like_exact_term(needle)
    )
    if exact:
        results = grep_chunks(grouped, [needle], max_hits=max(1, min(int(top_k or 8), 50)))
        used = "exact"
    else:
        results = lexical_search(
            grouped, needle, top_k=max(1, min(int(top_k or 8), 50))
        )
        # The stateless bridge has no RAGFlow semantic endpoint; be explicit
        # instead of pretending lexical ranking is semantic retrieval.
        used = "lexical"
    return {
        "kb_name": kb_name,
        "query": needle,
        "kind": wanted,
        "mode": used,
        "results": results,
        "result_count": len(results),
        "files_searched": files_searched,
        "chunks_searched": chunks_searched,
        "cache_files": len(_CHUNK_CACHE),
        "note": "lexical/exact score over raw chunks; read hits with hdb_read_file_chunks(file_id, start_index)",
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
    from src.services.request_context import build_context_for_user
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
        "relative_path": str(
            (getattr(info, "metadata", {}) or {}).get("relative_path")
            or getattr(info, "name", file_id)
        ),
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
    directory_id: int = 0,
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
    form = {"directory_id": str(int(directory_id or 0))}
    if str(source_group or "").strip():
        form["source_group"] = source_group
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


_HTTP_AUTH_CACHE: dict[str, tuple[float, str]] = {}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


async def _json_401(send, message: str) -> None:
    """Emit a minimal MCP-compatible 401 JSON error."""
    import json as _json

    payload = _json.dumps({"error": message}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class _McpTokenAuthMiddleware:
    """ASGI gate around the FastMCP streamable-http app.

    Requires a per-user ``hdbmcp_*`` token in the ``X-HDB-MCP-Token`` header
    and verifies it through the platform exchange — the same contract
    scripts/hdb_mcp_user_setup.sh mints (POST /me/mcp-token).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        token = str(headers.get("x-hdb-mcp-token", "")).strip()
        if not token.startswith("hdbmcp_"):
            await _json_401(send, "X-HDB-MCP-Token hdbmcp_* header required")
            return
        cached = _HTTP_AUTH_CACHE.get(token)
        now = time.monotonic()
        if not (cached and now < cached[0]):
            response = _client().post(
                f"{_API_BASE}/mcp/session",
                headers={"X-HDB-MCP-Token": token},
            )
            if response.status_code >= 400:
                await _json_401(send, f"mcp token rejected ({response.status_code})")
                return
            bearer = (response.json() or {}).get("token") or ""
            if not bearer:
                await _json_401(send, "mcp token exchange returned no bearer")
                return
            if len(_HTTP_AUTH_CACHE) > 1000:
                for stale in sorted(_HTTP_AUTH_CACHE.items(), key=lambda item: item[1][0])[:32]:
                    _HTTP_AUTH_CACHE.pop(stale[0], None)
            _HTTP_AUTH_CACHE[token] = (now + 900.0, bearer)
        await self.app(scope, receive, send)


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _warn_or_fail_open_socket(host: str, *, allow_unauth: bool) -> None:
    """Print run-mode info: default behavior mounts the per-user hdbmcp_*
    gate; ``HDB_MCP_HTTP_ALLOW_UNAUTHED=1`` opts out for intranet use."""
    if _is_loopback(host):
        if allow_unauth:
            print("[hdb-mcp] note: loopback socket without the token gate "
                  "is expected to be safe (same-machine only).")
        return
    if allow_unauth:
        print(
            "[hdb-mcp] WARNING: binding %s WITHOUT the per-user token gate "
            "(HDB_MCP_HTTP_ALLOW_UNAUTHED=1) — intranet exposure only." % host
        )
    print(
        "[hdb-mcp] info: %s bound; agents must send X-HDB-MCP-Token with an "
        "hdbmcp_* token from scripts/hdb_mcp_user_setup.sh." % host
    )
    return


def main() -> None:
    """Start the MCP bridge in either stdio or remote streamable-http mode.

    HDB_MCP_TRANSPORT=stdio (default) keeps the host-spawned local process;
    streamable-http exposes the same tools over HTTP at /mcp for remote
    registration (scripts/hdb_mcp_http.sh, platform plugins page).  The
    remote mode runs the per-user hdbmcp_* token gate middleware by
    default; HDB_MCP_HTTP_ALLOW_UNAUTHED=1 removes it explicitly.
    """
    transport = os.getenv("HDB_MCP_TRANSPORT", "stdio").strip().lower()
    host = os.getenv("HDB_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("HDB_MCP_PORT", "9100"))
    if transport in {"http", "streamable-http", "streamable_http"}:
        allow_unauth = os.getenv("HDB_MCP_HTTP_ALLOW_UNAUTHED") == "1"
        _warn_or_fail_open_socket(host, allow_unauth=allow_unauth)
        mcp.settings.host = host
        mcp.settings.port = port
        import uvicorn

        app = mcp.streamable_http_app()
        if not allow_unauth:
            # per-user hdbmcp_* token gate (default); raw app only with the
            # explicit escape hatch.
            app = _McpTokenAuthMiddleware(app)
        uvicorn.run(app, host=host, port=port, log_level="info")
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
