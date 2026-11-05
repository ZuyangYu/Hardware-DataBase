"""HDB MCP bridge.

Exposes the platform to external agents (dcode/OpenCode/Claude Code) over
MCP.  The tools are thin HTTP clients so the bridge never bypasses the
platform's auth, knowledge-scope and task governance.

Run over stdio:

    HDB_API_BASE=http://127.0.0.1:8002/api/v1 \
    HDB_API_TOKEN=... \
    uv run python -m src.mcp_bridge.server

Tools:
- list_kbs
- create_task / get_task / list_task_artifacts
- ask_knowledge_base (runs one KB turn and returns the cited answer)
- list_spreadsheets / list_circuit_designs (structured read models)
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hdb-knowledge")

_API_BASE = os.getenv("HDB_API_BASE", "http://127.0.0.1:8002/api/v1").rstrip("/")
_TOKEN = os.getenv("HDB_API_TOKEN", "").strip()
_USERNAME = os.getenv("HDB_USERNAME", "").strip()
_PASSWORD = os.getenv("HDB_PASSWORD", "")
_TIMEOUT = float(os.getenv("HDB_MCP_TIMEOUT", "300"))

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
    if _USERNAME and _PASSWORD:
        response = _client().post(
            f"{_API_BASE}/login",
            json={"username": _USERNAME, "password": _PASSWORD},
        )
        response.raise_for_status()
        _TOKEN = str(response.json()["token"])
        return _TOKEN
    raise RuntimeError(
        "HDB_API_TOKEN or HDB_USERNAME/HDB_PASSWORD is required for the MCP bridge"
    )


def _request(method: str, path: str, **kwargs: Any) -> Any:
    headers = {"Authorization": f"Bearer {_token()}"}
    response = _client().request(
        method, f"{_API_BASE}{path}", headers=headers, **kwargs
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"HDB API {response.status_code}: {response.text[:300]}"
        )
    return response.json() if response.content else {}


def _kb_path(kb_name: str) -> str:
    return quote(str(kb_name or ""), safe="")


@mcp.tool()
def list_kbs() -> list[dict[str, Any]]:
    """List knowledge bases visible to the configured account."""
    return _request("GET", "/kbs")


@mcp.tool()
def create_task(
    query: str,
    kb_name: str = "__general__",
    definition_id: str = "",
    title: str = "",
) -> dict[str, Any]:
    """Create a durable general-agent task and return its id/status.

    ``definition_id`` optionally selects an external task definition under
    ``TASK_DEFINITION_DIR``.  Use ``get_task`` to poll and
    ``list_task_artifacts`` to fetch deliverables.
    """
    payload: dict[str, Any] = {"query": query, "kb_name": kb_name}
    if definition_id:
        payload["definition_id"] = definition_id
    if title:
        payload["title"] = title
    return _request("POST", "/tasks", json=payload)


@mcp.tool()
def get_task(task_id: str) -> dict[str, Any]:
    """Get task status, current answer, plan, verification and artifacts."""
    return _request("GET", f"/tasks/{quote(str(task_id), safe='')}")


@mcp.tool()
def list_task_artifacts(task_id: str) -> dict[str, Any]:
    """List downloadable artifacts produced by a task."""
    return _request(
        "GET", f"/tasks/{quote(str(task_id), safe='')}/artifacts"
    )


@mcp.tool()
def ask_knowledge_base(
    question: str,
    kb_name: str,
    timeout_seconds: float = _TIMEOUT,
) -> dict[str, Any]:
    """Ask one question against a mounted knowledge base and wait for the
    cited answer.  This uses the platform's normal turn governance; it is not
    a raw vector-search API.
    """
    session = _request(
        "POST",
        "/conversations",
        json={"kb_name": kb_name, "title": question[:40] or "MCP question"},
    )
    session_id = int(session["id"])
    turn_payload = _request(
        "POST",
        f"/conversations/{session_id}/turns",
        json={"query": question, "source_scope": "knowledge_base_only"},
    )
    turn_id = str(turn_payload["turn"]["id"])
    _request("POST", f"/turns/{turn_id}/start")

    deadline = time.monotonic() + max(10.0, float(timeout_seconds))
    status = "pending"
    while time.monotonic() < deadline:
        turn = _request("GET", f"/turns/{turn_id}")
        status = str(turn.get("status") or "")
        if status in {"completed", "failed", "cancelled"}:
            summary = turn.get("summary") or {}
            return {
                "status": status,
                "answer": turn.get("answer") or "",
                "error_message": turn.get("error_message") or "",
                "evidence_count": len(summary.get("evidence") or []),
                "turn_id": turn_id,
                "session_id": session_id,
            }
        time.sleep(2)
    return {
        "status": "timeout",
        "answer": "",
        "error_message": f"turn still {status} after {timeout_seconds}s",
        "turn_id": turn_id,
        "session_id": session_id,
    }


@mcp.tool()
def list_spreadsheets(kb_name: str) -> dict[str, Any]:
    """List structured spreadsheet sources in one knowledge base."""
    return _request("GET", f"/kbs/{_kb_path(kb_name)}/structured/spreadsheets")


@mcp.tool()
def list_circuit_designs(kb_name: str) -> dict[str, Any]:
    """List circuit/netlist designs in one knowledge base."""
    return _request("GET", f"/kbs/{_kb_path(kb_name)}/structured/circuit-designs")


if __name__ == "__main__":
    mcp.run()
