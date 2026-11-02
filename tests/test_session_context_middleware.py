"""Session continuity middleware appends a compact, idempotent block."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import SystemMessage

from src.agents.middleware.session_context import SessionContextMiddleware


class _Request:
    def __init__(self, content: str):
        self.system_message = SystemMessage(content=content)

    def override(self, **kwargs):
        return SimpleNamespace(system_message=kwargs["system_message"])


def test_session_context_middleware_appends_block():
    captured = {}
    middleware = SessionContextMiddleware("<session_state>上一轮：TC377</session_state>")

    def handler(request):
        captured["system"] = request.system_message.content
        return "ok"

    result = middleware.wrap_model_call(_Request("base prompt"), handler)

    assert result == "ok"
    assert captured["system"].startswith("base prompt")
    assert "<session_state>上一轮：TC377</session_state>" in captured["system"]


def test_session_context_middleware_is_idempotent():
    block = "<session_state>上一轮：TC377</session_state>"
    middleware = SessionContextMiddleware(block)
    seen = []

    def handler(request):
        seen.append(request.system_message.content)
        return "ok"

    middleware.wrap_model_call(_Request(f"base\n\n{block}"), handler)

    assert seen[0].count("<session_state>") == 1


def test_session_context_middleware_without_block_passes_through():
    middleware = SessionContextMiddleware("")
    request = _Request("base")

    result = middleware.wrap_model_call(request, lambda value: value)

    assert result is request
