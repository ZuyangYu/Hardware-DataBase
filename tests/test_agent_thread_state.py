"""Phase 1 会话状态持久化（LangGraph checkpointer）回归测试：

- checkpointer 进程单例与 thread 寻址；
- thread 模式：同一 session 两轮查询，第二轮模型能看到第一轮的完整消息；
- stateless 默认路径不受影响；
- 存量会话首次播种（DB 历史 → thread 状态）；
- forget_thread 丢弃 thread 状态。
"""

import os
import shutil
import tempfile
import unittest

from langchain_core.language_models import BaseChatModel
from pydantic import Field
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from src.agents import runner as runner_mod
import src.settings as app_settings


class _RecordingChatModel(BaseChatModel):
    """记录每次模型调用收到的消息；bind_tools 原样返回自身（不产生工具调用）。"""

    received: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="好的"))])

    @property
    def _llm_type(self) -> str:
        return "recording-fake"

    def bind_tools(self, tools, **kwargs):
        return self


class _FakeRAGBackend:
    name = "fake"

    def retrieve(self, *args, **kwargs):
        return []


class AgentThreadStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hdb-thread-")
        self._original_path = app_settings.AGENT_CHECKPOINT_DB_PATH
        self._original_saver = runner_mod._CHECKPOINT_SAVER
        app_settings.AGENT_CHECKPOINT_DB_PATH = os.path.join(self._tmp, "ckpt.db")
        runner_mod._CHECKPOINT_SAVER = None

    def tearDown(self):
        app_settings.AGENT_CHECKPOINT_DB_PATH = self._original_path
        runner_mod._CHECKPOINT_SAVER = self._original_saver
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _runner(self):
        return runner_mod.MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)

    def _stream(self, runner, model, query, *, thread_id, persist_thread, history=None):
        original = runner_mod.create_chat_model
        runner_mod.create_chat_model = lambda: model
        try:
            list(
                runner.stream(
                    query=query,
                    kb_name="",
                    history=list(history or []),
                    thread_id=thread_id,
                    persist_thread=persist_thread,
                )
            )
        finally:
            runner_mod.create_chat_model = original

    def test_checkpointer_is_process_singleton(self):
        self.assertIs(runner_mod._get_checkpointer(), runner_mod._get_checkpointer())

    def test_thread_mode_restores_prior_turns(self):
        model = _RecordingChatModel()
        runner = self._runner()

        self._stream(runner, model, "第一轮问题", thread_id="77", persist_thread=True)
        self._stream(runner, model, "第二轮问题", thread_id="77", persist_thread=True)

        self.assertEqual(len(model.received), 2)
        second_turn_contents = [str(m.content) for m in model.received[1]]
        self.assertIn("第二轮问题", second_turn_contents)
        # 第一轮的 user 消息与 assistant 回复由 checkpointer 恢复
        self.assertIn("第一轮问题", second_turn_contents)
        self.assertIn("好的", second_turn_contents)
        # 第一轮只包含本轮新消息（无历史播种时）
        first_turn_contents = [str(m.content) for m in model.received[0]]
        self.assertIn("第一轮问题", first_turn_contents)
        self.assertNotIn("第二轮问题", first_turn_contents)

    def test_stateless_path_does_not_restore(self):
        model = _RecordingChatModel()
        runner = self._runner()

        self._stream(runner, model, "第一轮问题", thread_id="77", persist_thread=False)
        self._stream(runner, model, "第二轮问题", thread_id="77", persist_thread=False)

        second_turn_contents = [str(m.content) for m in model.received[1]]
        self.assertIn("第二轮问题", second_turn_contents)
        self.assertNotIn("第一轮问题", second_turn_contents)

    def test_first_touch_seeds_existing_history(self):
        model = _RecordingChatModel()
        runner = self._runner()

        # 存量会话：thread 尚无 checkpoint，把 DB 近期历史一次性播种
        self._stream(
            runner,
            model,
            "本轮问题",
            thread_id="88",
            persist_thread=True,
            history=[("旧问题", "旧回答")],
        )

        contents = [str(m.content) for m in model.received[0]]
        self.assertIn("旧问题", contents)
        self.assertIn("旧回答", contents)
        self.assertIn("本轮问题", contents)

    def test_forget_thread_drops_state(self):
        model = _RecordingChatModel()
        runner = self._runner()

        self._stream(runner, model, "第一轮问题", thread_id="99", persist_thread=True)
        config = {"configurable": {"thread_id": "99"}}
        self.assertIsNotNone(runner_mod._get_checkpointer().get_tuple(config))

        runner_mod.forget_thread("99")

        self.assertIsNone(runner_mod._get_checkpointer().get_tuple(config))

    def test_forget_thread_ignores_empty_id(self):
        runner_mod.forget_thread("")  # 不应抛异常
        runner_mod.forget_thread("   ")


if __name__ == "__main__":
    unittest.main()
