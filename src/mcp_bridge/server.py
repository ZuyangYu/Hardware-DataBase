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
import hashlib
import json
import mimetypes
import os
import time
from contextvars import ContextVar
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.services.knowledge_retrieval import (
    clip as _clip,
    read_chunks,
)

from src.services.document_search import (
    SUPPORTED_KINDS,
    SUPPORTED_MODES,
    DocumentSearchRequest,
    search_documents as run_document_search,
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

_REQUEST_MCP_TOKEN: ContextVar[str] = ContextVar("hdb_mcp_request_token", default="")
_REQUEST_BEARER: ContextVar[str] = ContextVar("hdb_mcp_request_bearer", default="")
_REQUEST_USER: ContextVar[dict[str, Any]] = ContextVar("hdb_mcp_request_user", default={})

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


def _exchange_mcp_token(
    token: str, *, force: bool = False
) -> tuple[str, dict[str, Any]] | None:
    """Exchange a per-user hdbmcp_* token for a bearer and user identity."""

    now = time.monotonic()
    if not force:
        cached = _HTTP_AUTH_CACHE.get(token)
        if cached and now < cached[0]:
            return cached[1], cached[2]
    response = _client().post(
        f"{_API_BASE}/mcp/session",
        headers={"X-HDB-MCP-Token": token},
    )
    if response.status_code >= 400:
        _HTTP_AUTH_CACHE.pop(token, None)
        return None
    payload = response.json() or {}
    bearer = str(payload.get("token") or "")
    if not bearer:
        return None
    user = payload.get("user")
    user = dict(user) if isinstance(user, dict) else {}
    if len(_HTTP_AUTH_CACHE) > 1000:
        for stale in sorted(
            _HTTP_AUTH_CACHE.items(), key=lambda item: item[1][0]
        )[:32]:
            _HTTP_AUTH_CACHE.pop(stale[0], None)
    _HTTP_AUTH_CACHE[token] = (now + 900.0, bearer, user)
    return bearer, user


def _headers() -> dict[str, str]:
    request_bearer = _REQUEST_BEARER.get()
    if request_bearer:
        return {"Authorization": f"Bearer {request_bearer}"}
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
    if response.status_code == 401:
        # The short-lived exchange token may have expired.  In HTTP mode the
        # original per-user hdbmcp_* token is available in the request context;
        # in stdio mode fall back to the configured process token.
        request_token = _REQUEST_MCP_TOKEN.get()
        if request_token:
            exchanged = _exchange_mcp_token(request_token, force=True)
            if exchanged is not None:
                bearer, user = exchanged
                _REQUEST_BEARER.set(bearer)
                _REQUEST_USER.set(user)
                response = _client().request(
                    method, f"{_API_BASE}{path}", headers=_headers(), **kwargs
                )
        elif _MCP_TOKEN:
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


def _current_user() -> dict[str, Any]:
    user = dict(_REQUEST_USER.get() or {})
    return user or _LOGIN_USER


def _department_id(kb: dict[str, Any]) -> str:
    return str(kb.get("department_id") or _current_user().get("department_id") or "")


@mcp.tool()
def list_kbs() -> list[dict[str, Any]]:
    """List knowledge bases visible to the configured account."""
    return _request("GET", "/kbs")


@mcp.tool()
def list_kb_files(
    kb_name: str,
    directory_id: int | None = None,
    recursive: bool = False,
    query: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List source files in one knowledge base.

    ``directory_id`` scopes the listing to one directory; set ``recursive`` to
    include descendants.  ``query`` filters by relative path.
    """
    params: dict[str, Any] = {
        "limit": int(limit),
        "offset": int(offset),
    }
    if directory_id is not None:
        params["directory_id"] = int(directory_id)
        params["recursive"] = bool(recursive)
    if str(query or "").strip():
        params["query"] = str(query).strip()
    rows = _request("GET", f"/kbs/{_kb_path(kb_name)}/files", params=params)
    return [
        {
            "id": row.get("id", ""),
            "file_id": row.get("id", ""),
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


def _directory_rows(
    kb_name: str, parent_id: int, recursive: bool
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending = [int(parent_id or 0)]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        children = _request(
            "GET",
            f"/kbs/{_kb_path(kb_name)}/directories",
            params={"parent_id": current},
        )
        for child in children or []:
            if not isinstance(child, dict):
                continue
            rows.append(dict(child))
            child_id = int(child.get("id") or 0)
            if recursive and child_id:
                pending.append(child_id)
        if not recursive:
            break
    return rows


def _directory_tree(
    rows: list[dict[str, Any]], root_parent_id: int
) -> list[dict[str, Any]]:
    children: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        children.setdefault(int(row.get("parent_id") or 0), []).append(row)

    def build(parent_id: int) -> list[dict[str, Any]]:
        result = []
        for row in sorted(
            children.get(parent_id, []),
            key=lambda item: (
                int(item.get("depth") or 0),
                str(item.get("name") or ""),
            ),
        ):
            item = dict(row)
            item["children"] = build(int(row.get("id") or 0))
            result.append(item)
        return result

    return build(int(root_parent_id or 0))


@mcp.tool()
def list_directories(
    kb_name: str, parent_id: int = 0, recursive: bool = True
) -> dict[str, Any]:
    """List the KB directory tree, including empty folders.

    ``parent_id=0`` starts at the root.  ``recursive=True`` returns every
    descendant; ``False`` returns only direct children.  The response includes
    both a flat ``directories`` list and a nested ``tree``.
    """

    rows = _directory_rows(kb_name, parent_id, recursive)
    return {
        "kb_name": kb_name,
        "parent_id": int(parent_id or 0),
        "recursive": bool(recursive),
        "directory_count": len(rows),
        "directories": rows,
        "tree": _directory_tree(rows, int(parent_id or 0)),
    }


def _parse_task_id(task_id: str, file_id: str) -> str:
    value = str(task_id or "").strip()
    if value:
        return value
    file_value = str(file_id or "").strip()
    if file_value.startswith("ragflow-"):
        return file_value
    if file_value.startswith("ragflow:"):
        file_value = file_value.split(":", 1)[1]
    if file_value.isdigit():
        return f"ragflow-{file_value}"
    raise ValueError("task_id or a ragflow file_id is required")


@mcp.tool()
def get_parse_task(
    kb_name: str, task_id: str = "", file_id: str = ""
) -> dict[str, Any]:
    """Read one parse task and its failure reason.

    Pass either the parse ``task_id`` (for example ``ragflow-57``) or the
    document ``file_id`` returned by ``list_kb_files`` (for example
    ``ragflow:57``).  Requires read permission on the knowledge base.
    """

    resolved = _parse_task_id(task_id, file_id)
    return _request(
        "GET",
        f"/kbs/{_kb_path(kb_name)}/parse-tasks/{quote(resolved, safe='')}",
    )


@mcp.tool()
def retry_parse(
    kb_name: str, task_id: str = "", file_id: str = ""
) -> dict[str, Any]:
    """Re-submit one parse task without re-uploading the original file.

    Pass either ``task_id`` or the document ``file_id``.  Requires write
    permission.  RAGFlow documents are re-submitted remotely; spreadsheet
    tasks are re-queued for the local worker.
    """

    resolved = _parse_task_id(task_id, file_id)
    return _request(
        "POST",
        f"/kbs/{_kb_path(kb_name)}/parse-tasks/{quote(resolved, safe='')}/retry",
    )


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
    try:
        data = _request(
            "GET",
            f"/kbs/{_kb_path(kb_name)}/files/{quote(str(file_id), safe='')}/chunks",
        )
    except RuntimeError as exc:
        if "citation_processor_unsupported" in str(exc):
            # Spreadsheet/circuit records have no document chunk stream.
            # Point the caller at the structured tools instead of failing
            # with an opaque upstream 422.
            return {
                "error": "structured_source",
                "message": (
                    "该文件是表格/网表，不是文档 chunk；"
                    "请使用 query_table 或 query_circuit。"
                ),
                "file_id": str(file_id),
            }
        raise
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


def _directory_scope(
    kb_name: str, directory_id: int, recursive: bool
) -> set[int] | None:
    """Resolve a directory filter to itself and, optionally, its descendants."""

    target = int(directory_id or 0)
    if target <= 0:
        return None
    # Fail closed when the id is not a directory in this KB/department.
    _request(
        "GET",
        f"/kbs/{_kb_path(kb_name)}/directories/{target}",
    )
    allowed = {target}
    if not recursive:
        return allowed
    pending = [target]
    while pending:
        parent = pending.pop()
        children = _request(
            "GET",
            f"/kbs/{_kb_path(kb_name)}/directories",
            params={"parent_id": parent},
        )
        for child in children or []:
            if not isinstance(child, dict):
                continue
            child_id = int(child.get("id") or 0)
            if child_id and child_id not in allowed:
                allowed.add(child_id)
                pending.append(child_id)
    return allowed


def _semantic_search(
    kb_name: str,
    query: str,
    top_k: int,
    directory_id: int,
    recursive: bool,
    file_id: str,
) -> dict[str, Any]:
    """Call the platform's real RAGFlow semantic retrieval endpoint."""

    return _request(
        "POST",
        f"/kbs/{_kb_path(kb_name)}/semantic-search",
        json={
            "query": query,
            "top_k": max(1, min(int(top_k or 8), 50)),
            "directory_id": int(directory_id or 0),
            "recursive": bool(recursive),
            "file_id": str(file_id or ""),
        },
    )


@mcp.tool()
def search_knowledge_base(
    kb_name: str,
    query: str,
    top_k: int = 8,
    file_id: str = "",
    refresh: bool = False,
    mode: Literal["auto", "exact", "hybrid"] = "auto",
    kind: str = "document",
    directory_id: int = 0,
    recursive: bool = True,
) -> dict[str, Any]:
    """Stateless search over one KB's parsed chunks (no session).

    ``mode``: ``auto`` (default) detects exact refdes/part/net ids and uses
    literal grep; otherwise it runs fast IDF-weighted lexical retrieval, and
    only escalates to RAGFlow semantic fusion when lexical confidence is low.
    ``hybrid`` forces semantic + lexical fusion; ``exact`` forces literal
    grep.  These are the only three supported modes.
    ``kind``: ``document`` (default), ``table``, ``circuit`` or ``all``.
    ``directory_id`` + ``recursive`` restrict the search to a directory and,
    when recursive, its descendants.  Results are chunk locators (file / index
    / rag_id) with snippet + score; read the text with
    ``hdb_read_file_chunks``.  Unparsed files are returned separately in
    ``raw_file_matches`` (filename/path match only) with a hint to use
    ``download_kb_file(include_text=true)``.  ``kind=table`` returns the
    materialized table list and points to ``query_table``.  The chunk index is
    cached per file (~15 min TTL, ``refresh=True`` forces a rebuild).
    """
    needle = str(query or "").strip()
    if not needle:
        raise ValueError("query is required")
    wanted = str(kind or "document").strip().lower()
    if wanted not in SUPPORTED_KINDS:
        wanted = "document"
    plane = str(mode or "auto").strip().lower()
    if plane not in SUPPORTED_MODES:
        raise ValueError("mode must be one of: auto, exact, hybrid")
    if wanted == "table":
        tables = list_tables(
            kb_name, directory_id=directory_id, recursive=recursive
        )
        return {
            "kb_name": kb_name,
            "query": needle,
            "kind": wanted,
            "mode": "table",
            "route": "table",
            "directory_id": int(directory_id or 0),
            "recursive": bool(recursive),
            "results": [],
            "result_count": 0,
            "raw_file_matches": [],
            "tables": tables.get("tables", []),
            "hint": (
                "table cells are not in the document chunk index; "
                "use list_tables + query_table"
            ),
        }

    rows = _request("GET", f"/kbs/{_kb_path(kb_name)}/files")
    allowed_directory_ids = _directory_scope(kb_name, directory_id, recursive)

    def _scoped_rows() -> list[dict[str, Any]]:
        scoped: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            fid = str(row.get("id") or "")
            fid_name = str(row.get("relative_path") or row.get("name") or "")
            if file_id and (
                fid != file_id
                and fid_name != file_id
                and str(row.get("name") or "") != file_id
            ):
                continue
            if (
                allowed_directory_ids is not None
                and int(row.get("directory_id") or 0) not in allowed_directory_ids
            ):
                continue
            scoped.append(row)
        return scoped

    scoped_rows = _scoped_rows()

    class _Backend:
        def list_files(self) -> list[dict[str, Any]]:
            return scoped_rows

        def load_chunks(self, row_file_id: str) -> list[dict[str, Any]]:
            return list(
                _load_chunks(kb_name, row_file_id, refresh).get("chunks") or []
            )

        def semantic_search(
            self, query_text: str, limit: int
        ) -> list[dict[str, Any]]:
            payload = _semantic_search(
                kb_name,
                query_text,
                limit,
                directory_id,
                recursive,
                file_id,
            )
            return list((payload or {}).get("results") or [])

    outcome = run_document_search(
        _Backend(),
        DocumentSearchRequest(
            query=needle,
            top_k=top_k,
            mode=plane,
            kind=wanted,
            file_id=file_id,
        ),
    )
    payload = {
        "kb_name": kb_name,
        "query": needle,
        "kind": wanted,
        "directory_id": int(directory_id or 0),
        "recursive": bool(recursive),
        "cache_files": len(_CHUNK_CACHE),
        **outcome.as_dict(),
    }
    if outcome.raw_file_matches:
        payload["hint"] = (
            "raw files matched by filename/path. "
            "Use download_kb_file(file_id, include_text=true) to read them."
        )
    return payload


@mcp.tool()
def download_kb_file(
    kb_name: str,
    file_id: str = "",
    filename: str = "",
    directory_id: int | None = None,
    max_bytes: int = 20 * 1024 * 1024,
    include_text: bool = False,
) -> dict[str, Any]:
    """Read the original uploaded file as base64 (requires KB read access).

    This works for every stored file, including ``raw_archived`` types that
    have no parser (``.py``, ``.drawio``, archives, ...).  Locate the file by
    ``file_id``, or by ``filename`` optionally scoped to ``directory_id``.
    Large files are rejected rather than silently truncated; the caller is
    responsible for decoding ``content_base64``.

    Set ``include_text=true`` to also receive a UTF-8 preview for text files.
    """
    kb = _visible_kb(kb_name)
    from src import settings as app_settings
    from src.services.request_context import build_context_for_user
    from src.core.app_pipeline import AppPipeline
    from src.core.auth import AuthService

    file_id = str(file_id or "").strip()
    filename = str(filename or "").strip()
    if not file_id and not filename:
        raise ValueError("file_id or filename is required")

    auth = AuthService()
    username = str(_current_user().get("username") or _USERNAME or "").strip()
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

    def _metadata(info: Any) -> dict[str, Any]:
        value = getattr(info, "metadata", {}) or {}
        return value if isinstance(value, dict) else {}

    candidates = []
    for info in infos:
        info_id = str(getattr(info, "id", ""))
        info_name = str(getattr(info, "name", ""))
        meta = _metadata(info)
        relative_path = str(meta.get("relative_path") or "")
        if file_id:
            if info_id == file_id or info_name == file_id:
                candidates.append(info)
            continue
        if not (
            info_name == filename
            or relative_path == filename
            or relative_path.endswith("/" + filename)
        ):
            continue
        if directory_id is not None and int(
            meta.get("directory_id") or 0
        ) != int(directory_id):
            continue
        candidates.append(info)
    if not candidates:
        locator = file_id or filename
        raise RuntimeError(f"file not found: {locator}")
    if len(candidates) > 1:
        raise RuntimeError(
            "file locator is ambiguous; pass file_id or a directory_id"
        )
    info = candidates[0]
    meta = _metadata(info)
    local_path = str(getattr(info, "local_path", "") or "")
    if not local_path:
        raise RuntimeError(f"file has no local archive: {file_id or filename}")
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
        raise RuntimeError(f"file archive is missing: {file_id or filename}")
    limit = max(1, min(int(max_bytes or 20 * 1024 * 1024), 100 * 1024 * 1024))
    size = os.path.getsize(path)
    if size > limit:
        raise ValueError(f"file is {size} bytes; max_bytes={limit}")
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"file exceeds max_bytes={limit}")
    name = str(getattr(info, "name", filename or file_id))
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    result = {
        "file_id": str(getattr(info, "id", file_id)),
        "filename": name,
        "relative_path": str(meta.get("relative_path") or name),
        "size_bytes": len(data),
        "content_type": content_type,
        "sha256": hashlib.sha256(data).hexdigest(),
        "processor_kind": str(meta.get("processor_kind") or ""),
        "status": str(meta.get("status") or getattr(info, "status", "") or ""),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }
    try:
        probe = data[:4096]
        is_text = b"\x00" not in probe
        probe.decode("utf-8")
    except UnicodeDecodeError:
        is_text = False
    result["is_text"] = is_text
    if include_text:
        text = data.decode("utf-8", errors="replace") if is_text else ""
        result["text"] = text[:_MAX_TOOL_TEXT]
        result["text_truncated"] = len(text) > _MAX_TOOL_TEXT
    return result


def _record_id_from_file_row(row: dict[str, Any]) -> int:
    value = str(row.get("id") or "")
    if value.startswith("ragflow:"):
        try:
            return int(value.split(":", 1)[1])
        except ValueError:
            return 0
    try:
        return int(row.get("record_id") or 0)
    except (TypeError, ValueError):
        return 0


def _scoped_file_rows(
    kb_name: str,
    directory_id: int,
    recursive: bool,
    *,
    processor: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[int] | None]:
    rows = _request("GET", f"/kbs/{_kb_path(kb_name)}/files")
    allowed = _directory_scope(kb_name, directory_id, recursive)
    scoped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if processor and processor not in str(row.get("processor_kind") or ""):
            continue
        if allowed is not None and int(row.get("directory_id") or 0) not in allowed:
            continue
        scoped.append(row)
    return rows, scoped, allowed


def _design_matches_scope(
    design: dict[str, Any],
    allowed_files: list[dict[str, Any]],
    allowed_record_ids: set[int],
) -> bool:
    record_id = int(design.get("record_id") or 0)
    if record_id and record_id in allowed_record_ids:
        return True
    design_id = str(design.get("design_id") or "")
    design_files = {str(item or "") for item in (design.get("files") or [])}
    for row in allowed_files:
        name = str(row.get("name") or "")
        relative = str(row.get("relative_path") or name)
        if name in design_files or os.path.basename(relative) in design_files:
            return True
        stem = os.path.splitext(os.path.basename(name))[0]
        if stem and (design_id.startswith(stem) or stem in design_id):
            return True
    return False


def _circuit_designs(
    kb_name: str, directory_id: int, recursive: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = _request("GET", f"/kbs/{_kb_path(kb_name)}/structured/circuit-designs")
    designs = list(data.get("designs") or [])
    failed_logs = list(data.get("failed_logs") or [])
    if not int(directory_id or 0):
        return designs, failed_logs
    _, allowed_files, _allowed = _scoped_file_rows(
        kb_name, directory_id, recursive, processor="circuit"
    )
    allowed_record_ids = {
        _record_id_from_file_row(row) for row in allowed_files
    } - {0}
    designs = [
        design for design in designs
        if _design_matches_scope(design, allowed_files, allowed_record_ids)
    ]
    allowed_design_ids = {str(item.get("design_id") or "") for item in designs}
    failed_logs = [
        item for item in failed_logs
        if str(item.get("design_id") or "") in allowed_design_ids
    ]
    return designs, failed_logs


@mcp.tool()
def list_tables(
    kb_name: str, directory_id: int = 0, recursive: bool = True
) -> dict[str, Any]:
    """List materialized SQL tables and their schemas for one knowledge base."""
    kb = _visible_kb(kb_name)
    from src.agents.tools.spreadsheet_tools import _load_sql_registry
    from src.services.spreadsheet_index_service import SpreadsheetIndexService

    db_path = SpreadsheetIndexService().db_path(
        _department_id(kb), kb_name, create=False
    )
    tables = _load_sql_registry(db_path)
    if int(directory_id or 0):
        _, scoped, _allowed = _scoped_file_rows(
            kb_name, directory_id, recursive, processor="spreadsheet"
        )
        allowed_record_ids = {
            _record_id_from_file_row(row) for row in scoped
        } - {0}
        tables = [
            table for table in tables
            if int(table.get("record_id") or 0) in allowed_record_ids
        ]
    return {
        "kb_name": kb_name,
        "department_id": _department_id(kb),
        "directory_id": int(directory_id or 0),
        "recursive": bool(recursive),
        "table_count": len(tables),
        "tables": tables,
    }


@mcp.tool()
def query_table(
    kb_name: str,
    sql: str,
    limit: int = 200,
    directory_id: int = 0,
    recursive: bool = True,
) -> dict[str, Any]:
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
    if int(directory_id or 0):
        _, scoped, _directory_scope = _scoped_file_rows(
            kb_name, directory_id, recursive, processor="spreadsheet"
        )
        allowed_record_ids = {
            _record_id_from_file_row(row) for row in scoped
        } - {0}
        registry = [
            entry for entry in registry
            if int(entry.get("record_id") or 0) in allowed_record_ids
        ]
    if not registry:
        return {
            "kb_name": kb_name,
            "directory_id": int(directory_id or 0),
            "recursive": bool(recursive),
            "error": "no tables in directory",
            "tables": [],
        }
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
        "directory_id": int(directory_id or 0),
        "recursive": bool(recursive),
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
    relative_path: str = "",
) -> dict[str, Any]:
    """Upload one file into a knowledge base (requires write permission).

    ``content_base64`` is the standard base64 encoding of the file bytes.  The
    platform applies the caller's write permission, audit and parsing pipeline;
    this is a real write, not a proposal.

    ``relative_path`` optionally preserves a folder path below
    ``directory_id`` (for example ``hd_sloution/references/01.md``); missing
    directories are created automatically.
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
    relative = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
    if relative:
        form["manifest"] = json.dumps(
            {"entries": [{"index": 0, "relative_path": relative}]},
            ensure_ascii=False,
        )
    return _request(
        "POST", f"/kbs/{_kb_path(kb_name)}/files", files=files, data=form
    )


@mcp.tool()
def delete_file(
    kb_name: str,
    filename: str,
    confirm: bool = False,
    directory_id: int = 0,
) -> dict[str, Any]:
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
        "DELETE",
        f"/kbs/{_kb_path(kb_name)}/files/{quote(name, safe='')}",
        params={"directory_id": int(directory_id or 0)},
    )


@mcp.tool()
def list_spreadsheets(
    kb_name: str, directory_id: int = 0, recursive: bool = True
) -> dict[str, Any]:
    """List structured spreadsheet sources in one knowledge base."""
    data = _request(
        "GET", f"/kbs/{_kb_path(kb_name)}/structured/spreadsheets"
    )
    if int(directory_id or 0):
        _, scoped, _allowed = _scoped_file_rows(
            kb_name, directory_id, recursive, processor="spreadsheet"
        )
        allowed_record_ids = {
            _record_id_from_file_row(row) for row in scoped
        } - {0}
        data["rows"] = [
            row for row in (data.get("rows") or [])
            if int(row.get("record_id") or 0) in allowed_record_ids
        ]
        data.setdefault("totals", {})["file_count"] = len(data["rows"])
    data["directory_id"] = int(directory_id or 0)
    data["recursive"] = bool(recursive)
    return data


@mcp.tool()
def list_circuit_designs(
    kb_name: str, directory_id: int = 0, recursive: bool = True
) -> dict[str, Any]:
    """List circuit/netlist designs in one knowledge base."""
    designs, failed_logs = _circuit_designs(kb_name, directory_id, recursive)
    return {
        "designs": designs,
        "failed_logs": failed_logs,
        "directory_id": int(directory_id or 0),
        "recursive": bool(recursive),
    }


@mcp.tool()
def query_circuit(
    kb_name: str,
    query: str,
    design_id: str = "",
    limit: int = _MAX_ROWS,
    mode: str = "both",
    include_summary: bool = False,
    include_modules: bool = False,
    directory_id: int = 0,
    recursive: bool = True,
) -> dict[str, Any]:
    """Search one circuit design by instance/refdes or net/signal name.

    ``mode`` selects the search plane: ``both`` (default), ``nets``, or
    ``instances``.  The bulky design summary and per-page module roll-up are
    opt-in via ``include_summary`` / ``include_modules`` (default off) — keep
    the default for repeated point probing.  If ``design_id`` is empty the
    first design returned by the knowledge base is used.
    """
    designs, _ = _circuit_designs(kb_name, directory_id, recursive)
    if design_id:
        allowed_design_ids = {
            str(design.get("design_id") or "") for design in designs
        }
        if int(directory_id or 0) and design_id not in allowed_design_ids:
            return {
                "design_id": design_id,
                "query": query,
                "instances": [],
                "nets": [],
                "modules": [],
                "summary": {},
                "error": "design not in directory",
            }
    else:
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


_HTTP_AUTH_CACHE: dict[str, tuple[float, str, dict[str, Any]]] = {}
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
        exchanged = _exchange_mcp_token(token)
        if exchanged is None:
            await _json_401(send, "mcp token rejected")
            return
        bearer, user = exchanged
        token_ctx = _REQUEST_MCP_TOKEN.set(token)
        bearer_ctx = _REQUEST_BEARER.set(bearer)
        user_ctx = _REQUEST_USER.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST_USER.reset(user_ctx)
            _REQUEST_BEARER.reset(bearer_ctx)
            _REQUEST_MCP_TOKEN.reset(token_ctx)


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
