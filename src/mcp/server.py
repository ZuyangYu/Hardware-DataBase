"""MCP server exposing the Hardware DataBase API as tools for local agents.

This is the "MCP layer" built on top of the HTTP API (see ``src/api/``): it is a
*client* of the API, not a parallel in-process path, so there is one business
logic path and auth/permissions stay server-side. It reuses the CLI's
``ApiClient`` (``src/cli/client.py``) for HTTP + SSE, and resolves the API url
and token the same way the CLI does (``HDB_TOKEN`` env or the session saved by
``hardware-database login``).

Run as a stdio MCP server (Claude Code spawns it)::

    hardware-database-mcp        # or: uv run hardware-database-mcp

The API server (``hardware-database-server``) must be running separately; the
``health`` tool probes it and tells the agent what to do if it is down/unauthed.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from src.cli import config as clicfg
from src.cli import session as sess
from src.cli.client import ApiClient, ApiError

mcp = FastMCP("hardware-database")

_DEFAULT_URL = "http://127.0.0.1:8000"


def _resolve() -> tuple[str, str | None]:
    """Resolve (api_url, token) from env / saved session, exactly like the CLI."""
    s = sess.load_session() or {}
    token = os.getenv("HDB_TOKEN") or s.get("token")
    api_url = clicfg.resolve_api_url(os.getenv("HDB_API_URL"), s.get("api_url"))
    return api_url, token


def _client() -> ApiClient:
    api_url, token = _resolve()
    return ApiClient(api_url, token=token)


def _err(exc: ApiError) -> dict[str, Any]:
    # Return a clean dict instead of raising: an unhandled raise would kill the
    # stdio request; a structured error lets the agent react (e.g. re-login).
    return {"error": exc.message, "status_code": exc.status_code}


def _health(api_url: str, token: str | None) -> dict[str, Any]:
    """Three-state probe: server_down / server_up (unauthed) / ok (authed)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.get(f"{api_url}/whoami", headers=headers, timeout=3.0)
    except httpx.HTTPError:
        return {"status": "server_down", "url": api_url,
                "hint": "start the API: `uv run hardware-database-server` (HDB_API_HOST/PORT)"}
    if r.status_code in (401, 403):
        return {"status": "server_up", "authed": False, "url": api_url,
                "hint": "run `hardware-database login --user <u>` (or set HDB_TOKEN)"}
    if r.status_code == 200:
        return {"status": "ok", "authed": True, "url": api_url, "user": r.json()}
    return {"status": "error", "code": r.status_code, "url": api_url}


@mcp.tool()
def health() -> dict[str, Any]:
    """Probe the Hardware DataBase API: is the server up, and are we logged in?

    Returns status = 'ok' (authed) | 'server_up' (not logged in) | 'server_down'.
    Always call this first if unsure whether the API is reachable/authed.
    """
    api_url, token = _resolve()
    return _health(api_url, token)


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Return the currently authenticated user (username, role, department)."""
    try:
        return _client().whoami()
    except ApiError as exc:
        return _err(exc)


@mcp.tool()
def list_kbs() -> list[dict[str, Any]] | dict[str, Any]:
    """List knowledge bases the current user can access (name, department, permission)."""
    try:
        return _client().list_kbs()
    except ApiError as exc:
        return _err(exc)


@mcp.tool()
def list_files(kb: str) -> list[dict[str, Any]] | dict[str, Any]:
    """List files in a knowledge base (name, status, processor_kind). Requires read permission."""
    try:
        return _client().list_files(kb)
    except ApiError as exc:
        return _err(exc)


@mcp.tool()
def query(kb: str, question: str) -> dict[str, Any]:
    """Query a hardware-design knowledge base and return a grounded answer.

    Runs the bounded LangGraph agent (question analysis -> multi-round retrieval
    -> evidence judging -> grounded synthesis) and returns a single object:
    {answer, summary (with status/evidence/rounds), footer, token_usage}.
    Use this for questions over documents (Word/PDF), spreadsheets (Excel), or
    circuit designs (EDIF/EDF netlists + schematic PDFs).
    """
    client = _client()
    answer_parts: list[str] = []
    summary: dict[str, Any] | None = None
    try:
        for event, data in client.query(kb, question):
            if event == "delta":
                answer_parts.append(data.get("text", ""))
            elif event == "done":
                summary = data
            elif event == "error":
                return {"error": data.get("message", "query failed")}
    except ApiError as exc:
        return _err(exc)
    if summary is None:
        summary = {}
    summary["answer"] = "".join(answer_parts)
    return summary


@mcp.tool()
def upload(kb: str, files: list[str], source_group: str = "") -> dict[str, Any]:
    """Upload local files to a knowledge base. Requires dept_admin role.

    Args:
        kb: Target knowledge base name.
        files: Local file paths to upload. Extension picks the pipeline
            (.doc/.docx/.pdf -> document, .xlsx -> spreadsheet, .edf/.edif -> circuit).
        source_group: Optional source group (文档资料/物料数据/设计数据/...).
            Empty string = auto-classify by extension.
    """
    try:
        return _client().upload(kb, files, source_group=source_group or None)
    except ApiError as exc:
        return _err(exc)


@mcp.tool()
def delete(kb: str, filename: str) -> dict[str, Any]:
    """Delete a file from a knowledge base. Requires system_admin role."""
    try:
        return _client().delete_file(kb, filename)
    except ApiError as exc:
        return _err(exc)


def main() -> int:
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
