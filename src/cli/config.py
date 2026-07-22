"""API URL resolution for the CLI.

Priority: ``--api-url`` flag > ``HDB_API_URL`` env > value stored in the
session file > built-in default.
"""
from __future__ import annotations

import os

DEFAULT_API_URL = "http://127.0.0.1:8000"


def resolve_api_url(cli_arg: str | None = None, session_url: str | None = None) -> str:
    if cli_arg:
        return cli_arg.rstrip("/")
    env = os.getenv("HDB_API_URL")
    if env:
        return env.rstrip("/")
    if session_url:
        return session_url.rstrip("/")
    return DEFAULT_API_URL
