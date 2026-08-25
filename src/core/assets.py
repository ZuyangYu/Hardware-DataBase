"""Hardware asset master data and reviewable extraction candidates.

This module deliberately keeps AI output separate from trusted asset records.
An extractor creates ``pending`` candidates with source evidence; a user with
write access must accept one before it becomes an asset.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import config.settings
from src.core.model_factory import create_chat_model


ASSET_TYPES = {"device", "board", "component", "firmware", "other"}
CANDIDATE_STATES = {"pending", "accepted", "rejected"}
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_MODEL_PATTERN = re.compile(
    r"(?:型号|model|part\s*(?:number|no)?|p/?n|料号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9._/+-]{1,63})",
    re.IGNORECASE,
)
_VERSION_PATTERN = re.compile(r"(?:版本|version|rev(?:ision)?)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9._+-]{0,31})", re.IGNORECASE)
_MANUFACTURER_PATTERN = re.compile(r"(?:厂商|制造商|manufacturer|vendor)\s*[:：#]?\s*([^\n,，;；]{2,80})", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AssetSource:
    file_id: str
    file_name: str
    processor_kind: str = ""
    dataset_kind: str = ""
    metadata: dict[str, Any] | None = None
    excerpt: str = ""
    locator: str = ""
    source_category: str = ""
    extraction_target: str = ""


@dataclass(frozen=True)
class SourceProfile:
    """Semantic dispatch for a parsed source before any model call."""

    category: str
    extraction_target: str
    asset_eligible: bool


def classify_asset_source(file_name: str, processor_kind: str = "", dataset_kind: str = "") -> SourceProfile:
    """Classify sources by the information they can truthfully contribute.

    A requirement document is evidence for constraints, not an asset record.
    Keeping that distinction here stops a generic extraction prompt from
    turning every specification into a fictitious device.
    """
    name = str(file_name or "").lower()
    processor = str(processor_kind or "").lower()
    dataset = str(dataset_kind or "").lower()
    if "circuit" in processor or "edf" in name:
        return SourceProfile("circuit_design", "board_components_topology", True)
    if any(token in name for token in ("架构", "architecture", "架构设计")):
        return SourceProfile("hardware_architecture", "assets_relations", True)
    if any(token in name for token in ("需求", "requirement", "specification")):
        return SourceProfile("hardware_requirement", "requirements_constraints", False)
    if "spreadsheet" in processor or dataset in {"table", "spreadsheet"} or name.endswith((".xlsx", ".xls", ".csv")):
        return SourceProfile("structured_table", "bom_assets_parameters", True)
    return SourceProfile("document_rag", "document_assets", True)


class AssetService:
    """SQLite-backed asset domain, colocated with auth/KB ownership data."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.settings.AUTH_DB_PATH
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS hardware_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    department_id INTEGER NOT NULL,
                    kb_id INTEGER NOT NULL,
                    asset_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    manufacturer TEXT NOT NULL DEFAULT '',
                    serial_number TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    owner_user_id INTEGER,
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    created_from_candidate_id INTEGER,
                    created_by_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id),
                    FOREIGN KEY(owner_user_id) REFERENCES users(id),
                    FOREIGN KEY(created_by_user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_hardware_assets_scope
                    ON hardware_assets(kb_id, department_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS asset_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    department_id INTEGER NOT NULL,
                    kb_id INTEGER NOT NULL,
                    kb_name TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    source_kind TEXT NOT NULL DEFAULT '',
                    source_fingerprint TEXT NOT NULL,
                    extraction_method TEXT NOT NULL DEFAULT 'rule',
                    asset_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    manufacturer TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL DEFAULT '',
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    evidence_excerpt TEXT NOT NULL DEFAULT '',
                    evidence_locator TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    asset_id INTEGER,
                    resolved_by_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id),
                    FOREIGN KEY(asset_id) REFERENCES hardware_assets(id),
                    FOREIGN KEY(resolved_by_user_id) REFERENCES users(id),
                    UNIQUE(kb_id, file_id)
                );
                CREATE INDEX IF NOT EXISTS idx_asset_candidates_scope
                    ON asset_candidates(kb_id, status, created_at DESC);

                CREATE TABLE IF NOT EXISTS asset_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    locator TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES hardware_assets(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_asset_evidence_asset ON asset_evidence(asset_id);
                """
            )

    def list_assets(self, *, kb_id: int, department_id: int, query: str = "") -> list[dict[str, Any]]:
        needle = f"%{query.strip()}%"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT a.*, COUNT(e.id) AS evidence_count
                FROM hardware_assets a
                LEFT JOIN asset_evidence e ON e.asset_id = a.id
                WHERE a.kb_id = ? AND a.department_id = ?
                  AND (? = '' OR a.name LIKE ? OR a.model LIKE ? OR a.manufacturer LIKE ?)
                GROUP BY a.id
                ORDER BY a.updated_at DESC, a.id DESC
                """,
                (kb_id, department_id, query.strip(), needle, needle, needle),
            ).fetchall()
        return [self._asset_row(row) for row in rows]

    def get_asset(self, *, asset_id: int, kb_id: int, department_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT a.*, COUNT(e.id) AS evidence_count
                FROM hardware_assets a LEFT JOIN asset_evidence e ON e.asset_id = a.id
                WHERE a.id = ? AND a.kb_id = ? AND a.department_id = ?
                GROUP BY a.id
                """,
                (asset_id, kb_id, department_id),
            ).fetchone()
            if row is None:
                return None
            result = self._asset_row(row)
            evidence = conn.execute(
                "SELECT * FROM asset_evidence WHERE asset_id = ? ORDER BY id DESC", (asset_id,)
            ).fetchall()
        result["evidence"] = [self._evidence_row(item) for item in evidence]
        return result

    def list_candidates(self, *, kb_id: int, department_id: int, status: str = "pending") -> list[dict[str, Any]]:
        if status and status not in CANDIDATE_STATES:
            raise ValueError("invalid candidate status")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM asset_candidates
                WHERE kb_id = ? AND department_id = ? AND (? = '' OR status = ?)
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC, id DESC
                """,
                (kb_id, department_id, status, status),
            ).fetchall()
        return [self._candidate_row(row) for row in rows]

    def list_file_links(
        self,
        *,
        kb_id: int,
        department_id: int,
        files: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project existing parsed files into the asset domain.

        Files are source records before an AI candidate exists. This makes the
        input corpus visible and prevents the UI from implying that a document
        only exists after somebody has accepted an asset suggestion.
        """
        with closing(self._connect()) as conn:
            candidate_rows = conn.execute(
                "SELECT id, file_id, status, asset_id FROM asset_candidates WHERE kb_id = ? AND department_id = ?",
                (kb_id, department_id),
            ).fetchall()
            evidence_rows = conn.execute(
                """
                SELECT e.file_id, a.id AS asset_id, a.name AS asset_name
                FROM asset_evidence e
                JOIN hardware_assets a ON a.id = e.asset_id
                WHERE a.kb_id = ? AND a.department_id = ?
                """,
                (kb_id, department_id),
            ).fetchall()
        candidates = {str(row["file_id"]): row for row in candidate_rows}
        evidence_by_file = {str(row["file_id"]): row for row in evidence_rows}
        result: list[dict[str, Any]] = []
        for file in files:
            file_id = str(file.get("id") or "")
            candidate = candidates.get(file_id)
            evidence = evidence_by_file.get(file_id)
            if evidence is not None:
                link_status = "linked"
            elif candidate is not None and candidate["status"] == "pending":
                link_status = "pending_review"
            elif candidate is not None and candidate["status"] == "rejected":
                link_status = "ignored"
            else:
                link_status = "unprocessed"
            result.append(
                {
                    "file_id": file_id,
                    "file_name": str(file.get("name") or ""),
                    "file_status": str(file.get("status") or ""),
                    "processor_kind": str(file.get("processor_kind") or ""),
                    "dataset_kind": str(file.get("dataset_kind") or ""),
                    "link_status": link_status,
                    "candidate_id": int(candidate["id"]) if candidate is not None else None,
                    "asset_id": int(evidence["asset_id"]) if evidence is not None else None,
                    "asset_name": str(evidence["asset_name"]) if evidence is not None else "",
                    "source_category": str(file.get("source_category") or "document_rag"),
                    "extraction_target": str(file.get("extraction_target") or "document_assets"),
                    "asset_eligible": bool(file.get("asset_eligible", True)),
                }
            )
        return result

    def generate_candidate(
        self,
        *,
        kb_id: int,
        department_id: int,
        kb_name: str,
        source: AssetSource,
    ) -> tuple[dict[str, Any], bool]:
        payload, method = self._extract(source)
        fingerprint = hashlib.sha256(
            json.dumps({"payload": payload, "excerpt": source.excerpt[:12000]}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with closing(self._connect()) as conn:
            # Older local installs created this table before its one-source /
            # one-candidate constraint was introduced.  A transaction gives us
            # the same idempotency without relying on a migration-sensitive
            # ON CONFLICT target.
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM asset_candidates WHERE kb_id = ? AND file_id = ? ORDER BY id DESC LIMIT 1",
                    (kb_id, source.file_id),
                ).fetchone()
                values = (
                    source.source_category or source.processor_kind or source.dataset_kind, fingerprint, method,
                    payload["asset_type"], payload["name"], payload["model"], payload["manufacturer"],
                    payload["version"], json.dumps(payload["attributes"], ensure_ascii=False),
                    source.excerpt[:4000], source.locator[:500], payload["confidence"], now,
                )
                if row is not None and row["status"] == "pending":
                    conn.execute(
                        """
                        UPDATE asset_candidates SET
                            source_kind = ?, source_fingerprint = ?, extraction_method = ?, asset_type = ?,
                            name = ?, model = ?, manufacturer = ?, version = ?, attributes_json = ?,
                            evidence_excerpt = ?, evidence_locator = ?, confidence = ?, created_at = ?
                        WHERE id = ?
                        """,
                        (*values, row["id"]),
                    )
                    row = conn.execute("SELECT * FROM asset_candidates WHERE id = ?", (row["id"],)).fetchone()
                elif row is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO asset_candidates (
                            department_id, kb_id, kb_name, file_id, file_name, source_kind, source_fingerprint,
                            extraction_method, asset_type, name, model, manufacturer, version, attributes_json,
                            evidence_excerpt, evidence_locator, confidence, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            department_id, kb_id, kb_name, source.file_id, source.file_name,
                            *values,
                        ),
                    )
                    row = conn.execute("SELECT * FROM asset_candidates WHERE id = ?", (cursor.lastrowid,)).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self._candidate_row(row), method == "llm"

    def accept_candidate(
        self,
        *,
        candidate_id: int,
        kb_id: int,
        department_id: int,
        actor_user_id: int,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        overrides = overrides or {}
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                candidate = conn.execute(
                    "SELECT * FROM asset_candidates WHERE id = ? AND kb_id = ? AND department_id = ?",
                    (candidate_id, kb_id, department_id),
                ).fetchone()
                if candidate is None:
                    raise LookupError("asset candidate not found")
                if candidate["status"] == "accepted" and candidate["asset_id"]:
                    asset = conn.execute("SELECT * FROM hardware_assets WHERE id = ?", (candidate["asset_id"],)).fetchone()
                    conn.execute("COMMIT")
                    return self._asset_row(asset)
                if candidate["status"] != "pending":
                    raise ValueError("only pending candidates can be accepted")
                values = self._candidate_values(candidate, overrides)
                now = utc_now()
                cursor = conn.execute(
                    """
                    INSERT INTO hardware_assets (
                        department_id, kb_id, asset_type, name, model, manufacturer, serial_number, version,
                        status, owner_user_id, attributes_json, created_from_candidate_id, created_by_user_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        department_id, kb_id, values["asset_type"], values["name"], values["model"],
                        values["manufacturer"], values["serial_number"], values["version"], values["status"],
                        values["owner_user_id"], json.dumps(values["attributes"], ensure_ascii=False),
                        candidate_id, actor_user_id, now, now,
                    ),
                )
                asset_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    INSERT INTO asset_evidence (asset_id, file_id, file_name, locator, excerpt, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id, candidate["file_id"], candidate["file_name"], candidate["evidence_locator"],
                        candidate["evidence_excerpt"], json.dumps({"candidate_id": candidate_id, "source_kind": candidate["source_kind"]}, ensure_ascii=False), now,
                    ),
                )
                conn.execute(
                    "UPDATE asset_candidates SET status = 'accepted', asset_id = ?, resolved_by_user_id = ?, resolved_at = ? WHERE id = ?",
                    (asset_id, actor_user_id, now, candidate_id),
                )
                asset = conn.execute("SELECT a.*, 1 AS evidence_count FROM hardware_assets a WHERE a.id = ?", (asset_id,)).fetchone()
                conn.execute("COMMIT")
                return self._asset_row(asset)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def reject_candidate(self, *, candidate_id: int, kb_id: int, department_id: int, actor_user_id: int) -> None:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE asset_candidates SET status = 'rejected', resolved_by_user_id = ?, resolved_at = ?
                WHERE id = ? AND kb_id = ? AND department_id = ? AND status = 'pending'
                """,
                (actor_user_id, utc_now(), candidate_id, kb_id, department_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("pending asset candidate not found")

    def _extract(self, source: AssetSource) -> tuple[dict[str, Any], str]:
        fallback = self._rule_extract(source)
        if not source.excerpt.strip():
            return fallback, "rule"
        prompt = (
            "你是硬件资料结构化助手。仅根据给出的资料片段提取一个最主要的硬件资产候选，"
            "不得猜测不存在的事实。只返回 JSON 对象，不要 Markdown。字段必须是 "
            "asset_type(device|board|component|firmware|other)、name、model、manufacturer、version、"
            "attributes(对象)、confidence(0 到 1)。name 必须非空；无法确认的字段填空字符串。\n\n"
            f"文件名：{source.file_name}\n资料类型：{source.source_category or source.processor_kind or source.dataset_kind or 'document'}\n"
            f"提取目标：{source.extraction_target or 'asset'}\n"
            f"资料片段：\n{source.excerpt[:12000]}"
        )
        try:
            response = create_chat_model().invoke(
                [{"role": "system", "content": "输出严格 JSON，不解释。"}, {"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0,
                timeout=30,
            )
            raw = getattr(response, "content", "")
            if isinstance(raw, list):
                raw = "".join(
                    str(block.get("text") or "") for block in raw if isinstance(block, dict)
                )
            parsed = self._parse_model_json(str(raw))
            if parsed is not None:
                return self._normalise_payload(parsed, fallback), "llm"
        except Exception:
            # A file remains reviewable even during model outages; the method is
            # recorded so the UI never presents a heuristic as model output.
            pass
        return fallback, "rule"

    @staticmethod
    def _parse_model_json(raw: str) -> dict[str, Any] | None:
        match = _JSON_OBJECT.search(raw.strip())
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _rule_extract(self, source: AssetSource) -> dict[str, Any]:
        stem = os.path.splitext(os.path.basename(source.file_name))[0].strip() or "未命名硬件资料"
        text = source.excerpt
        model = self._first_match(_MODEL_PATTERN, text)
        version = self._first_match(_VERSION_PATTERN, text)
        manufacturer = self._first_match(_MANUFACTURER_PATTERN, text)
        kind = (source.processor_kind or source.dataset_kind).lower()
        if "circuit" in kind or any(token in stem.lower() for token in ("pcb", "board", "schematic", "原理图")):
            asset_type = "board"
        elif "spreadsheet" in kind or any(token in stem.lower() for token in ("bom", "器件", "parts")):
            asset_type = "component"
        elif any(token in stem.lower() for token in ("firmware", "固件")):
            asset_type = "firmware"
        else:
            asset_type = "device"
        return {
            "asset_type": asset_type,
            "name": model or stem,
            "model": model or "",
            "manufacturer": manufacturer or "",
            "version": version or "",
            "attributes": {"source_processor": source.processor_kind or source.dataset_kind} if (source.processor_kind or source.dataset_kind) else {},
            "confidence": 0.42 if model else 0.25,
        }

    @staticmethod
    def _first_match(pattern: re.Pattern[str], text: str) -> str:
        match = pattern.search(text or "")
        return match.group(1).strip() if match else ""

    def _normalise_payload(self, payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("asset_type") or fallback["asset_type"]).lower().strip()
        attributes = payload.get("attributes")
        confidence = payload.get("confidence", fallback["confidence"])
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = fallback["confidence"]
        return {
            "asset_type": kind if kind in ASSET_TYPES else fallback["asset_type"],
            "name": str(payload.get("name") or fallback["name"]).strip()[:200],
            "model": str(payload.get("model") or "").strip()[:200],
            "manufacturer": str(payload.get("manufacturer") or "").strip()[:200],
            "version": str(payload.get("version") or "").strip()[:100],
            "attributes": attributes if isinstance(attributes, dict) else {},
            "confidence": confidence,
        }

    def _candidate_values(self, candidate: sqlite3.Row, overrides: dict[str, Any]) -> dict[str, Any]:
        attributes = self._json_object(candidate["attributes_json"])
        if isinstance(overrides.get("attributes"), dict):
            attributes.update(overrides["attributes"])
        kind = str(overrides.get("asset_type") or candidate["asset_type"]).strip().lower()
        if kind not in ASSET_TYPES:
            raise ValueError("invalid asset type")
        name = str(overrides.get("name") or candidate["name"]).strip()[:200]
        if not name:
            raise ValueError("asset name is required")
        return {
            "asset_type": kind,
            "name": name,
            "model": str(overrides.get("model") if "model" in overrides else candidate["model"]).strip()[:200],
            "manufacturer": str(overrides.get("manufacturer") if "manufacturer" in overrides else candidate["manufacturer"]).strip()[:200],
            "serial_number": str(overrides.get("serial_number") or "").strip()[:200],
            "version": str(overrides.get("version") if "version" in overrides else candidate["version"]).strip()[:100],
            "status": str(overrides.get("status") or "active").strip()[:50],
            "owner_user_id": overrides.get("owner_user_id"),
            "attributes": attributes,
        }

    @staticmethod
    def _json_object(value: str) -> dict[str, Any]:
        try:
            result = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return result if isinstance(result, dict) else {}

    def _asset_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]), "department_id": int(row["department_id"]), "kb_id": int(row["kb_id"]),
            "asset_type": row["asset_type"], "name": row["name"], "model": row["model"],
            "manufacturer": row["manufacturer"], "serial_number": row["serial_number"], "version": row["version"],
            "status": row["status"], "owner_user_id": row["owner_user_id"],
            "attributes": self._json_object(row["attributes_json"]), "evidence_count": int(row["evidence_count"] or 0),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def _candidate_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]), "kb_name": row["kb_name"], "file_id": row["file_id"], "file_name": row["file_name"],
            "source_kind": row["source_kind"], "extraction_method": row["extraction_method"], "asset_type": row["asset_type"],
            "name": row["name"], "model": row["model"], "manufacturer": row["manufacturer"], "version": row["version"],
            "attributes": self._json_object(row["attributes_json"]), "evidence_excerpt": row["evidence_excerpt"],
            "evidence_locator": row["evidence_locator"], "confidence": float(row["confidence"]), "status": row["status"],
            "asset_id": row["asset_id"], "created_at": row["created_at"], "resolved_at": row["resolved_at"],
        }

    def _evidence_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]), "file_id": row["file_id"], "file_name": row["file_name"], "locator": row["locator"],
            "excerpt": row["excerpt"], "metadata": self._json_object(row["metadata_json"]), "created_at": row["created_at"],
        }
