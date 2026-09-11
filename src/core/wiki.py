"""Wiki knowledge layer, modelled on Tencent WeKnora's Wiki Mode.

Documents are distilled by an LLM into interlinked markdown pages
(summary / entity / concept) with chunk-level provenance (``chunk_refs``),
bidirectional links (``in_links`` / ``out_links``), a granularity knob for
extraction density, and immutable revision snapshots with one-click rollback.

Pipeline (WeKnora style, distilling from the *parsed* chunk store — never raw
originals):
  Pass 0  per document: extract candidate entity/concept slugs
  Map     per document: classify which source chunks substantively discuss
          each candidate (chunk handles c000... translated back to real ids;
          unknown handles are dropped — anti-hallucination), then write a
          per-document summary page
  Reduce  per slug: write or update the wiki page from the cited chunk text
          (pages without material are skipped — no stubs)
  Post    linkify cross-page links, strip dead links, rebuild index, prune
          revisions

Wiki pages are NOT the citation authority: answers always trace back to the
source anchors recorded in ``source_refs`` / ``chunk_refs``.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from typing import Any, Callable

import src.settings


PAGE_TYPES = ("summary", "entity", "concept", "index")
PAGE_STATUSES = ("draft", "published", "archived")
EDIT_SOURCES = ("pipeline", "agent", "user", "revert")
GRANULARITIES = ("focused", "standard", "exhaustive")

MAX_REVISIONS_PER_PAGE = 50
MAX_CHUNK_CHARS_PER_BATCH = 12000

WIKI_JOB_STATE: dict[int, dict[str, Any]] = {}
_WIKI_JOB_LOCK = threading.Lock()


class WikiError(ValueError):
    """Wiki validation / state errors surfaced as 400 to the API."""


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def slugify(name: str) -> str:
    token = re.sub(r"\s+", "-", str(name).strip().lower())
    token = re.sub(r"[^\w\u4e00-\u9fff\-]+", "", token, flags=re.UNICODE)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    return token[:80] or "untitled"


def _clip(text: str, limit: int = 24000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…(截断)"


def _parse_json_object(raw: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _first_paragraph(content: str, limit: int = 200) -> str:
    for para in str(content or "").split("\n"):
        text = para.strip().lstrip("#").strip()
        if text:
            return text[:limit]
    return ""


def _batch_chunks(chunks: list[str], max_chars: int) -> list[str]:
    """把原始素材块按顺序合并成批次, 保证 handle→原始块 的映射不丢失。

    旧实现把拼接后的全文重新按段落切批, 批次索引与原始块索引错位,
    引用阶段拿到的块文本张冠李戴(LP87702 引用到封面页)。"""
    batches: list[str] = []
    current: list[str] = []
    size = 0
    for chunk in chunks:
        text = str(chunk).strip()
        if not text:
            continue
        if current and size + len(text) > max_chars:
            batches.append("\n\n".join(current))
            current, size = [], 0
        current.append(text)
        size += len(text) + 2
    if current:
        batches.append("\n\n".join(current))
    return batches


CANDIDATE_PROMPT = """你是硬件知识库的 Wiki 编辑。阅读以下文档内容,抽取值得建词条的实体与概念。

抽取粒度:{granularity_hint}

要求:
1. entity = 具体对象(芯片型号/器件/板卡/接口/信号/网络/接插件/文档号); concept = 抽象概念/机制/流程
2. slug = 小写连字符形式, 如 entity/tcan1145dmtrq1 或 concept/看门狗
3. aliases = 别名/缩写/英文名
4. 只抽文本中实际出现或实质讨论的, 不要编造; 不要给出每条的具体事实(稍后由材料引用阶段补充)
5. 输出严格 JSON: {{"entities": [{{"name": "...", "slug": "...", "aliases": [], "description": "一句话"}}],
   "concepts": [{{"name": "...", "slug": "...", "aliases": [], "description": "一句话"}}]}} 不要其他文字。

文档内容:
{text}"""

CITATION_PROMPT = """判断下列候选词条分别被哪些内容块实质讨论。

候选词条:
{candidates}

内容块(handle 与正文):
{chunks}

要求:
1. 只在内容块确实实质讨论该词条时才引用(handle 原样返回); 一次性顺带提及不算
2. 若发现候选之外的新词条, 放入 new_slugs(带实质讨论它的内容块 handle)
3. 输出严格 JSON: {{"citations": {{"<slug>": ["c000", ...]}}, "new_slugs": [{{"type": "entity|concept", "name": "...", "slug": "...", "aliases": [], "description": "...", "source_chunks": ["c000"]}}]}} 不要其他文字。"""

PAGE_PROMPT = """为 Wiki 词条《{title}》撰写页面。

已有页面内容(如有, 请在事实不变的前提下合并更新):
{existing}

支撑材料(来自知识库原文, 逐字引用):
{chunks}

要求:
1. 用 Markdown, 中文; 结构化组织(概述 / 关键事实 / 出处), 忠实于材料, 不要编造
2. 提到其他词条时用 [[slug]] 形式做互链(只允许这些 slug: {known_slugs})
3. 保留材料中的关键数值/型号/位号原文
4. 页面末尾加一行 "## 来源" 列出支撑材料编号
5. 直接输出页面内容, 不要其他说明。"""

SUMMARY_PROMPT = """你是硬件知识库的 Wiki 编辑。为下面这份文档撰写摘要页。

要求:
1. 中文 Markdown, 400-700 字: 概述(这份文档是什么、覆盖什么) / 关键内容与结构 / 值得单独建词条的主题
2. 忠实于材料, 不编造; 关键数值/位号/型号保留原文
3. 末尾加 "## 来源" 列出 [n] 编号
4. 直接输出页面内容, 不要其他说明。

文档材料(逐字引用):
{chunks}
"""

GRANULARITY_HINTS = {
    "focused": "只抽文档的主要对象(如一份需求书抽它描述的产品/模块), 跳过顺带提到的技术名词",
    "standard": "主要对象 + 被实质讨论(专门段落或多条内容)的实体/概念, 跳过一次性提及",
    "exhaustive": "所有被点名的实体和可识别概念, 包括顺带提到的型号/工具",
}

# Excel 工程文档的样板表单(模板说明/封面/版本记录): 不作为蒸馏素材
_APPROVAL_RE = re.compile(r"批准人|Approve date|保存期限|密级|Security classification", re.IGNORECASE)

_TEMPLATE_SHEET_RE = re.compile(
    r"模板|封面|cover|使用说明|说明页|定义页|变更记录|版本记录|"
    r"变更历史|change\s*history|example|示例|样例|sample|(?<![a-z])face(?![a-z])",
    re.IGNORECASE,
)

# 正文外的样板内容(跨格式通用): 模板使用说明/封面/签署页/变更履历/声明等。
# 表格块与 RAGFlow chunk 都过这一层, 防止摘要与词条取材落到示例模板而非正文。
_BOILERPLATE_TEXT_RE = re.compile(
    r"模板使用说明|Template\s+instructions|填写说明|项目适配说明|参考样例|"
    r"Instruction\s+Manual|变更历史|Change\s+History|变更履历|Change\s+Record|"
    r"所\s*有\s*权\s*声\s*明|Statement\s+of\s+(proprietary\s+rights|ownership)|"
    r"文件状态|Document\s+Status|Released\s+on|Implemented\s+on|"
    r"文件编号\s*File\s*No|文件编号\s*[:：]|编制\s*[:：]|Author\s*[:：]|审核\s*[:：]|Checker\s*[:：]|"
    r"批准\s*[:：]|Approver\s*[:：]|签名\s*[:：]|Sign\s*[:：]|使用本模板",
    re.IGNORECASE,
)

WIKI_MAX_BLOCKS_PER_DOC = 6
WIKI_MIN_CELL_DENSITY = 24


class WikiService:
    """SQLite-backed wiki domain colocated with auth/KB data."""

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
                CREATE TABLE IF NOT EXISTS wiki_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    department_id INTEGER NOT NULL,
                    kb_id INTEGER NOT NULL,
                    kb_name TEXT NOT NULL DEFAULT '',
                    slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    page_type TEXT NOT NULL DEFAULT 'entity',
                    status TEXT NOT NULL DEFAULT 'published',
                    content TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    chunk_refs_json TEXT NOT NULL DEFAULT '[]',
                    in_links_json TEXT NOT NULL DEFAULT '[]',
                    out_links_json TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 1,
                    last_edit_source TEXT NOT NULL DEFAULT 'pipeline',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id),
                    UNIQUE(kb_id, slug)
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_pages_scope
                    ON wiki_pages(kb_id, page_type, status);

                CREATE TABLE IF NOT EXISTS wiki_page_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    page_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    edit_source TEXT NOT NULL DEFAULT '',
                    editor_id INTEGER,
                    edited_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(page_id) REFERENCES wiki_pages(id) ON DELETE CASCADE,
                    UNIQUE(page_id, version)
                );
                """
            )

    # -- reading -------------------------------------------------------------

    def list_pages(
        self, *, kb_id: int, department_id: int, query: str = "",
        page_type: str = "", status: str = "published",
    ) -> list[dict[str, Any]]:
        clauses = ["kb_id = ?", "department_id = ?"]
        params: list[Any] = [kb_id, department_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if page_type:
            clauses.append("page_type = ?")
            params.append(page_type)
        needle = query.strip()
        if needle:
            clauses.append("(title LIKE ? OR summary LIKE ? OR aliases_json LIKE ?)")
            like = f"%{needle}%"
            params.extend([like, like, like])
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT id, kb_id, slug, title, page_type, status, summary, aliases_json,
                       in_links_json, out_links_json, source_refs_json, chunk_refs_json,
                       version, last_edit_source, updated_at
                FROM wiki_pages WHERE {' AND '.join(clauses)}
                ORDER BY page_type, title
                """,
                params,
            ).fetchall()
            return [self._page_row(r, content=False) for r in rows]

    def get_page(self, *, kb_id: int, department_id: int, slug: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE kb_id = ? AND department_id = ? AND slug = ?",
                (kb_id, department_id, slug),
            ).fetchone()
            if row is None:
                return None
            page = self._page_row(row)
            page["backlinks"] = [
                {"slug": r["slug"], "title": r["title"]}
                for r in conn.execute(
                    "SELECT slug, title FROM wiki_pages WHERE kb_id = ? AND status = 'published' AND out_links_json LIKE ?",
                    (kb_id, f'%"{slug}"%'),
                )
            ]
            page["revisions"] = [
                dict(r) for r in conn.execute(
                    """
                    SELECT version, title, page_type, status, edit_source, editor_id, edited_at, created_at
                    FROM wiki_page_revisions WHERE page_id = ? ORDER BY version DESC
                    """,
                    (row["id"],),
                )
            ]
            return page

    def get_stats(self, *, kb_id: int, department_id: int) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            by_type = {
                r["page_type"]: r["n"]
                for r in conn.execute(
                    "SELECT page_type, COUNT(*) AS n FROM wiki_pages WHERE kb_id = ? AND department_id = ? AND status = 'published' GROUP BY page_type",
                    (kb_id, department_id),
                )
            }
            orphan = conn.execute(
                """
                SELECT COUNT(*) FROM wiki_pages
                WHERE kb_id = ? AND department_id = ? AND status = 'published'
                  AND page_type != 'index' AND in_links_json = '[]'
                """,
                (kb_id, department_id),
            ).fetchone()[0]
            return {
                "total_pages": sum(by_type.values()),
                "pages_by_type": by_type,
                "orphan_count": int(orphan),
            }

    def get_graph(self, *, kb_id: int, department_id: int, limit: int = 200) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT slug, title, page_type, in_links_json, out_links_json
                FROM wiki_pages WHERE kb_id = ? AND department_id = ? AND status = 'published'
                """,
                (kb_id, department_id),
            ).fetchall()
        nodes = []
        slug_set: set[str] = set()
        for r in rows:
            out_links = json.loads(r["out_links_json"] or "[]")
            in_links = json.loads(r["in_links_json"] or "[]")
            slug_set.add(r["slug"])
            nodes.append({
                "slug": r["slug"], "title": r["title"], "page_type": r["page_type"],
                "link_count": len(out_links) + len(in_links),
            })
        edges: list[dict[str, str]] = []
        for r in rows:
            for target in json.loads(r["out_links_json"] or "[]"):
                if target in slug_set:
                    edges.append({"source": r["slug"], "target": target})
        nodes.sort(key=lambda n: n["link_count"], reverse=True)
        truncated = len(nodes) > limit
        return {"nodes": nodes[:limit], "edges": edges, "total": len(nodes), "truncated": truncated}

    # -- manual edits --------------------------------------------------------

    def upsert_manual(
        self, *, kb_id: int, department_id: int, kb_name: str, actor_user_id: int | None,
        title: str, page_type: str = "concept", slug: str = "", content: str = "",
        summary: str = "", aliases: list[str] | None = None, status: str = "published",
    ) -> dict[str, Any]:
        if page_type not in PAGE_TYPES:
            raise WikiError(f"未知页面类型: {page_type}")
        if status not in PAGE_STATUSES:
            raise WikiError(f"未知页面状态: {status}")
        if not title.strip():
            raise WikiError("页面标题不能为空")
        slug = slug or f"{page_type}/{slugify(title)}"
        if not slug.startswith(("entity/", "concept/", "summary/", "index/")):
            slug = f"{page_type}/{slug}"
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE kb_id = ? AND slug = ?", (kb_id, slug)
                ).fetchone()
                if row is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO wiki_pages (
                            department_id, kb_id, kb_name, slug, title, page_type, status,
                            content, summary, aliases_json, version, last_edit_source,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'user', ?, ?)
                        """,
                        (department_id, kb_id, kb_name, slug, title.strip(), page_type, status,
                         content, summary, json.dumps(aliases or [], ensure_ascii=False), now, now),
                    )
                    page_id = int(cursor.lastrowid)
                else:
                    page_id = int(row["id"])
                    self._snapshot_revision(conn, row, "user", actor_user_id, now)
                    conn.execute(
                        """
                        UPDATE wiki_pages SET title = ?, page_type = ?, status = ?, content = ?,
                            summary = ?, aliases_json = ?, version = version + 1,
                            last_edit_source = 'user', updated_at = ? WHERE id = ?
                        """,
                        (title.strip(), page_type, status, content, summary,
                         json.dumps(aliases or [], ensure_ascii=False), now, page_id),
                    )
                self._rebuild_links(conn, kb_id)
                page = conn.execute("SELECT * FROM wiki_pages WHERE id = ?", (page_id,)).fetchone()
                conn.execute("COMMIT")
                return self._page_row(page)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def update_page(
        self, *, kb_id: int, department_id: int, slug: str, actor_user_id: int | None,
        fields: dict[str, Any], expect_version: int | None = None,
    ) -> dict[str, Any]:
        allowed = {"title", "content", "summary", "aliases", "status"}
        unknown = set(fields) - allowed
        if unknown:
            raise WikiError(f"不支持的字段: {', '.join(sorted(unknown))}")
        if "status" in fields and fields["status"] not in PAGE_STATUSES:
            raise WikiError(f"未知页面状态: {fields['status']}")
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE kb_id = ? AND department_id = ? AND slug = ?",
                    (kb_id, department_id, slug),
                ).fetchone()
                if row is None:
                    raise LookupError("wiki page not found")
                if expect_version is not None and int(expect_version) != int(row["version"]):
                    raise WikiError("页面已被他人修改, 请刷新后重试")
                sets: list[str] = []
                params: list[Any] = []
                if "title" in fields:
                    if not str(fields["title"]).strip():
                        raise WikiError("页面标题不能为空")
                    sets.append("title = ?")
                    params.append(str(fields["title"]).strip())
                if "content" in fields:
                    sets.append("content = ?")
                    params.append(str(fields["content"] or ""))
                if "summary" in fields:
                    sets.append("summary = ?")
                    params.append(str(fields["summary"] or ""))
                if "aliases" in fields:
                    sets.append("aliases_json = ?")
                    params.append(json.dumps(
                        [str(a).strip() for a in fields["aliases"] or [] if str(a).strip()],
                        ensure_ascii=False,
                    ))
                if "status" in fields:
                    sets.append("status = ?")
                    params.append(fields["status"])
                self._snapshot_revision(conn, row, "user", actor_user_id, now)
                conn.execute(
                    f"UPDATE wiki_pages SET {', '.join(sets)}, version = version + 1, "
                    "last_edit_source = 'user', updated_at = ? WHERE id = ?",
                    (*params, now, row["id"]),
                )
                self._rebuild_links(conn, kb_id)
                page = conn.execute("SELECT * FROM wiki_pages WHERE id = ?", (row["id"],)).fetchone()
                conn.execute("COMMIT")
                return self._page_row(page)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def archive_page(self, *, kb_id: int, department_id: int, slug: str, actor_user_id: int | None) -> dict[str, Any]:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE kb_id = ? AND department_id = ? AND slug = ?",
                    (kb_id, department_id, slug),
                ).fetchone()
                if row is None:
                    raise LookupError("wiki page not found")
                self._snapshot_revision(conn, row, "user", actor_user_id, now)
                conn.execute(
                    "UPDATE wiki_pages SET status = 'archived', version = version + 1, "
                    "last_edit_source = 'user', updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                self._rebuild_links(conn, kb_id)
                page = conn.execute("SELECT * FROM wiki_pages WHERE id = ?", (row["id"],)).fetchone()
                conn.execute("COMMIT")
                return self._page_row(page)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def revert_page(
        self, *, kb_id: int, department_id: int, slug: str, version: int, actor_user_id: int | None
    ) -> dict[str, Any]:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE kb_id = ? AND department_id = ? AND slug = ?",
                    (kb_id, department_id, slug),
                ).fetchone()
                if row is None:
                    raise LookupError("wiki page not found")
                rev = conn.execute(
                    "SELECT * FROM wiki_page_revisions WHERE page_id = ? AND version = ?",
                    (row["id"], version),
                ).fetchone()
                if rev is None:
                    raise WikiError(f"版本 v{version} 不存在")
                self._snapshot_revision(conn, row, "revert", actor_user_id, now)
                conn.execute(
                    """
                    UPDATE wiki_pages SET title = ?, page_type = ?, status = ?, content = ?,
                        summary = ?, aliases_json = ?, version = version + 1,
                        last_edit_source = 'revert', updated_at = ? WHERE id = ?
                    """,
                    (rev["title"], rev["page_type"], rev["status"], rev["content"], rev["summary"],
                     rev["aliases_json"], now, row["id"]),
                )
                self._rebuild_links(conn, kb_id)
                page = conn.execute("SELECT * FROM wiki_pages WHERE id = ?", (row["id"],)).fetchone()
                conn.execute("COMMIT")
                return self._page_row(page)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _spreadsheet_blocks(
        self, doc_id: str, kb_id: int, department_id: int, kb_name: str
    ) -> list[str]:
        """选高密度、非样板 sheet 的 text_blocks 作为蒸馏素材。

        三层过滤, 全部通用(不依赖任何领域词表):
        ① sheet 名匹配已知样板词(模板/封面/使用说明...) → 廉价首滤
        ② 跨文档重复哈希: 同一文本块在本库不同文档中原样重复出现 >= 3 次
           = 组织级样板层(任何行业的通用模板都能自动识别, 换模板即适配)
        ③ 数据密度: 该 sheet 非空单元格数 < 阈值 -> 丢弃(信息量不足)
        ④ 正文过滤: 模板说明/封面/签署/变更履历/示例块整体剔除
        最终按 sheet 密度分配名额, 每个 sheet 内部等距抽样, 保证正文条目
        不被表头/术语区挤掉, 而不是只取前 N 块。

        同时剔除审批表单行(批准人/保存期限/密级等), 虽然这些术语是中文工程
        特有的, 但 WeKnora 面向通用文档没有这个问题; 保留正则是防御层,
        主要通用信号是 ②(检测到重复 -> 去掉)。
        """
        from collections import defaultdict

        import os as _os
        table_db = _os.path.join(
            "storage", "table_indexes", "departments", str(department_id), "kbs", kb_name, "table_indexes.db"
        )
        if not _os.path.exists(table_db):
            return []

        conn = sqlite3.connect(table_db)
        conn.row_factory = sqlite3.Row

        doc_sets: dict[str, set[str]] = defaultdict(set)
        for row in conn.execute("SELECT record_id, block_text, sheet_name FROM table_text_blocks"):
            if _TEMPLATE_SHEET_RE.search(str(row["sheet_name"])):
                continue
            doc_sets[str(row["block_text"])[:240]].add(str(row["record_id"]))
        boiler_keys = {key for key, doc_set in doc_sets.items() if len(doc_set) >= 3}

        blocks: list[dict] = []
        densities: dict[str, int] = {}
        for row in conn.execute(
            "SELECT sheet_name, non_empty_cell_count FROM table_sheets WHERE record_id = ?", (int(doc_id),)
        ):
            if _TEMPLATE_SHEET_RE.search(str(row["sheet_name"])):
                continue
            densities[str(row["sheet_name"])] = int(row["non_empty_cell_count"] or 0)
        for row in conn.execute(
            "SELECT block_index, block_text, sheet_name FROM table_text_blocks WHERE record_id = ?",
            (int(doc_id),),
        ):
            if _TEMPLATE_SHEET_RE.search(str(row["sheet_name"])):
                continue
            blocks.append(dict(row))
        conn.close()

        scored: list[tuple[str, str]] = []
        for b in blocks:
            text = str(b["block_text"])
            if text[:240] in boiler_keys:
                continue
            if _APPROVAL_RE.search(text) or _BOILERPLATE_TEXT_RE.search(text):
                continue
            density = densities.get(str(b["sheet_name"]), WIKI_MIN_CELL_DENSITY)
            if density < WIKI_MIN_CELL_DENSITY:
                continue
            scored.append((str(b["sheet_name"]), text))

        by_sheet: dict[str, list[str]] = defaultdict(list)
        for sheet, text in scored:
            by_sheet[sheet].append(text)
        if not by_sheet:
            return []

        # 按 sheet 密度分配素材名额, 每个 sheet 内等距抽样: 只按顺序取前 N 块
        # 会把摘要锁死在表头/术语/参考资料区, 后面的正文条目永远抽不到。
        total_density = sum(
            densities.get(s, WIKI_MIN_CELL_DENSITY) for s in by_sheet
        ) or 1
        alloc = {
            s: min(len(texts), max(1, round(
                WIKI_MAX_BLOCKS_PER_DOC * densities.get(s, WIKI_MIN_CELL_DENSITY) / total_density)))
            for s, texts in by_sheet.items()
        }
        while sum(alloc.values()) > WIKI_MAX_BLOCKS_PER_DOC:
            victims = [s for s in alloc if alloc[s] > 1]
            if not victims:
                break
            alloc[min(victims, key=lambda s: densities.get(s, 0))] -= 1
        for sheet in sorted(by_sheet, key=lambda s: -densities.get(s, 0)):
            if sum(alloc.values()) >= WIKI_MAX_BLOCKS_PER_DOC:
                break
            room = len(by_sheet[sheet]) - alloc[sheet]
            if room > 0:
                alloc[sheet] += min(room, WIKI_MAX_BLOCKS_PER_DOC - sum(alloc.values()))

        selected: list[str] = []
        for sheet in sorted(by_sheet, key=lambda s: -densities.get(s, 0)):
            texts = by_sheet[sheet]
            k = alloc[sheet]
            if k <= 0:
                continue
            indices = (
                [i * len(texts) // k for i in range(k)]
                if k < len(texts) else list(range(len(texts)))
            )
            selected.extend(texts[i] for i in indices)
        return selected[:WIKI_MAX_BLOCKS_PER_DOC]

    def _circuit_blocks(self, pipeline: Any, kb_name: str, *, file_name: str) -> list[str]:
        """电路网表 → 结构化摘要(规模/模块/电源时钟网/器件构成)。

        只喂确定性统计, 不喂全量网表: 网表动辄数千实例, 全量既进不了上下文
        也无法逐条引用。摘要与 CircuitQueryEngine.get_circuit_overview 同源,
        保证与 circuit_search 工具看到的是同一份索引。
        """
        from collections import Counter

        service = getattr(pipeline, "circuit_service", None)
        if service is None:
            service = getattr(pipeline, "circuit_indexes", None)
        if service is None:
            service = getattr(getattr(pipeline, "backend", None), "circuit_indexes", None)
        if service is None:
            return []
        engine = getattr(service, "query_engine", None)
        store = getattr(service, "store", None)

        designs: list[dict[str, Any]] = []
        if engine is not None and hasattr(engine, "list_designs"):
            try:
                designs = list(engine.list_designs(kb_name) or [])
            except Exception:  # noqa: BLE001 - 采集失败按无素材处理
                designs = []
        if not designs and store is not None:
            try:
                designs = [
                    {"design_id": d.design_id, "files": [f.file_name for f in d.files]}
                    for d in store.list_designs(kb_name)
                ]
            except Exception:  # noqa: BLE001
                designs = []
        match = next(
            (d for d in designs if file_name in [str(f) for f in d.get("files") or []]),
            None,
        )
        if match is None and len(designs) == 1:
            match = designs[0]
        if match is None:
            return []

        design_id = str(match.get("design_id") or "")
        design = None
        if store is not None and design_id:
            try:
                design = store.load(kb_name, design_id)
            except Exception:  # noqa: BLE001
                design = None
        overview = None
        if engine is not None and hasattr(engine, "get_circuit_overview") and design_id:
            try:
                overview = engine.get_circuit_overview(kb_name, design_id)
            except Exception:  # noqa: BLE001
                overview = None
        if overview is None and design is None:
            return []

        if overview:
            instances = int(overview.get("instance_count") or 0)
            nets = int(overview.get("net_count") or 0)
            modules = int(overview.get("module_count") or 0)
        else:
            instances, nets, modules = len(design.instances), len(design.nets), len(design.modules)

        first = [
            f"【电路网表结构化摘要】{file_name}",
            f"设计 ID：{design_id}",
            f"规模：实例 {instances} 个、网表 {nets} 个、模块 {modules} 个",
        ]
        module_rows = list((overview or {}).get("modules") or [])
        if not module_rows and design is not None:
            module_rows = [
                {
                    "name": m.name,
                    "module_id": m.module_id,
                    "instance_count": len(m.instances),
                    "net_count": len(m.nets),
                }
                for m in design.modules
            ]
        if module_rows:
            first.append("模块（按实例数前 20）：")
            for m in sorted(module_rows, key=lambda x: -int(x.get("instance_count") or 0))[:20]:
                first.append(
                    f"- {m.get('name') or m.get('module_id')}："
                    f"实例 {int(m.get('instance_count') or 0)}、网表 {int(m.get('net_count') or 0)}"
                )
        power_nets = [str(n).lstrip("&") for n in (overview or {}).get("power_nets") or []]
        clock_nets = [str(n).lstrip("&") for n in (overview or {}).get("clock_nets") or []]
        if design is not None:
            if not power_nets:
                power_nets = [n.name for n in design.nets if n.net_type in {"power", "ground"}]
            if not clock_nets:
                clock_nets = [n.name for n in design.nets if n.net_type == "clock"]
        power_nets = list(dict.fromkeys(power_nets))
        clock_nets = list(dict.fromkeys(clock_nets))
        if power_nets:
            first.append(f"电源/地网（{len(power_nets)} 个，前 30）：{'、'.join(power_nets[:30])}")
        if clock_nets:
            first.append(f"时钟网（{len(clock_nets)} 个，前 15）：{'、'.join(clock_nets[:15])}")

        blocks = ["\n".join(first)]
        if design is not None:
            def model_label(inst: Any) -> str:
                base = str(getattr(inst, "library_cell", None) or getattr(inst, "part_number", None) or "").strip()
                value = str(getattr(inst, "value", None) or "").replace("%%", "%").strip()
                return f"{base} {value}".strip()

            models = Counter(
                label for label in (model_label(inst) for inst in design.instances) if label
            )
            connectors = [
                str(inst.refdes)
                for inst in design.instances
                if str(inst.refdes or "").upper().startswith(("X", "J"))
            ]
            second = [f"【电路网表器件构成】{file_name}"]
            if models:
                second.append("主要器件（类型+参数，按实例数前 20）：")
                for name, count in models.most_common(20):
                    second.append(f"- {name} × {count}")
            if connectors:
                second.append(f"连接器位号（{len(connectors)} 个）：{'、'.join(sorted(connectors)[:30])}")
            warnings = [str(w) for w in (overview or {}).get("warnings") or []] or [
                str(w) for w in getattr(design, "parse_warnings", None) or []
            ]
            if warnings:
                second.append(f"解析告警（{len(warnings)} 条，前 5）：{'；'.join(warnings[:5])}")
            if len(second) > 1:
                blocks.append("\n".join(second))
        return [b for b in blocks if b.strip()]

    def gather_documents(
        self, pipeline: Any, kb_name: str, ctx: Any,
        *, kb_id: int, department_id: int,
        max_blocks_per_doc: int = WIKI_MAX_BLOCKS_PER_DOC,
    ) -> list[dict[str, Any]]:
        """确定性素材采集: 表格文件→高密度非样板 sheet 的 text_blocks;
        ragflow 文档 → 解析 chunks; 电路 → 结构化索引摘要(位号/型号统计)。
        与资产中心同一份数据链路, 保证页面可回溯到解析产物。"""
        from src.pipelines.document_rag.schemas import normalize_parse_status
        from src.pipelines.document_rag.schemas import TASK_STATUS_COMPLETED

        docs: list[dict[str, Any]] = []
        for info in pipeline.list_file_infos(kb_name, ctx=ctx):
            if normalize_parse_status(info.status, info.processor_kind) != TASK_STATUS_COMPLETED:
                continue
            # 页面/slug 稳定用本地主键; 取解析结果必须用资产中心的规范 file id
            # (ragflow:<pk>), 否则 get_parse_result 找不到记录。
            doc_id = str((info.metadata or {}).get("store_id") or info.id)
            kind = (info.processor_kind or "").lower()
            chunks: list[str] = []
            if "spreadsheet" in kind:
                rows = self._spreadsheet_blocks(doc_id, kb_id, department_id, kb_name)
                for b in rows[:max_blocks_per_doc]:
                    c = str(b).strip()
                    if c:
                        chunks.append(c[:2500])
            elif "circuit" in kind:
                for block in self._circuit_blocks(pipeline, kb_name, file_name=str(info.name)):
                    content = block.strip()
                    if content:
                        chunks.append(content[:2500])
            else:
                try:
                    result = pipeline.get_parse_result(kb_name, str(info.id), ctx=ctx)
                    for chunk in getattr(result, "chunks", []) or []:
                        content = str(getattr(chunk, "content", "") or "").strip()
                        if not content or _BOILERPLATE_TEXT_RE.search(content):
                            continue
                        chunks.append(content)
                        if len(chunks) >= 8:
                            break
                except Exception:  # noqa: BLE001 - 不可解析类型跳过
                    chunks = []
            if chunks:
                docs.append({
                    "doc_id": doc_id, "name": str(info.name),
                    "processor_kind": kind, "chunks": chunks,
                })
        return docs

    # -- ingest pipeline -----------------------------------------------------

    def _is_user_locked(self, kb_id: int, slug: str) -> bool:
        """人工编辑过的页视为锁定: 重蒸馏时跳过覆盖, 只读不上写。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT last_edit_source FROM wiki_pages WHERE kb_id = ? AND slug = ?",
                (kb_id, slug),
            ).fetchone()
            return row is not None and row["last_edit_source"] == "user"

    def ingest(
        self,
        *,
        kb_id: int,
        kb_name: str,
        department_id: int,
        documents: list[dict[str, Any]],
        chat_fn: Callable[[str], str],
        granularity: str = "standard",
        max_pages_per_ingest: int = 0,
        progress_fn: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """WeKnora-style pipeline: Pass0 -> citation -> summary + page writes -> post.

        ``documents`` items: {"doc_id": str, "name": str, "chunks": list[str]}
        (chunk text list; index is the chunk locator). Returns a stats dict.
        人工编辑过的页(last_edit_source='user')会被跳过(计数到 skipped_user_edited);
        想让管线重新接管某页, 回滚一次即可(revert 署名 'revert', 自动解锁)。
        ``progress_fn`` 接收阶段文案(供后台任务展示进度), 失败不影响蒸馏。
        """
        if granularity not in GRANULARITIES:
            granularity = "standard"

        def report(message: str) -> None:
            if progress_fn is None:
                return
            try:
                progress_fn(message)
            except Exception:  # noqa: BLE001 - 进度上报不能影响蒸馏
                pass

        stats = {"documents": len(documents), "candidates": 0, "cited": 0,
                 "pages_written": 0, "uncited": 0, "skipped_user_edited": 0,
                 "errors": []}
        slug_updates: dict[str, dict[str, Any]] = {}
        for doc_index, doc in enumerate(documents, start=1):
            text = "\n\n".join(str(c) for c in doc.get("chunks") or [] if str(c).strip())
            if not text.strip():
                continue
            report(f"蒸馏 {doc_index}/{len(documents)}: {str(doc.get('name') or '')[:40]}")
            try:
                candidates = self._pass0_candidates(chat_fn, text, granularity, kb_id)
                stats["candidates"] += len(candidates)
                cited, new_slugs, cited_texts = self._citation_pass(
                    chat_fn, doc.get("chunks") or [], candidates
                )
                stats["cited"] += sum(len(v) for v in cited.values())
                stats["uncited"] += sum(1 for c in candidates if not cited.get(c["slug"]))
                for c in candidates:
                    item = slug_updates.setdefault(
                        c["slug"], {**c, "chunks": [], "chunk_texts": [], "docs": set()}
                    )
                    item["docs"].add((str(doc["doc_id"]), str(doc["name"])))
                    cited_idx = cited.get(c["slug"], [])
                    item["chunks"].extend(cited_idx)
                    item["chunk_texts"].extend(
                        [str(cited_texts[i]) for i in cited_idx if 0 <= i < len(cited_texts)]
                    )
                for ns in new_slugs:
                    item = slug_updates.setdefault(
                        ns["slug"], {**ns, "chunks": [], "chunk_texts": [], "docs": set()}
                    )
                    item["docs"].add((str(doc["doc_id"]), str(doc["name"])))
                    ns_idx = ns.get("chunks", [])
                    item["chunks"].extend(ns_idx)
                    item["chunk_texts"].extend(
                        [str(cited_texts[i]) for i in ns_idx if 0 <= i < len(cited_texts)]
                    )
                # WeKnora: 每文档一页摘要(summary), 引用采样块;
                # 人工编辑过的页视为已锁定, 管线不再覆盖(防"改完→重跑→又坏")
                summary_slug = f"summary/{doc['doc_id']}"
                if self._is_user_locked(kb_id, summary_slug):
                    stats["skipped_user_edited"] += 1
                elif self._write_summary_page(
                    kb_id=kb_id, kb_name=kb_name, department_id=department_id, doc=doc, chat_fn=chat_fn,
                ):
                    stats["pages_written"] += 1
            except Exception as exc:  # noqa: BLE001 - fail-soft per document
                stats["errors"].append(f"{doc.get('name')}: {exc}")
        if max_pages_per_ingest and len(slug_updates) > max_pages_per_ingest:
            keep = sorted(slug_updates, key=lambda s: -len(slug_updates[s]["chunks"]))
            slug_updates = {s: slug_updates[s] for s in keep[:max_pages_per_ingest]}
        report(f"生成词条页 {len(slug_updates)} 个")
        for page_index, (slug, item) in enumerate(slug_updates.items(), start=1):
            if self._is_user_locked(kb_id, slug):
                stats["skipped_user_edited"] += 1
                continue
            report(f"写入词条 {page_index}/{len(slug_updates)}: {slug[:40]}")
            try:
                if self._write_page_from_item(
                    kb_id=kb_id, kb_name=kb_name, department_id=department_id,
                    slug=slug, item=item, chat_fn=chat_fn,
                ):
                    stats["pages_written"] += 1
                else:
                    stats["uncited"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"{slug}: {exc}")
        self.rebuild_links(kb_id=kb_id)
        self.cleanup_dead_links(kb_id=kb_id)
        self._ensure_index_page(kb_id=kb_id, kb_name=kb_name, department_id=department_id)
        return stats

    def _pass0_candidates(self, chat_fn: Callable[[str], str], text: str, granularity: str, kb_id: int) -> list[dict[str, Any]]:
        raw = chat_fn(CANDIDATE_PROMPT.format(text=_clip(text), granularity_hint=GRANULARITY_HINTS[granularity]))
        data = _parse_json_object(raw)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for kind, key in (("entity", "entities"), ("concept", "concepts")):
            for e in data.get(key) or []:
                name = str(e.get("name", "")).strip()
                if not name:
                    continue
                raw_slug = str(e.get("slug", "")).strip().lower()
                # slug 归一: 取末段 token, 修正跨类型前缀污染(如 concept/entity/r1618)
                token = raw_slug.rsplit("/", 1)[-1].strip("-")
                if not token:
                    token = slugify(name)
                slug = f"{kind}/{slugify(token)}"
                if slug in seen:
                    continue
                seen.add(slug)
                item: dict[str, Any] = {
                    "slug": slug, "name": name, "kind": kind,
                    "aliases": [str(a).strip() for a in e.get("aliases") or [] if str(a).strip()],
                    "description": str(e.get("description", "")).strip(),
                    "chunks": [],
                }
                if item["aliases"] and name not in item["aliases"]:
                    pass
                items.append(item)
        return items

    def _citation_pass(
        self, chat_fn: Callable[[str], str], chunks: list[str], candidates: list[dict[str, Any]]
    ) -> tuple[dict[str, list[int]], list[dict[str, Any]], list[str]]:
        """返回 (cited, new_slugs, batch_texts); cited 的索引指向 batch_texts,
        而 batch_texts 由原始 chunks 顺序合并而来, 因此引用块永不串位。"""
        batch_texts = _batch_chunks(chunks, MAX_CHUNK_CHARS_PER_BATCH)
        handles: dict[str, int] = {}
        rendered: list[str] = []
        for idx, batch in enumerate(batch_texts):
            handle = f"c{idx:03d}"
            handles[handle] = idx
            rendered.append(f"<{handle}>\n{batch}\n</{handle}>")
        candidates_xml = "\n".join(
            f"- slug: {c['slug']}, name: {c['name']}, type: {c['kind']}, aliases: {c['aliases']}"
            for c in candidates
        )
        raw = chat_fn(CITATION_PROMPT.format(candidates=candidates_xml, chunks="\n".join(rendered)))
        data = _parse_json_object(raw)
        cited: dict[str, list[int]] = {}
        for slug, handle_list in (data.get("citations") or {}).items():
            ids = [handles[h] for h in handle_list if h in handles]
            if ids:
                cited.setdefault(str(slug).strip().lower(), []).extend(ids)
        new_slugs: list[dict[str, Any]] = []
        existing = {c["slug"] for c in candidates}
        for ns in data.get("new_slugs") or []:
            name = str(ns.get("name", "")).strip()
            kind = "concept" if str(ns.get("type", "")).strip() == "concept" else "entity"
            token = str(ns.get("slug", "")).strip().lower().rsplit("/", 1)[-1].strip("-") or slugify(name)
            if not name or not token:
                continue
            slug = f"{kind}/{slugify(token)}"
            if slug in existing:
                continue
            new_slugs.append({
                "slug": slug, "name": name, "kind": kind,
                "aliases": [str(a).strip() for a in ns.get("aliases") or [] if str(a).strip()],
                "description": str(ns.get("description", "")).strip(),
                "chunks": [handles[h] for h in ns.get("source_chunks", []) if h in handles],
            })
            existing.add(slug)
        return cited, new_slugs, batch_texts

    def _write_summary_page(
        self, *, kb_id: int, kb_name: str, department_id: int,
        doc: dict[str, Any], chat_fn: Callable[[str], str],
    ) -> dict[str, Any] | None:
        """WeKnora summary: 每文档一页综述, 引用采样块, 幂等更新。"""
        chunks = [str(c) for c in doc.get("chunks") or [] if str(c).strip()]
        if not chunks:
            return None
        slug = f"summary/{doc['doc_id']}"
        material = "\n\n".join(f"[{i + 1}] {c[:1600]}" for i, c in enumerate(chunks[:8]))
        now = utc_now()
        content = chat_fn(SUMMARY_PROMPT.format(chunks=material)).strip()
        summary = _first_paragraph(content)
        source_refs = [f"{doc['doc_id']}|{doc.get('name') or ''}"]
        chunk_refs = sorted({f"{slug}#chunk-{i + 1}" for i in range(len(chunks[:8]))})
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE kb_id = ? AND slug = ?", (kb_id, slug)
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO wiki_pages (
                            department_id, kb_id, kb_name, slug, title, page_type, status,
                            content, summary, source_refs_json, chunk_refs_json,
                            version, last_edit_source, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'summary', 'published', ?, ?, ?, ?, 1, 'pipeline', ?, ?)
                        """,
                        (department_id, kb_id, kb_name, slug,
                         str(doc.get("name") or slug)[:200], content, summary,
                         json.dumps(source_refs, ensure_ascii=False),
                         json.dumps(chunk_refs, ensure_ascii=False), now, now),
                    )
                else:
                    if str(row["content"]).strip() == content.strip():
                        conn.execute("COMMIT")
                        return self._page_row(row)
                    self._snapshot_revision(conn, row, "pipeline", None, now)
                    conn.execute(
                        "UPDATE wiki_pages SET content = ?, summary = ?, source_refs_json = ?, "
                        "chunk_refs_json = ?, version = version + 1, last_edit_source = 'pipeline', "
                        "updated_at = ? WHERE id = ?",
                        (content, summary,
                         json.dumps(source_refs, ensure_ascii=False),
                         json.dumps(chunk_refs, ensure_ascii=False), now, row["id"]),
                    )
                page_id = int(row["id"]) if row is not None else int(
                    conn.execute("SELECT id FROM wiki_pages WHERE kb_id = ? AND slug = ?", (kb_id, slug)).fetchone()[0]
                )
                self._prune_revisions(conn, page_id)
                conn.execute("COMMIT")
                final = conn.execute("SELECT * FROM wiki_pages WHERE id = ?", (page_id,)).fetchone()
                return self._page_row(final)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _write_page_from_item(
        self, *, kb_id: int, kb_name: str, department_id: int, slug: str,
        item: dict[str, Any], chat_fn: Callable[[str], str],
    ) -> dict[str, Any] | None:
        now = utc_now()
        chunk_texts = [str(c) for c in item.get("chunk_texts") or [] if str(c).strip()][:8]
        if not chunk_texts:
            return None
        # 总材料预算 12000 字符按引用批次均分: 单批次文档能拿到全量素材,
        # 多批次也不至于把后面的块全截掉(旧实现固定 1500/块, 长文档只看到开头)。
        per_item = max(1500, MAX_CHUNK_CHARS_PER_BATCH // len(chunk_texts))
        material = "\n\n".join(f"[{i + 1}] {t[:per_item]}" for i, t in enumerate(chunk_texts))
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE kb_id = ? AND slug = ?", (kb_id, slug)
            ).fetchone()
        existing_content = row["content"] if row is not None else ""
        known_slugs = self._known_slugs(kb_id)
        known_slugs.add(slug)
        known_text = ", ".join(sorted(known_slugs))
        content = chat_fn(PAGE_PROMPT.format(
            title=item.get("name") or slug, existing=existing_content or "(尚无页面)",
            chunks=material, known_slugs=known_text,
        )).strip()
        summary = _first_paragraph(content)
        aliases = list(item.get("aliases") or [])
        if row is not None:
            aliases = list(dict.fromkeys(aliases + (json.loads(row["aliases_json"] or "[]"))))
        source_refs = sorted({f"{doc_id}|{doc_name}" for doc_id, doc_name in item.get("docs", set())})
        chunk_refs = sorted({f"{slug}#chunk-{i + 1}" for i in range(len(chunk_texts[:8]))})
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE kb_id = ? AND slug = ?", (kb_id, slug)
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO wiki_pages (
                            department_id, kb_id, kb_name, slug, title, page_type, status,
                            content, summary, aliases_json, source_refs_json, chunk_refs_json,
                            version, last_edit_source, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, ?, 1, 'pipeline', ?, ?)
                        """,
                        (department_id, kb_id, kb_name, slug, item.get("name") or slug,
                         item.get("kind", "entity"), content, summary,
                         json.dumps(aliases, ensure_ascii=False),
                         json.dumps(source_refs, ensure_ascii=False),
                         json.dumps(chunk_refs, ensure_ascii=False), now, now),
                    )
                else:
                    if str(row["content"]).strip() != content.strip():
                        self._snapshot_revision(conn, row, "pipeline", None, now)
                    merged_sources = sorted(set(source_refs) | set(json.loads(row["source_refs_json"] or "[]")))
                    merged_chunks = sorted(set(chunk_refs) | set(json.loads(row["chunk_refs_json"] or "[]")))
                    conn.execute(
                        """
                        UPDATE wiki_pages SET content = ?, summary = ?, aliases_json = ?,
                            source_refs_json = ?, chunk_refs_json = ?,
                            version = CASE WHEN ? THEN version ELSE version + 1 END,
                            last_edit_source = CASE WHEN ? THEN last_edit_source ELSE 'pipeline' END,
                            updated_at = ? WHERE id = ?
                        """,
                        (content, summary, json.dumps(aliases, ensure_ascii=False),
                         json.dumps(merged_sources, ensure_ascii=False),
                         json.dumps(merged_chunks, ensure_ascii=False),
                         int(str(row["content"]).strip() == content.strip()),
                         int(str(row["content"]).strip() == content.strip()),
                         now, row["id"]),
                    )
                page_id = int(row["id"]) if row is not None else int(
                    conn.execute("SELECT id FROM wiki_pages WHERE kb_id = ? AND slug = ?", (kb_id, slug)).fetchone()[0]
                )
                self._prune_revisions(conn, page_id)
                conn.execute("COMMIT")
                final = conn.execute("SELECT * FROM wiki_pages WHERE id = ?", (page_id,)).fetchone()
                return self._page_row(final)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def rebuild_links(self, *, kb_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._rebuild_links(conn, kb_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _rebuild_links(self, conn: sqlite3.Connection, kb_id: int) -> None:
        rows = conn.execute(
            "SELECT id, slug, title, aliases_json, content, status FROM wiki_pages WHERE kb_id = ? AND status = 'published'",
            (kb_id,),
        ).fetchall()
        surfaces: dict[str, list[str]] = {}
        for r in rows:
            surface = [r["slug"], r["title"]]
            surface.extend(json.loads(r["aliases_json"] or "[]"))
            surfaces[r["slug"]] = [s for s in surface if s]
        links: dict[int, set[str]] = {}
        for r in rows:
            mine = set()
            content = str(r["content"] or "")
            for target_slug, targets in surfaces.items():
                if target_slug == r["slug"]:
                    continue
                if f"[[{target_slug}]]" in content:
                    mine.add(target_slug)
                    continue
                for surface_form in targets:
                    if len(surface_form) >= 2 and surface_form in content:
                        mine.add(target_slug)
                        break
            links[r["id"]] = mine
        slug_by_id = {r["id"]: r["slug"] for r in rows}
        for r in rows:
            out_links = sorted(links.get(r["id"], set()))
            in_links = sorted(
                slug_by_id[other_id] for other_id, out in links.items()
                if other_id != r["id"] and r["slug"] in out
            )
            conn.execute(
                "UPDATE wiki_pages SET out_links_json = ?, in_links_json = ? WHERE id = ?",
                (json.dumps(out_links, ensure_ascii=False), json.dumps(in_links, ensure_ascii=False), r["id"]),
            )

    def cleanup_dead_links(self, *, kb_id: int) -> int:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT slug FROM wiki_pages WHERE kb_id = ? AND status = 'published'", (kb_id,)
            ).fetchall()
            live = {r["slug"] for r in rows}
            fixed = 0
            for slug in [r["slug"] for r in rows]:
                page = conn.execute(
                    "SELECT id, content, out_links_json FROM wiki_pages WHERE slug = ?", (slug,)
                ).fetchone()
                content = str(page["content"] or "")
                referenced = re.findall(r"\[\[([^\]]+)\]\]", content)
                dead = sorted({s for s in referenced if s not in live})
                out_links = json.loads(page["out_links_json"] or "[]")
                dead_links = sorted({s for s in out_links if s not in live})
                if not dead and not dead_links:
                    continue
                for s in dead:
                    content = content.replace(f"[[{s}]]", s)
                conn.execute(
                    "UPDATE wiki_pages SET content = ?, out_links_json = ? WHERE id = ?",
                    (content,
                     json.dumps([s for s in out_links if s in live], ensure_ascii=False),
                     page["id"]),
                )
                fixed += len(set(dead) | set(dead_links))
            return fixed

    def _ensure_index_page(self, *, kb_id: int, kb_name: str, department_id: int) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            pages = conn.execute(
                "SELECT slug, title, page_type, summary FROM wiki_pages WHERE kb_id = ? AND status = 'published' AND page_type != 'index' ORDER BY page_type, title",
                (kb_id,),
            ).fetchall()
            if not pages:
                return
            lines = [f"# {kb_name} 知识 Wiki", "", "本页由管线自动维护: 按类型列出全部词条(含文档摘要/实体/概念)。", ""]
            current = ""
            for p in pages:
                if p["page_type"] != current:
                    current = p["page_type"]
                    label = {"summary": "文档摘要", "entity": "实体", "concept": "概念"}.get(current, current)
                    lines.append(f"## {label}")
                    lines.append("")
                summary = f" — {p['summary']}" if p["summary"] else ""
                lines.append(f"- [[{p['slug']}]] {p['title']}{summary}")
            content = "\n".join(lines)
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE kb_id = ? AND slug = 'index'", (kb_id,)
            ).fetchone()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO wiki_pages (
                            department_id, kb_id, kb_name, slug, title, page_type, status,
                            content, summary, version, last_edit_source, created_at, updated_at
                        ) VALUES (?, ?, ?, 'index', '知识索引', 'index', 'published', ?, '', 1, 'pipeline', ?, ?)
                        """,
                        (department_id, kb_id, kb_name, content, now, now),
                    )
                else:
                    self._snapshot_revision(conn, row, "pipeline", None, now)
                    conn.execute(
                        "UPDATE wiki_pages SET content = ?, version = version + 1, last_edit_source = 'pipeline', updated_at = ? WHERE id = ?",
                        (content, now, row["id"]),
                    )
                self._rebuild_links(conn, kb_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # -- internals -----------------------------------------------------------

    def _known_slugs(self, kb_id: int) -> set[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT slug FROM wiki_pages WHERE kb_id = ?", (kb_id,)).fetchall()
            return {r["slug"] for r in rows}

    def _snapshot_revision(
        self, conn: sqlite3.Connection, row: sqlite3.Row, edit_source: str,
        editor_id: int | None, now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO wiki_page_revisions (
                page_id, version, title, page_type, status, content, summary,
                aliases_json, edit_source, editor_id, edited_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["id"], row["version"], row["title"], row["page_type"], row["status"],
             row["content"], row["summary"], row["aliases_json"], edit_source,
             editor_id, row["updated_at"], now),
        )

    def _prune_revisions(self, conn: sqlite3.Connection, page_id: int) -> None:
        keep_from = conn.execute(
            "SELECT MIN(version) FROM (SELECT version FROM wiki_page_revisions WHERE page_id = ? ORDER BY version DESC LIMIT ?)",
            (page_id, MAX_REVISIONS_PER_PAGE),
        ).fetchone()[0]
        if keep_from is not None:
            conn.execute(
                "DELETE FROM wiki_page_revisions WHERE page_id = ? AND version < ?",
                (page_id, keep_from),
            )

    def _page_row(self, row: sqlite3.Row, content: bool = True) -> dict[str, Any]:
        data = dict(row)
        for key in ("aliases_json", "source_refs_json", "chunk_refs_json", "in_links_json", "out_links_json"):
            if key in data:
                data[key.replace("_json", "")] = json.loads(data.pop(key) or "[]")
        if not content:
            data.pop("content", None)
        return data
