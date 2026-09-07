"""Lexical index over attachment parts (design §8).

SQLite FTS5 with the default ``unicode61`` tokenizer. Availability is probed
at startup; an unavailable FTS5 module degrades to bounded scans instead of
crashing the service (design §8.4). An optional ``trigram`` table mirrors the
content for CJK/substring >= 3 char lookups when the build supports it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

import src.settings
from src.attachments.query_normalizer import build_fts_match, fts_safe_term, normalize_identifier

_FTS_PROBE_SQL = "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)"
_TRIGRAM_PROBE_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe_trigram USING fts5(x, tokenize='trigram')"
)


@dataclass
class IndexedHit:
    asset_id: str
    part_id: str
    ordinal: int
    part_type: str
    text_content: str
    locator: dict[str, Any]
    metadata: dict[str, Any]
    score: float
    backend: str  # fts5 | trigram | scan


def fts5_available(db_path: str | None = None) -> bool:
    try:
        with closing(_connect(db_path or src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)) as conn:
            conn.execute("DROP TABLE IF EXISTS _fts5_probe")
            conn.execute(_FTS_PROBE_SQL)
            conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except Exception:
        return False


def trigram_available(db_path: str | None = None) -> bool:
    try:
        with closing(_connect(db_path or src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)) as conn:
            conn.execute("DROP TABLE IF EXISTS _fts5_probe_trigram")
            conn.execute(_TRIGRAM_PROBE_SQL)
            conn.execute("DROP TABLE IF EXISTS _fts5_probe_trigram")
        return True
    except Exception:
        return False


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class AttachmentIndex:
    """FTS5-backed lexical index for one attachment database."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH
        self._fts_enabled = (
            bool(src.settings.CHAT_ATTACHMENT_FTS_ENABLED) and fts5_available(self.db_path)
        )
        self._trigram_enabled = (
            bool(src.settings.CHAT_ATTACHMENT_TRIGRAM_ENABLED) and trigram_available(self.db_path)
        )
        self._ensure_schema()

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

    @property
    def trigram_enabled(self) -> bool:
        return self._trigram_enabled

    def _ensure_schema(self) -> None:
        if not self._fts_enabled:
            return
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS attachment_parts_fts USING fts5(
                        text_content,
                        part_id UNINDEXED,
                        asset_id UNINDEXED,
                        ordinal UNINDEXED,
                        part_type UNINDEXED,
                        locator_json UNINDEXED,
                        metadata_json UNINDEXED
                    )
                    """
                )
                if self._trigram_enabled:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS attachment_parts_trigram
                        USING fts5(
                            text_content,
                            tokenize='trigram',
                            part_id UNINDEXED,
                            asset_id UNINDEXED
                        )
                        """
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def replace_asset(self, asset_id: str, parts: list[Any]) -> None:
        """(Re)index every part of an asset atomically."""
        if not self._fts_enabled:
            return
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM attachment_parts_fts WHERE asset_id = ?", (asset_id,))
                if self._trigram_enabled:
                    conn.execute(
                        "DELETE FROM attachment_parts_trigram WHERE asset_id = ?",
                        (asset_id,),
                    )
                for part in parts:
                    text = getattr(part, "text_content", "") or ""
                    if not text.strip():
                        continue
                    conn.execute(
                        """
                        INSERT INTO attachment_parts_fts (
                            text_content, part_id, asset_id, ordinal,
                            part_type, locator_json, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            text,
                            part.part_id,
                            asset_id,
                            int(part.ordinal),
                            part.part_type,
                            json.dumps(part.locator, ensure_ascii=False, default=str),
                            json.dumps(part.metadata, ensure_ascii=False, default=str),
                        ),
                    )
                    if self._trigram_enabled:
                        conn.execute(
                            "INSERT INTO attachment_parts_trigram (text_content, part_id, asset_id) VALUES (?, ?, ?)",
                            (text, part.part_id, asset_id),
                        )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def delete_asset(self, asset_id: str) -> None:
        with closing(_connect(self.db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "attachment_parts_fts" in tables:
                conn.execute(
                    "DELETE FROM attachment_parts_fts WHERE asset_id = ?",
                    (asset_id,),
                )
            if "attachment_parts_trigram" in tables:
                conn.execute(
                    "DELETE FROM attachment_parts_trigram WHERE asset_id = ?",
                    (asset_id,),
                )

    def search(
        self,
        query: str,
        *,
        asset_ids: list[str],
        limit: int = 8,
    ) -> list[IndexedHit]:
        """Sparse retrieval restricted to authorized asset ids."""
        if not self._fts_enabled or not asset_ids:
            return []
        match_expr = build_fts_match(query)
        if not match_expr:
            return []
        placeholders = ",".join("?" for _ in asset_ids)
        sql = f"""
            SELECT part_id, asset_id, ordinal, part_type, locator_json, metadata_json,
                   text_content,
                   bm25(attachment_parts_fts) AS rank
            FROM attachment_parts_fts
            WHERE attachment_parts_fts MATCH ? AND asset_id IN ({placeholders})
            ORDER BY rank
            LIMIT ?
        """
        hits: list[IndexedHit] = []
        try:
            with closing(_connect(self.db_path)) as conn:
                rows = conn.execute(
                    sql, (match_expr, *asset_ids, max(1, int(limit)))
                ).fetchall()
        except Exception:
            # Malformed MATCH or missing table: fail soft to other retrievers.
            return []
        for row in rows:
            try:
                locator = json.loads(row["locator_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                locator = {}
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            rank = float(row["rank"] or 0.0)
            hits.append(
                IndexedHit(
                    asset_id=row["asset_id"],
                    part_id=row["part_id"],
                    ordinal=int(row["ordinal"]),
                    part_type=row["part_type"],
                    text_content=row["text_content"] or "",
                    locator=locator,
                    metadata=metadata,
                    # bm25 is negative-better; convert to a positive score.
                    score=max(0.0, 1.0 / (1.0 + abs(rank))),
                    backend="fts5",
                )
            )
        return hits

    def trigram_search(
        self,
        term: str,
        *,
        asset_ids: list[str],
        limit: int = 8,
    ) -> list[IndexedHit]:
        """Substring lookup for identifiers >= 3 chars (trigram tokenizer)."""
        if not self._trigram_enabled or not asset_ids:
            return []
        term = str(term or "").strip()
        if len(term) < 3:
            return []
        quoted = fts_safe_term(term)
        if not quoted:
            return []
        placeholders = ",".join("?" for _ in asset_ids)
        sql = f"""
            SELECT t.part_id, t.asset_id, t.text_content,
                   m.ordinal, m.part_type, m.locator_json, m.metadata_json
            FROM attachment_parts_trigram t
            LEFT JOIN attachment_parts_fts m ON m.part_id = t.part_id
            WHERE attachment_parts_trigram MATCH ? AND t.asset_id IN ({placeholders})
            LIMIT ?
        """
        hits: list[IndexedHit] = []
        try:
            with closing(_connect(self.db_path)) as conn:
                rows = conn.execute(sql, (quoted, *asset_ids, max(1, int(limit)))).fetchall()
        except Exception:
            return []
        seen: set[str] = set()
        for row in rows:
            if row["part_id"] in seen:
                continue
            seen.add(row["part_id"])
            try:
                locator = json.loads(row["locator_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                locator = {}
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            hits.append(
                IndexedHit(
                    asset_id=row["asset_id"],
                    part_id=row["part_id"],
                    ordinal=int(row["ordinal"] or 0),
                    part_type=row["part_type"] or "text",
                    text_content=row["text_content"] or "",
                    locator=locator,
                    metadata=metadata,
                    score=0.9,
                    backend="trigram",
                )
            )
        return hits

    def exact_scan(
        self,
        needle: str,
        *,
        asset_ids: list[str],
        limit: int = 8,
        max_parts_scanned: int = 4000,
    ) -> list[IndexedHit]:
        """Bounded LIKE scan fallback when FTS5 is unavailable or the term is
        too short for trigram (e.g. ``R1``, ``EN``, ``FB``)."""
        if not asset_ids:
            return []
        needle = str(needle or "").strip()
        if len(needle) < 2:
            return []
        placeholders = ",".join("?" for _ in asset_ids)
        sql = f"""
            SELECT part_id, asset_id, ordinal, part_type, locator_json, metadata_json, text_content
            FROM chat_attachment_parts
            WHERE asset_id IN ({placeholders}) AND text_content LIKE ? ESCAPE '\\'
            ORDER BY ordinal, part_id
            LIMIT ?
        """
        escaped = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        hits: list[IndexedHit] = []
        try:
            with closing(_connect(self.db_path)) as conn:
                rows = conn.execute(sql, (*asset_ids, escaped, max(1, int(limit)))).fetchall()
        except Exception:
            return []
        for row in rows:
            try:
                locator = json.loads(row["locator_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                locator = {}
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            hits.append(
                IndexedHit(
                    asset_id=row["asset_id"],
                    part_id=row["part_id"],
                    ordinal=int(row["ordinal"]),
                    part_type=row["part_type"],
                    text_content=row["text_content"] or "",
                    locator=locator,
                    metadata=metadata,
                    score=0.8,
                    backend="scan",
                )
            )
        return hits

    def normalized_identifier_scan(
        self,
        identifier: str,
        *,
        asset_ids: list[str],
        limit: int = 8,
        max_parts_scanned: int = 4000,
    ) -> list[IndexedHit]:
        """Find identifiers after removing separator noise from both sides."""
        normalized_needle = normalize_identifier(identifier)
        if len(normalized_needle) < 2 or not asset_ids:
            return []
        placeholders = ",".join("?" for _ in asset_ids)
        sql = f"""
            SELECT part_id, asset_id, ordinal, part_type, locator_json, metadata_json, text_content
            FROM chat_attachment_parts
            WHERE asset_id IN ({placeholders})
            ORDER BY ordinal, part_id
            LIMIT ?
        """
        try:
            with closing(_connect(self.db_path)) as conn:
                rows = conn.execute(
                    sql,
                    (*asset_ids, max(1, int(max_parts_scanned))),
                ).fetchall()
        except Exception:
            return []
        hits: list[IndexedHit] = []
        for row in rows:
            if normalized_needle not in normalize_identifier(row["text_content"] or ""):
                continue
            try:
                locator = json.loads(row["locator_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                locator = {}
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            hits.append(
                IndexedHit(
                    asset_id=row["asset_id"],
                    part_id=row["part_id"],
                    ordinal=int(row["ordinal"] or 0),
                    part_type=row["part_type"] or "text",
                    text_content=row["text_content"] or "",
                    locator=locator,
                    metadata=metadata,
                    score=0.85,
                    backend="scan",
                )
            )
            if len(hits) >= max(1, int(limit)):
                break
        return hits
