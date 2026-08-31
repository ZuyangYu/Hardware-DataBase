import tempfile
import unittest

from src.pipelines.document_rag.schemas import RequestContext
from src.external_conversations.models import ConversationTurn, ExternalConversation
from src.external_conversations.query_engine import ExternalConversationQueryEngine
from src.agents.tools.external_conversation_tools import ExternalConversationSearchTool


def _seed(engine, department_id, kb_name, cid):
    engine.index_conversation(
        ExternalConversation(
            conversation_id=cid,
            kb_name=kb_name,
            department_id=department_id,
            title=f"标题-{cid}",
            source_file=f"{cid}.md",
            content_hash="h",
            source_group="外部数据",
            turns=[
                ConversationTurn(role="user", content="LDO 的压差要求是什么?", start_offset=0, end_offset=10),
                ConversationTurn(role="assistant", content="最大压差 0.3V,注意散热。", start_offset=10, end_offset=30),
            ],
        )
    )


class ExternalConversationSearchToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ext_conv_tool_")
        self.engine = ExternalConversationQueryEngine(root=self.tmp)
        _seed(self.engine, "dept_1", "kb_a", "c1")
        self.tool = ExternalConversationSearchTool(self.engine)

    def _ctx(self, department_id="dept_1"):
        return RequestContext(
            user_id="u1",
            metadata={"department_id": department_id},
        )

    def test_tool_returns_evidence_with_locator_and_metadata(self):
        evidences = self.tool.run("LDO 压差", "kb_a", self._ctx(), top_k=5)
        self.assertTrue(evidences)
        ev = evidences[0]
        self.assertEqual(ev.content_kind, "external_conversation")
        self.assertEqual(ev.processor_kind, "external_conversation")
        self.assertIn("conversation_id", ev.locator)
        self.assertIn("start_offset", ev.locator)
        self.assertEqual(ev.metadata.get("origin"), "upload")

    def test_tool_requires_department_scope(self):
        with self.assertRaises(PermissionError):
            self.tool.run("任意查询", "kb_a", None, top_k=5)

    def test_tool_swallows_engine_errors(self):
        class BoomEngine:
            def search_by_scope(self, *args, **kwargs):
                raise RuntimeError("boom")

        tool = ExternalConversationSearchTool(BoomEngine())
        self.assertEqual(tool.run("查询", "kb_a", self._ctx(), top_k=5), [])

    def test_tool_scoped_by_department(self):
        _seed(self.engine, "dept_2", "kb_a", "other")
        rows = self.tool.run("LDO 压差", "kb_a", self._ctx(department_id="dept_2"), top_k=5)
        self.assertTrue(rows)
        self.assertTrue(all(ev.metadata.get("department_id") == "dept_2" for ev in rows))


if __name__ == "__main__":
    unittest.main()
