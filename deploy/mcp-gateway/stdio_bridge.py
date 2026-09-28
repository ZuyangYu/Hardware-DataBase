#!/usr/bin/env python3
"""HTTP bridge for the fixed stdio MCP server (runs inside the service container).

The Gateway never spawns third-party processes and never controls the runtime;
it talks HTTP to this bridge, which the Sandbox Runner started as a service
profile.  The bridge spawns the reviewed stdio MCP server as a child with a
fixed argv and forwards JSON-RPC over stdin/stdout.

Endpoints (loopback only inside the container network namespace):

- ``GET /health``            -> {"status": "up"}
- ``GET /tools``             -> {"tools": [...]} from MCP ``tools/list``
- ``POST /invoke``           -> {"result": {...}} from MCP ``tools/call``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = int(os.getenv("MCP_BRIDGE_PORT", "8080"))
SERVER_PATH = os.getenv("MCP_STDIO_SERVER", "/opt/mcp/stdio_server.py")
MAX_BODY_BYTES = 65536

_lock = threading.Lock()
_next_id = 0
_process: subprocess.Popen[str] | None = None


def _start_child() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, SERVER_PATH],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    global _next_id, _process
    with _lock:
        if _process is None or _process.poll() is not None:
            _process = _start_child()
            _initialize(_process)
        _next_id += 1
        request_id = _next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        assert _process.stdin is not None and _process.stdout is not None
        _process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        _process.stdin.flush()
        while True:
            line = _process.stdout.readline()
            if not line:
                raise RuntimeError("stdio MCP server closed the stream")
            try:
                response = json.loads(line)
            except ValueError:
                continue
            if isinstance(response, dict) and response.get("id") == request_id:
                return response


def _initialize(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None and process.stdout is not None
    request = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "hdb-mcp-bridge", "version": "1.0.0"},
        },
    }
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    process.stdout.readline()
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    process.stdin.flush()


class Handler(BaseHTTPRequestHandler):
    server_version = "hdb-mcp-bridge/1.0"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"status": "up"})
            return
        if self.path == "/tools":
            try:
                response = _rpc("tools/list")
                tools = (response.get("result") or {}).get("tools") or []
                self._json(200, {"tools": tools})
            except Exception:  # noqa: BLE001 - bounded bridge error
                self._json(502, {"error": "stdio server unavailable"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/invoke":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(400, {"error": "invalid body"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            name = str(body.get("name") or "")
            arguments = dict(body.get("arguments") or {})
        except (ValueError, TypeError):
            self._json(400, {"error": "invalid body"})
            return
        try:
            response = _rpc("tools/call", {"name": name, "arguments": arguments})
        except Exception:  # noqa: BLE001
            self._json(502, {"error": "stdio server unavailable"})
            return
        if "error" in response:
            self._json(400, {"error": str(response["error"].get("message") or "tool error")})
            return
        self._json(200, {"result": response.get("result") or {}})


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
