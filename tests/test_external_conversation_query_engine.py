import os
import tempfile
import unittest

from src.external_conversations.models import ConversationTurn, ExternalConversation
from src.external_conversations.query_engine import ExternalConversationQueryEngine


def _conversation(department_id: str, kb_name: str, cid: str, question: str, answer: str) -> ExternalConversation:
    return ExternalConversation(
        conversation_id=cid,
        kb_name=kb_name,
        department_id=department_id,
        title=f"标题-{cid}",
        source_file=f"{cid}.md",
        content_hash="h",
        turns=[
            ConversationTurn(role="user", content=question, start_offset=0, end_offset=len(question)),
            ConversationTurn(role="assistant", content=answer, start_offset=10, end_offset=30),
        ],
        blocks=[],
    )


class ExternalConversationQueryEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ext_conv_qe_")
        self.engine = ExternalConversationQueryEngine(root=self.tmp)

    def _seed(self, department_id: str, kb_name: str, cid: str, q: str, a: str):
        self.engine.index_conversation(_conversation(department_id, kb_name, cid, q, a))

    def test_index_and_search_returns_ranked_messages(self):
        self._seed("dept_1", "kb_a", "c1", "LDO 压差是多少?", "最大压差 0.3V。")
        self._seed("dept_1", "kb_a", "c2", "CAN 波特率配置?", "使用 500kbps。")
        rows = self.engine.search_by_scope("dept_1", "kb_a", "LDO 压差", top_k=5)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["conversation_id"], "c1")
        self.assertIn("content", rows[0])
        self.assertIn("role", rows[0])

    def test_search_isolated_between_departments_with_same_kb_name(self):
        self._seed("dept_1", "shared", "d1c", "LDO 压差问题", "答案一")
        self._seed("dept_2", "shared", "d2c", "完全无关的问题", "答案二")
        rows = self.engine.search_by_scope("dept_1", "shared", "LDO 压差")
        ids = {r["conversation_id"] for r in rows}
        self.assertEqual(ids, {"d1c"})
        listing = self.engine.list_conversations("dept_2", "shared")
        self.assertEqual([c["conversation_id"] for c in listing], ["d2c"])

    def test_search_empty_index_returns_empty_list(self):
        self.assertEqual(self.engine.search_by_scope("dept_1", "nope", "任何词"), [])

    def test_delete_conversation_cascades_messages(self):
        self._seed("dept_1", "kb_a", "c1", "问题", "回答")
        self.assertTrue(self.engine.delete_conversation("dept_1", "kb_a", "c1"))
        self.assertEqual(self.engine.search_by_scope("dept_1", "kb_a", "问题"), [])
        self.assertFalse(self.engine.delete_conversation("dept_1", "kb_a", "c1"))

    def test_get_conversation_returns_meta_and_preview(self):
        self._seed("dept_1", "kb_a", "c1", "问题内容", "回答内容")
        meta = self.engine.get_conversation("dept_1", "kb_a", "c1")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["title"], "标题-c1")
        self.assertEqual(meta["turn_count"], 2)

    def test_reindex_is_idempotent(self):
        conv = _conversation("dept_1", "kb_a", "c1", "LDO 压差", "压差回答")
        self.engine.index_conversation(conv)
        first = self.engine.search_by_scope("dept_1", "kb_a", "LDO 压差")
        self.engine.index_conversation(conv)
        second = self.engine.search_by_scope("dept_1", "kb_a", "LDO 压差")
        def strip_id(rows):
            return [{k: v for k, v in r.items() if k != "message_id"} for r in rows]

        self.assertEqual(strip_id(first), strip_id(second))

    def test_rebuild_kb_recovers_from_deleted_index_db(self):
        store_stub_records = [
            _conversation("dept_1", "kb_a", f"c{i}", f"LDO 问题{i}", f"压差答案{i}") for i in range(3)
        ]

        class StoreStub:
            def list_conversations(self, dept, kb):
                return list(store_stub_records)

        self.engine.index_conversation(_conversation("dept_1", "kb_a", "seed", "seed 问题", "seed 回答"))
        db_path = self.engine.db_path("dept_1", "kb_a")
        self.assertTrue(os.path.exists(db_path))
        os.remove(db_path)
        result = self.engine.rebuild_kb(StoreStub(), "dept_1", "kb_a")
        self.assertEqual(result["rebuilt"], 3)
        self.assertEqual(result["failed"], [])
        rows = self.engine.search_by_scope("dept_1", "kb_a", "LDO 问题")
        self.assertEqual({r["conversation_id"] for r in rows}, {"c0", "c1", "c2"})


if __name__ == "__main__":
    unittest.main()


class SummaryPersistenceTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp(prefix="ext_conv_sum_")
        self.engine = ExternalConversationQueryEngine(root=self.tmp)

    def test_summary_columns_added_to_legacy_index_db(self):
        """Simulate an index.db created before summaries existed."""
        import sqlite3

        db_path = self.engine.db_path("dept_1", "kb_a")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, department_id TEXT NOT NULL DEFAULT '',"
            " kb_id INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '', source_file TEXT NOT NULL DEFAULT '',"
            " origin TEXT NOT NULL DEFAULT 'upload', source_group TEXT NOT NULL DEFAULT '', turn_count INTEGER NOT NULL DEFAULT 0,"
            " block_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO conversations (conversation_id, department_id, title) VALUES ('old', 'dept_1', '旧记录')")
        conn.commit()
        conn.close()

        self._seed = self.engine.index_conversation(
            _conversation("dept_1", "kb_a", "c1", "问题", "回答")
        )
        meta = self.engine.get_conversation("dept_1", "kb_a", "c1")
        self.assertEqual(meta["summary"], "")

    def test_update_summary_persists_and_list_returns_it(self):
        self.engine.index_conversation(_conversation("dept_1", "kb_a", "c1", "问题", "回答"))
        ok = self.engine.update_summary("dept_1", "kb_a", "c1", "讨论了压差。", ["0.3V"], "2026-08-25")
        self.assertTrue(ok)
        listing = self.engine.list_conversations("dept_1", "kb_a")[0]
        self.assertEqual(listing["summary"], "讨论了压差。")
        self.assertEqual(listing["key_points"], ["0.3V"])
        self.assertEqual(listing["summary_generated_at"], "2026-08-25")

    def test_index_preserves_existing_summary_on_reindex(self):
        conv = _conversation("dept_1", "kb_a", "c1", "LDO 压差", "0.3V")
        conv.summary = "已有摘要"
        conv.key_points = ["kp1"]
        conv.summary_generated_at = "2026-08-25"
        self.engine.index_conversation(conv)
        rows = self.engine.search_by_scope("dept_1", "kb_a", "LDO 压差")
        self.assertTrue(rows)


class MixedLanguageTokenizerTests(unittest.TestCase):
    def test_ascii_word_inside_chinese_sentence_is_searchable(self):
        """Regression: 'AI网管的token是怎么管理的' must hit rows containing token."""
        import tempfile

        from src.external_conversations.models import ConversationTurn, ExternalConversation

        tmp = tempfile.mkdtemp(prefix="ext_conv_mix_")
        engine = ExternalConversationQueryEngine(root=tmp)
        engine.index_conversation(
            ExternalConversation(
                conversation_id="c1",
                kb_name="ADAS",
                department_id="47",
                title="t",
                source_file="c1.md",
                content_hash="h",
                turns=[
                    ConversationTurn(role="user", content="开通管理员权限", start_offset=0),
                    ConversationTurn(role="assistant", content="会议主题：AE产品部门公共token管理", start_offset=8),
                ],
            )
        )
        # note: user typo 网管 (instead of 网关) must still find the token row
        rows = engine.search_by_scope("47", "ADAS", "AI网管的token是怎么管理的？", top_k=5)
        self.assertTrue(rows)
        self.assertIn("token管理", rows[0]["content"])
