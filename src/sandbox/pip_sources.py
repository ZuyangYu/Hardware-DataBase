"""Administrator-configured, operator-host-allowlisted dependency indexes."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit

import src.settings as settings


class SkillPipSourceError(ValueError):
    pass


def validate_index_url(value: str, *, allowed_hosts: str) -> str:
    url = str(value or "").strip().rstrip("/") + "/" if value else ""
    if not url:
        return ""
    if len(url) > 512 or any(ord(ch) < 33 or ord(ch) > 126 for ch in url):
        raise SkillPipSourceError("dependency index URL must be bounded ASCII HTTPS")
    parsed = urlsplit(url)
    hosts = {host.strip().lower() for host in str(allowed_hosts or "").split(",") if host.strip()}
    if (parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in hosts
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9._~/-]*", parsed.path)
            or ".." in parsed.path or "//" in parsed.path):
        raise SkillPipSourceError("dependency index URL is not approved for this deployment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SkillPipSourceError("dependency index port is invalid") from exc
    if port not in (None, 443):
        raise SkillPipSourceError("dependency index must use the HTTPS default port")
    return url


class SkillPipSourceStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = os.path.abspath(str(db_path or settings.SANDBOX_TASK_DB_PATH))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sandbox_skill_pip_source "
                         "(id INTEGER PRIMARY KEY CHECK(id=1), index_url TEXT NOT NULL)")

    def get(self) -> str:
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
            row = conn.execute("SELECT index_url FROM sandbox_skill_pip_source WHERE id=1").fetchone()
        return str(row[0]) if row else ""

    def set(self, url: str) -> str:
        approved = validate_index_url(
            url, allowed_hosts=getattr(settings, "SANDBOX_SKILL_PIP_ALLOWED_HOSTS", "pypi.org")
        )
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
            conn.execute("INSERT INTO sandbox_skill_pip_source(id,index_url) VALUES (1,?) "
                         "ON CONFLICT(id) DO UPDATE SET index_url=excluded.index_url", (approved,))
            conn.commit()
        return approved


__all__ = ["SkillPipSourceStore", "SkillPipSourceError", "validate_index_url"]
