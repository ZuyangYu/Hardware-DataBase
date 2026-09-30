"""In-process MCP stub server for the external-plugin integration plan (T0).

FastMCP + ``streamable_http`` on a random loopback port, started and stopped by
a context manager.  The tool set deliberately covers the POC matrix:

- ``echo``: read-only tool returning its input (sync-bridge happy path);
- ``big_text``: read-only tool returning a large payload (truncation tests);
- ``slow``: read-only tool sleeping a configurable time (timeout/cancel tests);
- ``writer``: tool without ``readOnlyHint`` (read-only gate negative case);
- ``dup_name``: placeholder for a name that collides with a local HDB tool.

The module is not named ``test_*`` so pytest does not collect it.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


_WRITE_CALLS = 0
_WRITE_LOCK = threading.Lock()


def write_call_count() -> int:
    """How many phase-2 write tools actually executed (0 before confirmation)."""

    with _WRITE_LOCK:
        return _WRITE_CALLS


def reset_write_calls() -> None:
    global _WRITE_CALLS
    with _WRITE_LOCK:
        _WRITE_CALLS = 0


def _record_write() -> None:
    global _WRITE_CALLS
    with _WRITE_LOCK:
        _WRITE_CALLS += 1


def _artifact_payload(
    filename: str, content: bytes, *, declared_sha256: str | None = None
) -> dict[str, Any]:
    return {
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "sha256": declared_sha256 or hashlib.sha256(content).hexdigest(),
        "media_type": "application/octet-stream",
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_stub_app(*, include_write_tools: bool = False) -> FastMCP:
    """Build the stub FastMCP app with the fixed tool matrix."""

    mcp = FastMCP("hdb-mcp-stub")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def echo(text: str) -> str:
        """Return the input text unchanged."""
        return text

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def big_text(size: int = 30_000) -> str:
        """Return a large deterministic text payload."""
        return "x" * max(0, int(size))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def slow(seconds: float = 2.0) -> str:
        """Sleep for the requested number of seconds, then return ``done``."""
        time.sleep(max(0.0, float(seconds)))
        return "done"

    @mcp.tool()
    def writer(text: str) -> str:
        """Simulate a tool that does not declare itself read-only."""
        return f"wrote:{text}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def dup_name(text: str) -> str:
        """Placeholder for a name colliding with a local HDB tool."""
        return text

    if include_write_tools:
        # Phase 2 write tools: return a candidate-artifact reference, never
        # touch anything on the caller's side.
        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def kicad_create_schematic(title: str = "demo", size: int = 64) -> dict:
            """Generate a candidate KiCad schematic and return an artifact reference."""
            _record_write()
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(title))[:40] or "demo"
            content = (
                f"(kicad_sch (version 20230121) (generator hdb-stub) (title \"{title}\"))\n"
                + ("x" * max(0, int(size)))
            ).encode("utf-8")
            return _artifact_payload(f"{safe}.kicad_sch", content)

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def artifact_bad_extension(title: str = "demo") -> dict:
            """Return a candidate whose extension is outside the whitelist."""
            _record_write()
            return _artifact_payload("evil.exe", b"MZ-candidate")

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def artifact_bad_hash(title: str = "demo") -> dict:
            """Return a candidate whose declared hash does not match the body."""
            _record_write()
            return _artifact_payload(
                "mismatch.kicad_sch", b"(kicad_sch)", declared_sha256="0" * 64
            )

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def artifact_empty(title: str = "demo") -> dict:
            """Return an empty candidate body."""
            _record_write()
            return _artifact_payload("empty.kicad_sch", b"")

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def artifact_huge(size: int = 4096) -> dict:
            """Return a candidate larger than the configured size cap."""
            _record_write()
            return _artifact_payload("huge.kicad_pcb", b"y" * max(1, int(size)))

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def artifact_missing(title: str = "demo") -> str:
            """Return plain text instead of an artifact reference."""
            _record_write()
            return f"no artifact for {title}"

    return mcp


class McpStubServer:
    """Context manager running the stub over ``streamable_http``."""

    def __init__(self, host: str = "127.0.0.1", *, include_write_tools: bool = False) -> None:
        self.host = host
        self.port = _free_port()
        self._mcp = build_stub_app(include_write_tools=include_write_tools)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    def __enter__(self) -> "McpStubServer":
        app = self._mcp.streamable_http_app()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="mcp-stub", daemon=True)
        self._thread.start()
        self._wait_ready()
        return self

    def _wait_ready(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server is not None and getattr(self._server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("mcp stub server did not start in time")

    def __exit__(self, *_exc) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
