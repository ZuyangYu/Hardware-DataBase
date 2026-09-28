"""Reference internal KiCad MCP service (streamable_http).

This is the deployable example from the MCP integration plan §4.12: a thin,
reviewable wrapper that exposes exactly two governed tools and never accepts
shell text, arbitrary commands or host paths from the caller.

- ``kicad_analyze_schematic``  parameter-based example summary (``readOnlyHint=True``);
- ``kicad_create_schematic``   candidate-artifact generator (write channel).

The write tool returns the candidate-artifact contract expected by HDB
(``filename`` / ``content_base64`` / ``sha256`` / ``media_type``) and never
modifies any existing file: the caller decides what to do with the bytes.

Production deployments can replace the in-process generator with ``kicad-cli``
invocations using a fixed argv list (``subprocess.run([...], shell=False)``);
the reference intentionally ships without subprocess calls.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

HOST = os.getenv("KICAD_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("KICAD_MCP_PORT", "8080"))

mcp = FastMCP("hdb-kicad-mcp", host=HOST, port=PORT)


def _deterministic_uuid(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _symbol_block(index: int) -> str:
    x = 60 + (index % 4) * 40
    y = 60 + (index // 4) * 40
    return (
        "  (symbol (lib_id \"Device:R\") (at "
        f"{x} {y} 0) (unit 1)\n"
        f"    (uuid {_deterministic_uuid(f'R{index}')})\n"
        f"    (property \"Reference\" \"R{index + 1}\" (at {x} {y} 0))\n"
        f"    (property \"Value\" \"10k\" (at {x} {y} 0))\n"
        "  )\n"
    )


def _schematic_text(title: str, symbol_count: int) -> str:
    symbols = "".join(_symbol_block(index) for index in range(symbol_count))
    return (
        "(kicad_sch (version 20230121) (generator hdb-kicad-mcp)\n"
        f"  (uuid {_deterministic_uuid(title)})\n"
        "  (paper \"A4\")\n"
        "  (title_block\n"
        f"    (title \"{title}\")\n"
        "    (rev \"A\")\n"
        "  )\n"
        "  (lib_symbols)\n"
        f"{symbols}"
        "  (sheet_instances\n"
        "    (path \"/\" (page \"1\"))\n"
        "  )\n"
        ")\n"
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def kicad_analyze_schematic(title: str, symbol_count: int = 8) -> str:
    """Return a bounded example summary based only on the provided parameters.

    This reference implementation does not read or analyze schematic files,
    invoke KiCad, or run ERC.
    """

    count = max(0, min(int(symbol_count), 64))
    return (
        f"schematic '{title}': {count} symbols, 0 nets, single A4 sheet; "
        "ERC not run (reference service); no external file was read or modified."
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def kicad_create_schematic(title: str, symbol_count: int = 8) -> dict[str, Any]:
    """Generate a candidate schematic and return an artifact reference.

    The caller (HDB) validates extension/size/SHA-256 and stores the bytes as a
    candidate artifact. This tool never overwrites an existing design file.
    """

    count = max(1, min(int(symbol_count), 64))
    content = _schematic_text(str(title)[:120], count).encode("utf-8")
    return {
        "filename": "candidate.kicad_sch",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": "application/x-kicad-schematic",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")  # endpoint: http://<host>:<port>/mcp
