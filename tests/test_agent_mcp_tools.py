"""T5: MCP tools and events through ``MultiSourceAgentRunner.stream``.

Uses a fake agent/model (same pattern as ``test_agent_runner_stream.py``) so
the assertions target the runner's assembly and the guarded executor, not the
LLM.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import Mock, patch

import pytest
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage

import src.settings as settings
from src.agents import runner as runner_mod
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.client import reset_mcp_runtime
from src.agents.mcp.store import SqliteMcpServerStore
from src.pipelines.document_rag.schemas import RequestContext
from tests._mcp_stub import McpStubServer


class _FakeRAGBackend:
    name = "fake"

    def retrieve(self, *args, **kwargs):
        return []


def _patches(db_path: str, key: str, *, mcp_enabled: bool = True):
    return [
        patch.object(settings, "AUTH_DB_PATH", db_path),
        patch.object(settings, "MCP_CREDENTIALS_KEY", key),
        patch.object(settings, "MCP_ENABLED", mcp_enabled),
        patch.object(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True),
        patch.object(settings, "RESOURCE_GOVERNOR_ENABLED", True),
        patch.object(settings, "MCP_ALLOW_INSECURE_HTTP", True),
        patch.object(settings, "MCP_ALLOW_PRIVATE_HOSTS", True),
        patch.object(settings, "MCP_CONNECT_TIMEOUT_SECONDS", 10),
        patch.object(settings, "MCP_CALL_TIMEOUT_SECONDS", 5),
    ]


def _seed_server(store: SqliteMcpServerStore, url: str) -> None:
    store.create(
        slug="stub",
        name="桩服务",
        transport="streamable_http",
        url=url,
        department_scope="departments",
        department_ids=[7],
        created_by="admin",
    )
    store.set_enabled("stub", True, "admin")
    store.upsert_subscription(42, "stub")


def _stream(monkeypatch, captured: dict, events: list, *, kb_name: str = "kb_hw"):
    chunks = [AIMessage(content="最终回答")]
    fake_agent = type("F", (), {})()
    fake_agent.stream = lambda *args, **kwargs: iter(
        (chunk, {"langgraph_node": "model"}) for chunk in chunks
    )

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return fake_agent

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())

    runner = runner_mod.MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    ctx = RequestContext(
        user_id="alice", roles=["employee"], metadata={"department_id": 7}
    )
    deltas = list(
        runner.stream(
            query="问题",
            kb_name=kb_name,
            history=[],
            ctx=ctx,
            thread_id="t1",
            event_callback=events.append,
            attachment_user_id=42,
        )
    )
    return runner, deltas


def _cleanup():
    reset_mcp_runtime()
    get_tool_cache().clear_all()


def test_runner_injects_mcp_tools_and_prompt(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        key = Fernet.generate_key().decode("ascii")
        store = SqliteMcpServerStore(os.path.join(tmp, "auth.db"))
        patches = _patches(os.path.join(tmp, "auth.db"), key)
        for item in patches:
            item.start()
        try:
            with McpStubServer() as stub:
                _seed_server(store, stub.url)
                captured: dict = {}
                events: list = []
                runner, deltas = _stream(monkeypatch, captured, events)

                names = {getattr(tool, "name", "") for tool in captured["tools"]}
                assert "mcp__stub__echo" in names
                assert "mcp__stub__writer" not in names
                assert "外部插件结果说明" in captured["system_prompt"]

                # The captured sync tool is the real guarded executor path.
                echo = next(tool for tool in captured["tools"] if getattr(tool, "name", "") == "mcp__stub__echo")
                result = echo.invoke({"text": "端到端"})
                assert "端到端" in str(result)

                summary = runner.get_last_retrieval_summary()
                assert summary["evidence"] == []
                # MCP calls must stay out of the retrieval ledger: the only
                # possible diagnostic here is the pre-existing memory prefetch.
                assert not any(
                    str(item.get("tool_name", "")).startswith("mcp__")
                    for item in summary["tool_diagnostics"]
                )
                assert deltas == ["最终回答"]
        finally:
            for item in reversed(patches):
                item.stop()
            _cleanup()

    mcp_events = [
        event for event in events if (event.get("payload") or {}).get("tool_kind") == "mcp"
    ]
    assert [event["type"] for event in mcp_events].count("tool_started") == 1
    assert [event["type"] for event in mcp_events].count("tool_result") == 1


def test_runner_omits_mcp_tools_when_disabled(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        key = Fernet.generate_key().decode("ascii")
        db_path = os.path.join(tmp, "auth.db")
        store = SqliteMcpServerStore(db_path)
        patches = _patches(db_path, key, mcp_enabled=False)
        for item in patches:
            item.start()
        try:
            with McpStubServer() as stub:
                _seed_server(store, stub.url)
                captured: dict = {}
                runner, _ = _stream(monkeypatch, captured, [])
                names = {getattr(tool, "name", "") for tool in captured["tools"]}
                assert not any(name.startswith("mcp__") for name in names)
                assert "外部插件结果说明" not in captured["system_prompt"]
        finally:
            for item in reversed(patches):
                item.stop()
            _cleanup()


def test_general_chat_keeps_mcp_timeline_events(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        key = Fernet.generate_key().decode("ascii")
        db_path = os.path.join(tmp, "auth.db")
        store = SqliteMcpServerStore(db_path)
        patches = _patches(db_path, key)
        for item in patches:
            item.start()
        try:
            with McpStubServer() as stub:
                _seed_server(store, stub.url)
                captured: dict = {}
                events: list = []
                _stream(monkeypatch, captured, events, kb_name="")
                echo = next(
                    tool for tool in captured["tools"] if getattr(tool, "name", "") == "mcp__stub__echo"
                )
                echo.invoke({"text": "通用对话"})
        finally:
            for item in reversed(patches):
                item.stop()
            _cleanup()

    kinds = [
        event["type"]
        for event in events
        if (event.get("payload") or {}).get("tool_kind") == "mcp"
    ]
    assert "tool_started" in kinds
    assert "tool_result" in kinds


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
