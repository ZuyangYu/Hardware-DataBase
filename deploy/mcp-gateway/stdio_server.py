#!/usr/bin/env python3
"""Fixed, read-only stdio MCP server (standard library only).

This is the reviewed stdio MCP server hosted by the Sandbox Runner service
profile and bridged by the MCP Gateway.  It speaks line-delimited JSON-RPC 2.0
over stdin/stdout and exposes exactly one read-only tool.

It has no network, filesystem, shell or environment access beyond its own
process; the runtime isolation is provided by the Runner profile.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "hdb-sandbox-stdio-mcp"
SERVER_VERSION = "1.0.0"

TOOLS = [
    {
        "name": "sandbox_runtime_info",
        "description": "Return read-only information about the isolated runtime.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "maxLength": 64},
            },
            "required": ["label"],
            "additionalProperties": False,
        },
    }
]


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name != "sandbox_runtime_info":
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    label = str(arguments.get("label", ""))[:64]
    payload = {
        "label": label,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "network": "none",
        "read_only_rootfs": True,
        "server": SERVER_NAME,
    }
    return _text_result(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        result = _call_tool(str(params.get("name") or ""), dict(params.get("arguments") or {}))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "method not found"},
    }


def main() -> None:
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        response = _handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
