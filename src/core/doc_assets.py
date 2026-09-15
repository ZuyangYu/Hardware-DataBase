"""Document asset center: logical documents with version chains and lifecycle.

An asset is a *logical document* (one record per document, department+KB
scoped). Each uploaded file becomes a version on that asset and **登记即生效**:
a newly registered version immediately becomes the single ``effective`` one
(the previous effective version is marked ``superseded``). Lifecycle states
is ``effective`` (files leaving the parse store remove the ledger row with
them — the ledger is strictly bound to the parse store); transitions are recorded as audit
events, and documents relate to each other through typed links (wiki-style,
cross-KB within a department).
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from typing import Any

import src.settings


LIFECYCLE_STATES = ("draft", "effective")
VERSION_STATES = ("draft", "effective", "superseded", "deleted")
DOC_CATEGORIES = (
    "design_doc",
    "schematic",
    "bom",
    "netlist",
    "test_report",
    "requirement",
    "standard",
    "other",
)
RELATION_TYPES = ("derived_from", "companion", "references", "verified_by")
LINK_SOURCES = ("manual", "ai", "structure")
LINK_STATUSES = ("suggested", "confirmed", "dismissed")

def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def sync_file_deleted(*, kb_id: int, file_id: str) -> list[dict[str, Any]]:
    """模块级入口: 解析层删除后由 ragflow_backend 调用(fail-soft)。"""
    try:
        return DocumentAssetService().sync_file_deleted(kb_id=kb_id, file_id=file_id)
    except Exception:  # noqa: BLE001
        return []


def ensure_shadow_asset_for_record(record_id: int, *, store: Any | None = None) -> bool:
    """Parse 完成钩子: 为刚解析完成的文件立影子档(幂等, fail-soft)。

    由 document_store 的状态更新路径在「首次到达 completed」时调用;
    已登记过的文件直接跳过, 重复触发无副作用。
    """
    try:
        from src.pipelines.document_rag.schemas import TASK_STATUS_COMPLETED, normalize_parse_status
        from src.pipelines.document_store_sqlite import PipelineDocumentStore

        store = store or PipelineDocumentStore()
        rec = store.get_document_by_id(record_id)
        if rec is None or not rec.kb_id:
            return False
        parse_status = normalize_parse_status(rec.status, rec.processor_kind)
        if parse_status != TASK_STATUS_COMPLETED:
            return False
        service = DocumentAssetService()
        result = service.backfill_shadow_assets(
            kb_id=int(rec.kb_id),
            kb_name=rec.kb_name,
            department_id=int(rec.department_id),
            actor_user_id=None,
            files=[
                {
                    "file_id": str(rec.id),
                    "file_name": rec.original_file_name or rec.document_name,
                    "processor_kind": rec.processor_kind,
                    "content_hash": rec.content_hash,
                    "parse_status": parse_status,
                }
            ],
        )
        return result["created"] > 0
    except Exception:  # noqa: BLE001 - 钩子失败绝不阻断解析主流程
        return False


def _shadow_title(file_name: str) -> str:
    stem = os.path.splitext(str(file_name).strip())[0]
    return stem or str(file_name)


def _guess_category(file_name: str, processor_kind: str) -> str:
    name = file_name.lower()
    kind = processor_kind.lower()
    if "circuit" in kind or name.endswith(".edf"):
        return "netlist"
    if name.endswith(".pdf") and ("sch" in name or "原理图" in name):
        return "schematic"
    if "测试" in name or "test" in name or "debug" in name or "缺陷" in name:
        return "test_report"
    if "需求" in name or "requirement" in name:
        return "requirement"
    if "架构" in name or "architecture" in name or "设计说明" in name or "design" in name:
        return "design_doc"
    return "other"


class DocumentAssetError(ValueError):
    """Raised for lifecycle/validation violations surfaced as 400 to the API."""


class DocumentAssetService:
    """SQLite-backed document asset domain, colocated with auth/KB data."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or src.settings.AUTH_DB_PATH
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
                CREATE TABLE IF NOT EXISTS doc_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    department_id INTEGER NOT NULL,
                    kb_id INTEGER NOT NULL,
                    kb_name TEXT NOT NULL DEFAULT '',
                    project TEXT NOT NULL DEFAULT '',
                    doc_no TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    description TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    lifecycle_status TEXT NOT NULL DEFAULT 'draft',
                    current_version_id INTEGER,
                    owner_user_id INTEGER,
                    created_by_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id),
                    FOREIGN KEY(owner_user_id) REFERENCES users(id),
                    FOREIGN KEY(created_by_user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_doc_assets_scope
                    ON doc_assets(kb_id, department_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS doc_asset_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id INTEGER NOT NULL,
                    version_no INTEGER NOT NULL,
                    file_id TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    parse_status TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'draft',
                    uploaded_by_user_id INTEGER,
                    uploaded_at TEXT NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES doc_assets(id) ON DELETE CASCADE,
                    UNIQUE(asset_id, version_no)
                );
                CREATE INDEX IF NOT EXISTS idx_doc_asset_versions_asset
                    ON doc_asset_versions(asset_id, version_no);

                CREATE TABLE IF NOT EXISTS doc_asset_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id INTEGER NOT NULL,
                    version_id INTEGER,
                    event TEXT NOT NULL,
                    actor_user_id INTEGER,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES doc_assets(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_doc_asset_events_asset
                    ON doc_asset_events(asset_id, id);

                CREATE TABLE IF NOT EXISTS doc_asset_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    department_id INTEGER NOT NULL,
                    from_asset_id INTEGER NOT NULL,
                    to_asset_id INTEGER NOT NULL,
                    rel_type TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_by_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(from_asset_id) REFERENCES doc_assets(id) ON DELETE CASCADE,
                    FOREIGN KEY(to_asset_id) REFERENCES doc_assets(id) ON DELETE CASCADE,
                    UNIQUE(from_asset_id, to_asset_id, rel_type)
                );
                CREATE INDEX IF NOT EXISTS idx_doc_asset_links_from
                    ON doc_asset_links(from_asset_id);
                CREATE INDEX IF NOT EXISTS idx_doc_asset_links_to
                    ON doc_asset_links(to_asset_id);
                """
            )
            # 存量迁移: 登记即生效模型(草稿/修订中 → 生效; 已归档 → 已废止)
            conn.execute(
                """
                UPDATE doc_asset_versions SET state = 'effective'
                WHERE state = 'draft' AND id IN (
                    SELECT current_version_id FROM doc_assets
                    WHERE lifecycle_status IN ('draft', 'in_revision') AND current_version_id IS NOT NULL
                )
                """
            )
            conn.execute(
                """
                UPDATE doc_assets SET lifecycle_status = 'effective'
                WHERE lifecycle_status IN ('draft', 'in_revision') AND current_version_id IS NOT NULL
                """
            )
            conn.execute("DELETE FROM doc_assets WHERE lifecycle_status IN ('obsolete', 'archived')")
            conn.execute(
                "UPDATE doc_asset_versions SET file_id = replace(file_id, 'ragflow:', '') WHERE file_id LIKE 'ragflow:%'"
            )

    # -- create / list / detail ---------------------------------------------

    def create_asset(
        self,
        *,
        kb_id: int,
        kb_name: str,
        department_id: int,
        actor_user_id: int | None,
        title: str,
        doc_no: str = "",
        category: str = "other",
        project: str = "",
        description: str = "",
        tags: list[str] | None = None,
        initial_version: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise DocumentAssetError("资产标题不能为空")
        if category not in DOC_CATEGORIES:
            raise DocumentAssetError(f"未知的文档类别: {category}")
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO doc_assets (
                        department_id, kb_id, kb_name, project, doc_no, title, category,
                        description, tags_json, lifecycle_status, owner_user_id,
                        created_by_user_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        department_id, kb_id, kb_name, (project or "").strip(),
                        (doc_no or "").strip(), title, category, (description or "").strip(),
                        json.dumps([t.strip() for t in (tags or []) if t.strip()], ensure_ascii=False),
                        'effective' if initial_version else 'draft',
                        actor_user_id, actor_user_id, now, now,
                    ),
                )
                asset_id = int(cursor.lastrowid)
                if initial_version:
                    version_id = self._insert_version(conn, asset_id, 1, initial_version, actor_user_id, now)
                    conn.execute("UPDATE doc_asset_versions SET state = 'effective' WHERE id = ?", (version_id,))
                    self._log_event(conn, asset_id, version_id, "version_added", actor_user_id, "", now)
                else:
                    version_id = None
                conn.execute(
                    "UPDATE doc_assets SET current_version_id = ? WHERE id = ?",
                    (version_id, asset_id),
                )
                self._log_event(conn, asset_id, version_id, "created", actor_user_id, "", now)
                conn.execute("COMMIT")
                row = conn.execute("SELECT * FROM doc_assets WHERE id = ?", (asset_id,)).fetchone()
                return self._asset_row(row, conn)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_assets(
        self,
        *,
        kb_id: int,
        department_id: int,
        query: str = "",
        status: str = "",
        category: str = "",
        project: str = "",
    ) -> list[dict[str, Any]]:
        clauses = ["a.kb_id = ?", "a.department_id = ?"]
        params: list[Any] = [kb_id, department_id]
        needle = query.strip()
        if needle:
            clauses.append("(a.title LIKE ? OR a.doc_no LIKE ? OR a.project LIKE ?)")
            like = f"%{needle}%"
            params.extend([like, like, like])
        if status:
            clauses.append("a.lifecycle_status = ?")
            params.append(status)
        if category:
            clauses.append("a.category = ?")
            params.append(category)
        if project:
            clauses.append("a.project = ?")
            params.append(project.strip())
        sql = f"""
            SELECT a.*,
                   (SELECT COUNT(*) FROM doc_asset_versions v WHERE v.asset_id = a.id) AS version_count,
                   (SELECT v.version_no FROM doc_asset_versions v
                     WHERE v.id = a.current_version_id AND v.state = 'effective') AS effective_version_no
            FROM doc_assets a
            WHERE {' AND '.join(clauses)}
            ORDER BY a.updated_at DESC
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._asset_row(row, conn) for row in rows]

    def get_asset(self, *, asset_id: int, kb_id: int, department_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM doc_assets WHERE id = ? AND kb_id = ? AND department_id = ?",
                (asset_id, kb_id, department_id),
            ).fetchone()
            if row is None:
                return None
            detail = self._asset_row(row, conn)
            detail["versions"] = [
                self._version_row(v) for v in conn.execute(
                    "SELECT * FROM doc_asset_versions WHERE asset_id = ? ORDER BY version_no", (asset_id,)
                ).fetchall()
            ]
            detail["events"] = [
                self._event_row(e) for e in conn.execute(
                    "SELECT * FROM doc_asset_events WHERE asset_id = ? ORDER BY id DESC LIMIT 100", (asset_id,)
                ).fetchall()
            ]
            detail["links_out"] = [
                self._link_row(link_row) for link_row in conn.execute(
                    """
                    SELECT l.*, a.title AS to_title, a.category AS to_category, a.kb_name AS to_kb_name
                    FROM doc_asset_links l JOIN doc_assets a ON a.id = l.to_asset_id
                    WHERE l.from_asset_id = ? ORDER BY l.id
                    """,
                    (asset_id,),
                ).fetchall()
            ]
            detail["links_in"] = [
                self._link_row(link_row) for link_row in conn.execute(
                    """
                    SELECT l.*, a.title AS to_title, a.category AS to_category, a.kb_name AS to_kb_name
                    FROM doc_asset_links l JOIN doc_assets a ON a.id = l.from_asset_id
                    WHERE l.to_asset_id = ? ORDER BY l.id
                    """,
                    (asset_id,),
                ).fetchall()
            ]
            return detail

    # -- lifecycle -----------------------------------------------------------

    def update_info(
        self,
        *,
        asset_id: int,
        kb_id: int,
        department_id: int,
        actor_user_id: int | None,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {"title", "doc_no", "category", "project", "description", "tags"}
        unknown = set(fields) - allowed
        if unknown:
            raise DocumentAssetError(f"不支持的字段: {', '.join(sorted(unknown))}")
        updates: dict[str, Any] = {}
        if "title" in fields:
            title = (fields["title"] or "").strip()
            if not title:
                raise DocumentAssetError("资产标题不能为空")
            updates["title"] = title
        if "category" in fields:
            if fields["category"] not in DOC_CATEGORIES:
                raise DocumentAssetError(f"未知的文档类别: {fields['category']}")
            updates["category"] = fields["category"]
        for key in ("doc_no", "project", "description"):
            if key in fields:
                updates[key] = str(fields[key] or "").strip()
        if "tags" in fields:
            updates["tags_json"] = json.dumps(
                [str(t).strip() for t in (fields["tags"] or []) if str(t).strip()], ensure_ascii=False
            )
        if not updates:
            raise DocumentAssetError("没有需要更新的字段")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._scoped_asset(conn, asset_id, kb_id, department_id)
                # 认领动作(改标题/类别/项目)自动摘掉影子档标记
                if {"title", "category", "project"} & set(updates):
                    tags = json.loads(row["tags_json"] or "[]")
                    if "影子档" in tags:
                        tags = [t for t in tags if t != "影子档"]
                        updates["tags_json"] = json.dumps(tags, ensure_ascii=False)
                sets = ", ".join(f"{key} = ?" for key in updates)
                conn.execute(
                    f"UPDATE doc_assets SET {sets}, updated_at = ? WHERE id = ?",
                    (*updates.values(), utc_now(), asset_id),
                )
                self._log_event(conn, asset_id, None, "info_updated", actor_user_id, "", utc_now())
                conn.execute("COMMIT")
                row = conn.execute("SELECT * FROM doc_assets WHERE id = ?", (asset_id,)).fetchone()
                return self._asset_row(row, conn)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def add_version(
        self,
        *,
        asset_id: int,
        kb_id: int,
        department_id: int,
        actor_user_id: int | None,
        file_id: str,
        file_name: str,
        content_hash: str = "",
        parse_status: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        file_id = str(file_id or "").split(":")[-1].strip()
        if not file_id:
            raise DocumentAssetError("版本必须关联一个知识库文件")
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                asset = self._scoped_asset(conn, asset_id, kb_id, department_id)
                if asset["lifecycle_status"] in {"obsolete", "archived"}:
                    raise DocumentAssetError("已废止或归档的资产不能添加版本")
                dup = conn.execute(
                    "SELECT id, version_no FROM doc_asset_versions WHERE asset_id = ? AND file_id = ?",
                    (asset_id, file_id),
                ).fetchone()
                if dup is not None:
                    raise DocumentAssetError(f"该文件已作为 v{dup['version_no']} 登记过")
                if content_hash:
                    dup_hash = conn.execute(
                        "SELECT id, version_no FROM doc_asset_versions WHERE asset_id = ? AND content_hash = ?",
                        (asset_id, content_hash),
                    ).fetchone()
                    if dup_hash is not None:
                        raise DocumentAssetError(f"与 v{dup_hash['version_no']} 内容完全相同,疑似重复上传")
                next_no = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(version_no), 0) + 1 FROM doc_asset_versions WHERE asset_id = ?",
                        (asset_id,),
                    ).fetchone()[0]
                )
                version_id = self._insert_version(
                    conn, asset_id, next_no,
                    {"file_id": file_id, "file_name": file_name, "content_hash": content_hash,
                     "parse_status": parse_status, "note": note},
                    actor_user_id, now,
                )
                # 登记即生效: 新版本直接顶替旧生效版本
                old_effective = conn.execute(
                    "SELECT id FROM doc_asset_versions WHERE asset_id = ? AND state = 'effective'",
                    (asset_id,),
                ).fetchone()
                if old_effective is not None:
                    conn.execute(
                        "UPDATE doc_asset_versions SET state = 'superseded' WHERE id = ?",
                        (old_effective["id"],),
                    )
                    self._log_event(conn, asset_id, int(old_effective["id"]), "superseded", actor_user_id, "", now)
                conn.execute("UPDATE doc_asset_versions SET state = 'effective' WHERE id = ?", (version_id,))
                self._log_event(conn, asset_id, version_id, "version_added", actor_user_id, note, now)
                conn.execute(
                    "UPDATE doc_assets SET current_version_id = ?, lifecycle_status = 'effective', updated_at = ? WHERE id = ?",
                    (version_id, now, asset_id),
                )
                conn.execute("COMMIT")
                row = conn.execute("SELECT * FROM doc_assets WHERE id = ?", (asset_id,)).fetchone()
                return self._asset_row(row, conn)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def version_file_names(self, *, asset_id: int, kb_id: int, department_id: int) -> list[str]:
        """资产全部版本的文件名(供废止时联动解析层出库)。"""
        with closing(self._connect()) as conn:
            self._scoped_asset(conn, asset_id, kb_id, department_id)
            rows = conn.execute(
                """
                SELECT v.file_name FROM doc_asset_versions v
                JOIN doc_assets a ON a.id = v.asset_id
                WHERE v.asset_id = ? AND v.file_name != ''
                """,
                (asset_id,),
            ).fetchall()
            return [r["file_name"] for r in rows]

    def add_link(
        self,
        *,
        department_id: int,
        from_asset_id: int,
        to_asset_id: int,
        rel_type: str,
        note: str = "",
        source: str = "manual",
        status: str = "confirmed",
        actor_user_id: int | None = None,
    ) -> dict[str, Any]:
        if rel_type not in RELATION_TYPES:
            raise DocumentAssetError(f"未知的关系类型: {rel_type}")
        if source not in LINK_SOURCES or status not in LINK_STATUSES:
            raise DocumentAssetError("关系来源或状态不合法")
        if from_asset_id == to_asset_id:
            raise DocumentAssetError("文档不能与自己建立关联")
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for aid in (from_asset_id, to_asset_id):
                    row = conn.execute(
                        "SELECT id, department_id FROM doc_assets WHERE id = ?", (aid,)
                    ).fetchone()
                    if row is None or int(row["department_id"]) != int(department_id):
                        raise DocumentAssetError("关联目标不存在或不属于同一部门")
                dup = conn.execute(
                    "SELECT id FROM doc_asset_links WHERE from_asset_id = ? AND to_asset_id = ? AND rel_type = ?",
                    (from_asset_id, to_asset_id, rel_type),
                ).fetchone()
                if dup is not None:
                    raise DocumentAssetError("相同类型的关联已存在")
                cursor = conn.execute(
                    """
                    INSERT INTO doc_asset_links (
                        department_id, from_asset_id, to_asset_id, rel_type, note, source,
                        status, created_by_user_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (department_id, from_asset_id, to_asset_id, rel_type, (note or "").strip(),
                     source, status, actor_user_id, now),
                )
                link_id = int(cursor.lastrowid)
                conn.execute("COMMIT")
                row = conn.execute("SELECT * FROM doc_asset_links WHERE id = ?", (link_id,)).fetchone()
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self._link_row(row)

    def remove_link(self, *, department_id: int, link_id: int) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "DELETE FROM doc_asset_links WHERE id = ? AND department_id = ?", (link_id, department_id)
            )
            return cursor.rowcount > 0

    # -- shadow backfill -----------------------------------------------------

    def backfill_shadow_assets(
        self,
        *,
        kb_id: int,
        kb_name: str,
        department_id: int,
        actor_user_id: int | None,
        files: list[dict[str, Any]],
    ) -> dict[str, int]:
        """One shadow asset per parsed file that is not yet versioned anywhere.

        Shadow assets are drafts awaiting a human to claim (rename, set
        category/project). Idempotent: files already registered as any
        version inside this KB are skipped.
        """
        created = 0
        skipped = 0
        for f in files:
            file_id = str(f.get("file_id") or "").split(":")[-1].strip()
            if not file_id:
                skipped += 1
                continue
            with closing(self._connect()) as conn:
                dup = conn.execute(
                    """
                    SELECT v.id FROM doc_asset_versions v
                    JOIN doc_assets a ON a.id = v.asset_id
                    WHERE a.kb_id = ? AND v.file_id = ? LIMIT 1
                    """,
                    (kb_id, file_id),
                ).fetchone()
            if dup is not None:
                skipped += 1
                continue
            content_hash = str(f.get("content_hash") or "")
            if content_hash:
                with closing(self._connect()) as conn:
                    dup_hash = conn.execute(
                        """
                        SELECT v.id FROM doc_asset_versions v
                        JOIN doc_assets a ON a.id = v.asset_id
                        WHERE a.kb_id = ? AND v.content_hash = ? LIMIT 1
                        """,
                        (kb_id, content_hash),
                    ).fetchone()
                if dup_hash is not None:
                    skipped += 1
                    continue
            self.create_asset(
                kb_id=kb_id,
                kb_name=kb_name,
                department_id=department_id,
                actor_user_id=actor_user_id,
                title=_shadow_title(f.get("file_name") or file_id),
                category=_guess_category(str(f.get("file_name") or ""), str(f.get("processor_kind") or "")),
                description="",
                tags=["影子档"],
                initial_version={
                    "file_id": file_id,
                    "file_name": str(f.get("file_name") or ""),
                    "content_hash": str(f.get("content_hash") or ""),
                    "parse_status": str(f.get("parse_status") or ""),
                    "note": "",
                },
            )
            created += 1
        return {"created": created, "skipped": skipped}

    def sync_file_deleted(self, *, kb_id: int, file_id: str) -> list[dict[str, Any]]:
        """解析层删除文件 → 台账同步。

        该文件对应版本标 ``deleted``;若它是生效版本:
        有其他留存版本 → 回退最近留存版本为生效;一个不剩 → 资产转废止。
        返回受影响资产列表。
        """
        affected: list[dict[str, Any]] = []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT a.id FROM doc_asset_versions v
                JOIN doc_assets a ON a.id = v.asset_id
                WHERE a.kb_id = ? AND v.file_id = ?
                """,
                (kb_id, file_id),
            ).fetchall()
            for row in rows:
                asset_id = int(row["id"])
                now = utc_now()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    version = conn.execute(
                        "SELECT id, state FROM doc_asset_versions WHERE asset_id = ? AND file_id = ?",
                        (asset_id, file_id),
                    ).fetchone()
                    if version is None:
                        conn.execute("ROLLBACK")
                        continue
                    was_effective = version["state"] == "effective"
                    conn.execute(
                        "UPDATE doc_asset_versions SET state = 'deleted' WHERE id = ?",
                        (version["id"],),
                    )
                    asset = conn.execute("SELECT * FROM doc_assets WHERE id = ?", (asset_id,)).fetchone()
                    if was_effective:
                        remaining = conn.execute(
                            """
                            SELECT id, version_no FROM doc_asset_versions
                            WHERE asset_id = ? AND state IN ('effective', 'superseded')
                            ORDER BY version_no DESC LIMIT 1
                            """,
                            (asset_id,),
                        ).fetchone()
                        if remaining is not None:
                            conn.execute(
                                "UPDATE doc_asset_versions SET state = 'effective' WHERE id = ?",
                                (remaining["id"],),
                            )
                            conn.execute(
                                "UPDATE doc_assets SET current_version_id = ?, updated_at = ? WHERE id = ?",
                                (remaining["id"], now, asset_id),
                            )
                            self._log_event(
                                conn, asset_id, int(remaining["id"]), "source_file_deleted",
                                None, f"源文件已删除, v{remaining['version_no']} 回归生效", now,
                            )
                        else:
                            # 账随物走: 文件删光, 资产记录随之删除
                            conn.execute("DELETE FROM doc_assets WHERE id = ?", (asset_id,))
                    else:
                        self._log_event(
                            conn, asset_id, None, "source_file_deleted", None, "", now,
                        )
                    conn.execute("COMMIT")
                    affected.append({"asset_id": asset_id, "title": asset["title"]})
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        return affected

    def absorb_shadow_for_file(
        self, *, kb_id: int, department_id: int, file_id: str, exclude_asset_id: int
    ) -> dict[str, Any] | None:
        """文件被登记为新版本后, 若它此前只属于一条「纯影子档」
        (草稿 + 影子档标记 + 仅这一个版本), 自动归档该影子档, 返回其信息;
        已认领或含多版本的资产不动, 由人工处理。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.title, a.lifecycle_status, a.tags_json,
                       (SELECT COUNT(*) FROM doc_asset_versions v WHERE v.asset_id = a.id) AS version_count
                FROM doc_asset_versions v JOIN doc_assets a ON a.id = v.asset_id
                WHERE a.kb_id = ? AND a.department_id = ? AND v.file_id = ? AND a.id != ?
                """,
                (kb_id, department_id, file_id, exclude_asset_id),
            ).fetchall()
            for row in rows:
                tags = json.loads(row["tags_json"] or "[]")
                if (
                    row["lifecycle_status"] == "effective"
                    and "影子档" in tags
                    and int(row["version_count"]) == 1
                ):
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        conn.execute("DELETE FROM doc_assets WHERE id = ?", (row["id"],))
                        conn.execute("COMMIT")
                        return {"id": int(row["id"]), "title": row["title"]}
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
        return None

    # -- internals -----------------------------------------------------------

    def _scoped_asset(self, conn: sqlite3.Connection, asset_id: int, kb_id: int, department_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM doc_assets WHERE id = ? AND kb_id = ? AND department_id = ?",
            (asset_id, kb_id, department_id),
        ).fetchone()
        if row is None:
            raise LookupError("文档资产不存在")
        return row

    def _insert_version(
        self,
        conn: sqlite3.Connection,
        asset_id: int,
        version_no: int,
        payload: dict[str, Any],
        actor_user_id: int | None,
        now: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO doc_asset_versions (
                asset_id, version_no, file_id, file_name, content_hash, parse_status,
                note, state, uploaded_by_user_id, uploaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                asset_id, version_no, str(payload.get("file_id") or ""), str(payload.get("file_name") or ""),
                str(payload.get("content_hash") or ""), str(payload.get("parse_status") or ""),
                str(payload.get("note") or ""), actor_user_id, now,
            ),
        )
        return int(cursor.lastrowid)

    def _log_event(
        self, conn: sqlite3.Connection, asset_id: int, version_id: int | None,
        event: str, actor_user_id: int | None, comment: str, now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO doc_asset_events (asset_id, version_id, event, actor_user_id, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (asset_id, version_id, event, actor_user_id, comment or "", now),
        )

    def _asset_row(self, row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        data = dict(row)
        data["tags"] = json.loads(data.pop("tags_json") or "[]")
        if conn is not None:
            count = conn.execute(
                "SELECT COUNT(*) FROM doc_asset_versions WHERE asset_id = ?", (data["id"],)
            ).fetchone()[0]
            data["version_count"] = int(count)
            effective = conn.execute(
                "SELECT version_no FROM doc_asset_versions WHERE id = ? AND state = 'effective'",
                (data.get("current_version_id") or 0,),
            ).fetchone()
            data["effective_version_no"] = int(effective[0]) if effective is not None else None
        return data

    def _version_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _event_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _link_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)
