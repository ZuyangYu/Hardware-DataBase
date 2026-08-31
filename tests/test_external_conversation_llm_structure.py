import unittest

from src.external_conversations import llm_structure
import src.settings
from src.external_conversations.models import ConversationTurn


class _FakeLLM:
    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.calls: list = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


_VALID = (
    '```json\n{"title": "LDO 压差讨论", "turns": ['
    '{"role": "user", "content": "压差要求是什么?"}, '
    '{"role": "assistant", "content": "最大 0.3V"}]}```'
)


class LlmStructureTests(unittest.TestCase):
    def setUp(self):
        self._old_flag = src.settings.EXTERNAL_CONVERSATION_LLM_STRUCTURE
        self.addCleanup(setattr, src.settings, "EXTERNAL_CONVERSATION_LLM_STRUCTURE", self._old_flag)

    def test_parses_fenced_json_into_turns_and_title(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_STRUCTURE = True
        result = llm_structure.infer_structure("一段" * 60 + "没有标记的对话文本", llm_client=_FakeLLM(_VALID))
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "LDO 压差讨论")
        roles = [t.role for t in result["turns"]]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(result["turns"][0].start_offset, -1)

    def test_disabled_flag_returns_none_without_calling_llm(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_STRUCTURE = False
        fake = _FakeLLM(_VALID)
        self.assertIsNone(llm_structure.infer_structure("一段" * 60 + "文本", llm_client=fake))
        self.assertEqual(fake.calls, [])

    def test_short_text_skips_llm(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_STRUCTURE = True
        fake = _FakeLLM(_VALID)
        self.assertIsNone(llm_structure.infer_structure("太短了", llm_client=fake))
        self.assertEqual(fake.calls, [])

    def test_llm_failure_returns_none(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_STRUCTURE = True
        result = llm_structure.infer_structure("一段" * 60 + "文本", llm_client=_FakeLLM(RuntimeError("boom")))
        self.assertIsNone(result)

    def test_garbage_json_returns_none(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_STRUCTURE = True
        result = llm_structure.infer_structure("一段" * 60 + "文本", llm_client=_FakeLLM("抱歉我不能输出 JSON"))
        self.assertIsNone(result)

    def test_invalid_roles_filtered(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_STRUCTURE = True
        bad = '{"title": "t", "turns": [{"role": "system", "content": "x"}, {"role": "user", "content": ""}, {"role": "user", "content": "ok"}]}'
        result = llm_structure.infer_structure("一段" * 60 + "文本", llm_client=_FakeLLM(bad))
        self.assertIsNotNone(result)
        self.assertEqual([t.role for t in result["turns"]], ["user"])

    def test_conversation_turn_model_unchanged(self):
        turn = ConversationTurn(role="user", content="hi")
        self.assertEqual(turn.role, "user")


if __name__ == "__main__":
    unittest.main()


class SummarizeContentTests(unittest.TestCase):
    def setUp(self):
        self._old = src.settings.EXTERNAL_CONVERSATION_LLM_SUMMARY
        self.addCleanup(setattr, src.settings, "EXTERNAL_CONVERSATION_LLM_SUMMARY", self._old)

    def test_summarize_returns_summary_and_points(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_SUMMARY = True
        reply = '{"summary": "讨论了LDO压差与静态电流。", "key_points": ["最大压差0.3V", "静态电流12uA"]}'
        result = llm_structure.summarize_content("一段" * 60, llm_client=_FakeLLM(reply))
        self.assertIsNotNone(result)
        self.assertIn("LDO", result["summary"])
        self.assertEqual(len(result["key_points"]), 2)

    def test_summarize_disabled_returns_none(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_SUMMARY = False
        self.assertIsNone(llm_structure.summarize_content("一段" * 60, llm_client=_FakeLLM("{}")))

    def test_summarize_garbage_returns_none(self):
        src.settings.EXTERNAL_CONVERSATION_LLM_SUMMARY = True
        self.assertIsNone(llm_structure.summarize_content("一段" * 60, llm_client=_FakeLLM("不会")))
