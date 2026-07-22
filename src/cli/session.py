"""Token persistence for the CLI.

Stores ``{username, token, api_url}`` at
``~/.config/hardware-database/session.json`` (0600). Override the directory
with ``HDB_CONFIG_DIR`` for tests.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def session_dir() -> Path:
    override = os.getenv("HDB_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "hardware-database"


def session_path() -> Path:
    return session_dir() / "session.json"


def save_session(username: str, token: str, api_url: str) -> None:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"username": username, "token": token, "api_url": api_url}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def load_session() -> dict | None:
    path = session_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_session() -> None:
    path = session_path()
    if path.exists():
        path.unlink()
