"""Persistent pointer index shared by ``kb_grep`` calls.

The index stores evidence *pointers* only: id, source, locator and a compact
search text.  Full evidence bodies are not promoted here; callers still use
``evidence_read``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from typing import Any

import src.settings
from src.agents.schemas import Evidence

_INDEX_VERSION = "v2"


def _like_pattern(value: str) -> str:
    """Escape SQL ``LIKE`` wildcards so literal queries stay literal."""

    escaped = (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def records_signature(records: list[Any]) -> str:
    """Fingerprint the scoped document set so uploads invalidate the index."""

    parts = sorted(
        f"{getattr(record, 'id', '')}:{getattr(record, 'content_hash', '')}:"
        f"{getattr(record, 'status', '')}:{getattr(record, 'processor_kind', '')}"
        for record in records
    )
    raw = "\n".join([_INDEX_VERSION, *parts]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class PointerIndex:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pointers ("
                "id TEXT PRIMARY KEY, payload TEXT NOT NULL, search_text TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS pointer_fts "
                "USING fts5(id UNINDEXED, search_text, tokenize='unicode61')"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pointer_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM pointers").fetchone()[0])

    def needs_rebuild(self, signature: str) -> bool:
        """True when the persisted pointers no longer match the source records."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM pointer_meta WHERE key = 'signature'"
            ).fetchone()
            total = int(conn.execute("SELECT COUNT(*) FROM pointers").fetchone()[0])
        return total == 0 or (row[0] if row else "") != signature

    def build(self, items: list[Evidence], signature: str = "") -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM pointers")
            conn.execute("DELETE FROM pointer_fts")
            conn.execute("DELETE FROM pointer_meta")
            rows = []
            for item in items:
                search_text = " ".join(
                    str(part or "")
                    for part in (
                        item.source_name,
                        item.content,
                        json.dumps(item.locator or {}, ensure_ascii=False),
                    )
                )
                rows.append((item.id, item.model_dump_json(), search_text))
            conn.executemany("INSERT INTO pointers VALUES (?, ?, ?)", rows)
            conn.executemany(
                "INSERT INTO pointer_fts (id, search_text) VALUES (?, ?)",
                [(row[0], row[2]) for row in rows],
            )
            conn.execute(
                "INSERT INTO pointer_meta (key, value) VALUES ('signature', ?)",
                (signature,),
            )
            conn.execute("COMMIT")

    def search(self, patterns: list[str], *, regex: bool, limit: int) -> list[Evidence]:
        values = [str(value) for value in patterns if str(value or "").strip()]
        if not values:
            return []
        fetch_limit = max(int(limit) * 4, 200)
        with closing(self._connect()) as conn:
            if regex:
                compiled = [re.compile(value, re.IGNORECASE) for value in values]
                scored = []
                for rid, payload in conn.execute(
                    "SELECT id, payload FROM pointers LIMIT 5000"
                ):
                    item = Evidence.model_validate_json(payload)
                    hits = sum(len(hit.findall(item.content)) for hit in compiled)
                    if hits > 0:
                        scored.append((hits, rid, item))
                scored.sort(key=lambda row: (-row[0], row[1]))
                return [row[2] for row in scored[:limit]]

            rows: dict[str, tuple[str, str]] = {}

            fts_query = " OR ".join(
                '"' + value.replace('"', '""') + '"' for value in values
            )
            try:
                for rid, text, payload in conn.execute(
                    "SELECT f.id, p.search_text, p.payload FROM pointer_fts f "
                    "JOIN pointers p ON p.id = f.id WHERE pointer_fts MATCH ? LIMIT ?",
                    (fts_query, fetch_limit),
                ):
                    rows.setdefault(rid, (text, payload))
            except sqlite3.Error:
                pass

            # CJK/underscore tokens are not always split by unicode61; LIKE adds
            # the pointers FTS cannot see (e.g. ``VCC3V3_EQ`` circuit nets).
            for value in values:
                pattern = _like_pattern(value)
                for rid, text, payload in conn.execute(
                    "SELECT id, search_text, payload FROM pointers "
                    "WHERE search_text LIKE ? ESCAPE '\\' LIMIT ?",
                    (pattern, fetch_limit),
                ):
                    rows.setdefault(rid, (text, payload))

            scored = []
            for rid, (text, payload) in rows.items():
                hits = sum(
                    len(re.findall(re.escape(value), text, re.IGNORECASE))
                    for value in values
                )
                if hits > 0:
                    scored.append((hits, rid, payload))
            scored.sort(key=lambda row: (-row[0], row[1]))
            return [Evidence.model_validate_json(row[2]) for row in scored[:limit]]


def pointer_index_path(department_id: str | int, kb_name: str) -> str:
    raw = f"{department_id}\0{kb_name}".encode("utf-8")
    key = hashlib.sha256(raw).hexdigest()[:24]
    return os.path.join(src.settings.STORAGE_DIR, "pointer_index", f"{key}.db")


__all__ = ["PointerIndex", "pointer_index_path", "records_signature"]
