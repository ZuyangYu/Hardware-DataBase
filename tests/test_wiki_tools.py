"""Tests for the wiki retrieval tool: ranking, link expansion, evidence mapping."""
from __future__ import annotations

import gc
import json
import os
import tempfile
import unittest
from contextlib import closing

import src.settings
from src.agents.tools.wiki_tools import _page_evidence
from src.core.auth import AuthService, ROLE_EMPLOYEE
from src.core.wiki import WikiService


class WikiSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_password = src.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self._tmp = tempfile.TemporaryDirectory(prefix="_test_wiki_search_")
        self.db_path = os.path.join(self._tmp.name, "wiki.db")
        self.auth = AuthService(db_path=self.db_path)
        system_admin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        self.department = self.auth.create_department("hw")
        self.admin = self.auth.create_user_as(
            system_admin, "hw_admin", "password123", ROLE_EMPLOYEE, self.department.id
        )
        self.auth.register_knowledge_base("KB-W", owner=self.admin)
        self.kb_id = self.auth.get_knowledge_base_id("KB-W", department_id=self.department.id)
        self.service = WikiService(db_path=self.db_path)

    def tearDown(self) -> None:
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = self.old_password
        self._tmp.cleanup()
        gc.collect()

    def _page(
        self, slug: str, title: str, *, page_type: str = "entity",
        content: str = "", summary: str = "", aliases: list[str] | None = None,
        out_links: list[str] | None = None, in_links: list[str] | None = None,
    ) -> None:
        with closing(self.service._connect()) as conn:
            conn.execute(
                """
                INSERT INTO wiki_pages (
                    department_id, kb_id, kb_name, slug, title, page_type, status,
                    content, summary, aliases_json, in_links_json, out_links_json,
                    version, last_edit_source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, ?, 1, 'pipeline', ?, ?)
                """,
                (
                    self.department.id, self.kb_id, "KB-W", slug, title, page_type,
                    content, summary,
                    json.dumps(aliases or [], ensure_ascii=False),
                    json.dumps(in_links or [], ensure_ascii=False),
                    json.dumps(out_links or [], ensure_ascii=False),
                    "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                ),
            )
            conn.commit()

    def _search(self, query: str, **kwargs):
        return self.service.search_pages(
            kb_id=self.kb_id, department_id=self.department.id, query=query, **kwargs
        )

    def test_title_beats_content_and_alias_matches(self) -> None:
        self._page("entity/tcan1145", "TCAN1145", content="车规 CAN 收发器")
        self._page("entity/can", "CAN 总线", content="本项目使用 TCAN1145 收发器")
        self._page("concept/transceiver", "收发器选型", aliases=["TCAN1145"])
        results = self._search("TCAN1145", expand_links=False)
        slugs = [row["slug"] for row in results]
        self.assertEqual(slugs[0], "entity/tcan1145")
        self.assertIn("entity/can", slugs)
        self.assertIn("concept/transceiver", slugs)
        self.assertGreater(results[0]["score"], results[-1]["score"])

    def test_multi_token_query_matches_split_terms(self) -> None:
        self._page("entity/eyeq6l", "EYEQ6L", content="视觉处理芯片 功能安全 ASIL-B")
        self._page("entity/other", "其他器件", content="无关内容")
        slugs = [row["slug"] for row in self._search("EYEQ6L 安全", expand_links=False)]
        self.assertEqual(slugs, ["entity/eyeq6l"])

    def test_index_page_excluded_and_like_escaped(self) -> None:
        self._page("index", "索引", page_type="index", content="TCAN1145 汇总")
        self._page("entity/tcan1145", "TCAN1145", content="车规收发器")
        slugs = [row["slug"] for row in self._search("TCAN1145", expand_links=False)]
        self.assertNotIn("index", slugs)
        # 通配符按字面处理: "%" 不应命中文档
        self.assertEqual(self._search("%", expand_links=False), [])

    def test_link_expansion_one_hop(self) -> None:
        self._page("entity/eyeq6l", "EYEQ6L", content="视觉处理芯片", out_links=["entity/lpddr4"])
        self._page("entity/lpddr4", "LPDDR4", content="内存", in_links=["entity/eyeq6l"])
        self._page("entity/unrelated", "无关联", content="别的东西")
        results = self._search("EYEQ6L", expand_links=True)
        by_slug = {row["slug"]: row for row in results}
        self.assertIn("entity/lpddr4", by_slug)
        self.assertEqual(by_slug["entity/lpddr4"]["hop"], 1)
        self.assertEqual(by_slug["entity/lpddr4"]["via"], "entity/eyeq6l")
        self.assertLess(by_slug["entity/lpddr4"]["score"], by_slug["entity/eyeq6l"]["score"])
        self.assertNotIn("entity/unrelated", by_slug)
        # 关闭扩展时不应带出邻居
        limited = self._search("EYEQ6L", expand_links=False)
        self.assertEqual([row["slug"] for row in limited], ["entity/eyeq6l"])

    def test_page_evidence_mapping(self) -> None:
        self._page(
            "entity/tcan1145", "TCAN1145", content="车规收发器",
            summary="CAN 收发器", out_links=["entity/can"],
        )
        row = self._search("TCAN1145", expand_links=False)[0]
        evidence = _page_evidence(row)
        self.assertEqual(evidence.id, "wiki:entity/tcan1145")
        self.assertEqual(evidence.source_name, "Wiki: TCAN1145")
        self.assertEqual(evidence.content_kind, "wiki_page")
        self.assertEqual(evidence.processor_kind, "wiki")
        self.assertEqual(evidence.locator["slug"], "entity/tcan1145")
        self.assertEqual(evidence.locator["hop"], 0)
        self.assertIn("entity/can", evidence.metadata["out_links"])


if __name__ == "__main__":
    unittest.main()
