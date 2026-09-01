"""MultiSourceAgentRunner.stream 观测包装冒烟测试。

合并后 runner 换成 deepagents 门面，观测包装（observe.agent + 指标）是本分支
回植的关键路径；此处用假模型/假 agent 验证流式输出、answer/summary 记录与
指标调用不抛异常。
"""

from __future__ import annotations

from unittest.mock import Mock

from langchain_core.messages import AIMessage

from src.agents.runner import MultiSourceAgentRunner


class _FakeRAGBackend:
    name = "fake"

    def retrieve(self, *args, **kwargs):
        return []


def test_stream_wrapper_emits_deltas_and_records_answer(monkeypatch):
    from src.agents import runner as runner_mod

    chunks = [AIMessage(content="你好"), AIMessage(content="，世界")]
    fake_agent = type("F", (), {})()
    fake_agent.stream = lambda *args, **kwargs: iter((chunk, {"langgraph_node": "model"}) for chunk in chunks)

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: fake_agent)
    record_agent = Mock()
    monkeypatch.setattr(runner_mod, "record_agent", record_agent)

    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    deltas = list(
        runner.stream(query="问题", kb_name="kb_hw", history=[], thread_id="t1")
    )

    assert deltas == ["你好", "，世界"]
    record = runner_mod._current_run()
    assert record.answer == "你好，世界"
    summary = runner.get_last_retrieval_summary()
    assert summary["status"] == "no_evidence"
    assert summary["retriever_type"] == "multi_source_agent"
    assert runner.get_last_footer()
    record_agent.assert_called_once()
    assert record_agent.call_args.kwargs["mode"] == "deep"


def test_stream_wrapper_marks_failure_status(monkeypatch):
    from src.agents import runner as runner_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("模型炸了")

    fake_agent = type("F", (), {})()
    fake_agent.stream = _boom
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: fake_agent)
    record_agent = Mock()
    monkeypatch.setattr(runner_mod, "record_agent", record_agent)

    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend())
    try:
        list(runner.stream(query="问题", kb_name="kb_hw", history=[]))
        raised = False
    except RuntimeError:
        raised = True

    assert raised
    record_agent.assert_called_once()
    assert record_agent.call_args.kwargs["status"] == "failed"


def test_general_chat_runs_through_agent_without_retrieval_tools(monkeypatch):
    """未挂载知识库的通用对话与 KB 问答共用同一条 deepagents 链路：
    同一个模型接口与系统提示词来源，只是不注册知识库检索工具。"""
    from src.agents import runner as runner_mod

    captured: dict = {}

    def _fake_create_deep_agent(**kwargs):
        captured.update(kwargs)

        class _Agent:
            def stream(self, *args, **kwargs):
                return iter(())

        return _Agent()

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())

    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend())
    def _tool_name(tool) -> str:
        return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))

    for kb_name in ("", "__general__"):
        captured.clear()
        list(runner.stream(query="你好", kb_name=kb_name, history=[], thread_id="t1"))

        tool_names = [_tool_name(tool) for tool in captured["tools"]]
        assert tool_names == ["memory_search"]
        assert "当前未挂载知识库" in captured["system_prompt"]

    # 挂载知识库时仍然注册全部检索工具。
    captured.clear()
    list(runner.stream(query="问题", kb_name="kb_hw", history=[], thread_id="t1"))
    tool_names = [_tool_name(tool) for tool in captured["tools"]]
    assert "document_search" in tool_names
    assert "circuit_search" in tool_names
    assert "当前未挂载知识库" not in captured["system_prompt"]


def test_runner_prefetches_bounded_memory_context_before_agent_creation():
    from src.agents.tools.runtime import ToolRuntime
    from src.pipelines.document_rag.schemas import RequestContext

    services = []

    class _MemoryService:
        def __init__(self):
            self.closed = False
            services.append(self)

        def search(self, query, **kwargs):
            assert query == "历史电源方案"
            assert kwargs["scope"] == "all"
            assert kwargs["status"] == "all"
            return [{"id": "m1", "scope": "project", "status": "candidate", "content": {"content": "仅作历史线索"}}]

        def format_context(self, rows):
            assert len(rows) == 1
            return "<untrusted_memory>\n[M1]仅作历史线索\n</untrusted_memory>"

        def close(self):
            self.closed = True

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        memory_service_factory=_MemoryService,
    )
    context = runner._prefetch_memory_context(
        query="历史电源方案",
        kb_name="design",
        ctx=RequestContext(user_id="engineer"),
        rt=ToolRuntime(kb_name="design", ctx=RequestContext(user_id="engineer")),
    )

    assert "仅作历史线索" in context
    assert len(services) == 1
    assert services[0].closed is True
