#!/usr/bin/env python3
"""Agent-facing wrapper around the ``hardware-database`` CLI.

Always emits JSON. Used by the ``hardware-database`` Claude Code skill so the
agent gets one stable entry point instead of memorising CLI flags. Resolves
auth/url the same way the CLI does (``HDB_TOKEN`` / ``HDB_API_URL`` / the
session saved by ``hardware-database login``); the wrapper only forces
``--json`` and adds a ``health`` probe the CLI itself does not have.

Invoke from the skill root::

    cd {baseDir} && python3 scripts/hdb.py <subcommand> [args...]

Subcommands: health / whoami / kbs / files / query / upload / delete.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000"
SUBCOMMANDS = ["health", "whoami", "kbs", "files", "query", "upload", "delete"]


def _api_url() -> str:
    return os.getenv("HDB_API_URL", DEFAULT_URL).rstrip("/")


def _repo_root() -> str:
    # scripts/ <- hardware-database/ <- skills/ <- .claude/ <- repo root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _cli() -> list[str]:
    """Prefer the installed console script; fall back to ``uv run`` in the repo."""
    if shutil.which("hardware-database"):
        return ["hardware-database"]
    return ["uv", "run", "--project", _repo_root(), "hardware-database"]


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _run(cli_args: list[str]) -> int:
    cmd = _cli() + ["--api-url", _api_url(), "--json"] + cli_args
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        _emit({"error": "hardware-database CLI not found", "hint": f"run `uv sync` in {_repo_root()}"})
        return 127


def health() -> int:
    """Three-state probe: server_down / server_up (unauthed) / ok (authed)."""
    url = _api_url() + "/whoami"
    req = urllib.request.Request(url)
    token = os.getenv("HDB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            _emit({"status": "ok", "authed": True, "url": _api_url(), "user": json.loads(r.read())})
            return 0
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _emit({"status": "server_up", "authed": False, "url": _api_url(),
                   "hint": "run `hardware-database login --user <u>` (or set HDB_TOKEN)"})
            return 0
        _emit({"status": "error", "code": e.code, "url": _api_url()})
        return 1
    except urllib.error.URLError:
        _emit({"status": "server_down", "url": _api_url(),
               "hint": "start the API: `uv run hardware-database-server` (HDB_API_HOST/PORT)"})
        return 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        _emit({"subcommands": SUBCOMMANDS,
               "usage": "python3 scripts/hdb.py <subcommand> [args...]"})
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "health":
        return health()
    # Wrapper subcommand -> underlying CLI subcommand. `rest` carries the real
    # flags (--kb / --group / --file / question / file paths) verbatim.
    mapping = {"kbs": ["list-kb"], "whoami": ["whoami"],
               "files": ["list-files"], "query": ["query"],
               "upload": ["upload"], "delete": ["delete"]}
    if cmd in mapping:
        return _run(mapping[cmd] + rest)
    _emit({"error": f"unknown subcommand: {cmd}", "subcommands": SUBCOMMANDS})
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
