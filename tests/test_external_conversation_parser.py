import os
import tempfile
import unittest

from src.external_conversations.models import ConversationTurn, ExternalConversation
from src.external_conversations.parsers import parse_external_conversation


def _write_tmp(root: str, filename: str, content: str) -> str:
    path = os.path.join(root, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class ExternalConversationParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ext_conv_parser_")

    def test_parses_user_assistant_markers_into_turns(self):
        path = _write_tmp(
            self.tmp,
            "chat.md",
            "用户: LDO 的压差是多少?\nassistant: 最大压差 0.3V,见 datasheet 第 4 页。\n"
            "用户: 那静态电流呢?\nAI: 典型值 12uA。\n",
        )
        conv = parse_external_conversation(path, "chat.md", "kb_a", department_id="dept_1")
        self.assertEqual(len(conv.turns), 4)
        roles = [t.role for t in conv.turns]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        self.assertIn("0.3V", conv.turns[1].content)

    def test_parses_qa_and_timestamped_markers(self):
        path = _write_tmp(
            self.tmp,
            "qa.txt",
            "[2026-08-25 10:00] Q: 上电时序要求?\nA: 先 3V3 后 1V8,间隔至少 10ms。\n"
            "问: 复位极性?\n答: 低电平有效。\n",
        )
        conv = parse_external_conversation(path, "qa.txt", "kb_a", department_id="dept_1")
        self.assertEqual(len(conv.turns), 4)
        self.assertEqual(conv.turns[0].role, "user")
        self.assertEqual(conv.turns[0].ts, "2026-08-25 10:00")
        self.assertEqual(conv.turns[1].role, "assistant")

    def test_markdown_headings_split_topic_blocks(self):
        path = _write_tmp(
            self.tmp,
            "notes.md",
            "# 电源设计\nLDO 选型说明。\n\n# 接口定义\nUART 波特率 115200。\n",
        )
        conv = parse_external_conversation(path, "notes.md", "kb_a", department_id="dept_1")
        self.assertEqual(conv.turns, [])
        self.assertEqual(len(conv.blocks), 2)
        self.assertIn("LDO", conv.blocks[0])
        self.assertIn("UART", conv.blocks[1])

    def test_plain_text_falls_back_to_single_block(self):
        path = _write_tmp(self.tmp, "plain.txt", "只是一段没有结构的记录文字。")
        conv = parse_external_conversation(path, "plain.txt", "kb_a", department_id="dept_1")
        self.assertEqual(conv.turns, [])
        self.assertEqual(len(conv.blocks), 1)
        self.assertIn("没有结构", conv.blocks[0])

    def test_empty_file_yields_empty_conversation(self):
        path = _write_tmp(self.tmp, "empty.txt", "")
        conv = parse_external_conversation(path, "empty.txt", "kb_a", department_id="dept_1")
        self.assertEqual(conv.turns, [])
        self.assertEqual(conv.blocks, [])

    def test_offsets_recorded_for_each_turn(self):
        path = _write_tmp(self.tmp, "chat.txt", "用户: A?\n助手: B。\n")
        conv = parse_external_conversation(path, "chat.txt", "kb_a", department_id="dept_1")
        raw = open(path, encoding="utf-8").read()
        for turn in conv.turns:
            self.assertGreaterEqual(turn.start_offset, 0)
            self.assertLessEqual(turn.end_offset, len(raw))
            self.assertIn(turn.content, raw[turn.start_offset : turn.end_offset])

    def test_same_name_different_content_yields_distinct_ids(self):
        p1 = _write_tmp(self.tmp, "same.md", "用户: 第一版内容")
        p2 = _write_tmp(self.tmp, "same_2.md", "用户: 第二版内容不一样")
        c1 = parse_external_conversation(p1, "same.md", "kb_a", department_id="dept_1")
        c2 = parse_external_conversation(p2, "same.md", "kb_a", department_id="dept_1")
        self.assertNotEqual(c1.conversation_id, c2.conversation_id)

    def test_pure_cjk_filename_yields_valid_conversation_id(self):
        """Regression: 对话.md used to collapse to '__' and fail id validation."""
        p = _write_tmp(self.tmp, "对话.md", "用户: 内容\n助手: 回复\n")
        conv = parse_external_conversation(p, "对话.md", "kb_a", department_id="dept_1")
        self.assertRegex(conv.conversation_id, r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
        self.assertTrue(conv.conversation_id.endswith("_" + conv.content_hash[:12]))

    def test_model_roundtrip_via_dict(self):
        conv = ExternalConversation(
            conversation_id="c1",
            kb_name="kb_a",
            department_id="dept_1",
            title="标题",
            source_file="c1.md",
            content_hash="abc123",
            origin="upload",
            source_group="外部数据",
            turns=[ConversationTurn(role="user", content="hi", ts="", start_offset=0, end_offset=6)],
            blocks=["block"],
        )
        revived = ExternalConversation.from_dict(conv.to_dict())
        self.assertEqual(revived, conv)


if __name__ == "__main__":
    unittest.main()
