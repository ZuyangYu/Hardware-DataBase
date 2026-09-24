import os
import tempfile
import unittest

from src.external_conversations.models import ConversationTurn, ExternalConversation
from src.external_conversations.parsers import parse_external_conversation
from src.external_conversations.store import ExternalConversationStore


def _conversation(department_id: str, kb_name: str, cid: str = "c_1") -> ExternalConversation:
    return ExternalConversation(
        conversation_id=cid,
        kb_name=kb_name,
        department_id=department_id,
        title="标题",
        source_file="chat.md",
        content_hash="hash123",
        turns=[ConversationTurn(role="user", content="hi", start_offset=0, end_offset=2)],
        blocks=["b"],
    )


class ExternalConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ext_conv_store_")
        self.store = ExternalConversationStore(root=self.tmp)

    def test_save_and_load_roundtrip_preserves_turns_and_raw_copy(self):
        conv = _conversation("dept_1", "kb_a")
        self.store.save(conv, raw_bytes="用户: hi".encode("utf-8"), raw_ext=".md")
        loaded = self.store.load("dept_1", "kb_a", "c_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.turns[0].content, "hi")
        raw_path = os.path.join(
            self.tmp, "departments", "dept_1", "kbs", "kb_a", "c_1", "original.md"
        )
        self.assertTrue(os.path.exists(raw_path))

    def test_scope_dir_matches_spreadsheet_layout(self):
        path = self.store.scope_dir("dept_1", "kb_a")
        expected_tail = os.path.join("departments", "dept_1", "kbs", "kb_a")
        self.assertTrue(path.endswith(expected_tail), path)
        # default root is STORAGE_DIR/external_conversations
        default_store = ExternalConversationStore()
        self.assertTrue(default_store.root.endswith(os.path.join("external_conversations")))

    def test_same_kb_name_across_departments_isolated(self):
        for dept in ("dept_1", "dept_2"):
            self.store.save(_conversation(dept, "shared_kb", cid=f"c_{dept}"), b"x", ".txt")
        self.assertEqual(
            [c.conversation_id for c in self.store.list_conversations("dept_1", "shared_kb")],
            ["c_dept_1"],
        )
        self.assertIsNotNone(self.store.load("dept_2", "shared_kb", "c_dept_2"))
        self.assertIsNone(self.store.load("dept_1", "shared_kb", "c_dept_2"))

    def test_delete_conversation_removes_directory(self):
        self.store.save(_conversation("dept_1", "kb_a"), b"x", ".txt")
        self.assertTrue(self.store.delete_conversation("dept_1", "kb_a", "c_1"))
        self.assertIsNone(self.store.load("dept_1", "kb_a", "c_1"))
        self.assertFalse(self.store.delete_conversation("dept_1", "kb_a", "c_1"))

    def test_delete_kb_removes_only_that_department_tree(self):
        self.store.save(_conversation("dept_1", "kb_a"), b"x", ".txt")
        self.store.save(_conversation("dept_2", "kb_a"), b"x", ".txt")
        self.assertTrue(self.store.delete_kb("dept_1", "kb_a"))
        self.assertIsNone(self.store.load("dept_1", "kb_a", "c_1"))
        self.assertIsNotNone(self.store.load("dept_2", "kb_a", "c_1"))

    def test_path_traversal_rejected(self):
        with self.assertRaises(Exception):
            self.store.scope_dir("../evil", "kb_a")
        with self.assertRaises(Exception):
            self.store.scope_dir("dept_1", "../evil")
        # locating a record by a traversal id fails soft: no data, no error
        self.assertIsNone(self.store.load("dept_1", "kb_a", "../../escape"))
        self.assertFalse(self.store.delete_conversation("dept_1", "kb_a", "../../escape"))

    def test_department_required(self):
        with self.assertRaises(ValueError):
            self.store.save(_conversation("", "kb_a"), b"x", ".txt")

    def test_parse_and_store_integration(self):
        src = os.path.join(self.tmp, "in.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write("用户: 内容\n助手: 回复\n")
        conv = parse_external_conversation(src, "in.md", "kb_a", department_id="dept_1")
        self.store.save(conv, raw_bytes=open(src, "rb").read(), raw_ext=".md")
        loaded = self.store.load("dept_1", "kb_a", conv.conversation_id)
        self.assertEqual(loaded.conversation_id, conv.conversation_id)
        self.assertEqual(len(loaded.turns), 2)


if __name__ == "__main__":
    unittest.main()
