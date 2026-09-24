from __future__ import annotations

import json
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, ConfigDict

from src.core.chat_model_runtime import (
    StructuredOutputCapabilityError,
    StructuredOutputValidationError,
    TextResponseError,
    RuntimeObservedChatModel,
    instrument_chat_model,
    invoke_structured,
    invoke_text,
    model_provider,
)


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _Runnable:
    def __init__(self, response):
        self.response = response

    def invoke(self, messages):
        return self.response


class _Model:
    provider = "custom"
    model_name = "runtime-test"

    def __init__(self, response):
        self.response = response
        self.structured_schema = None

    def invoke(self, messages):
        return self.response

    def with_structured_output(self, schema):
        self.structured_schema = schema
        return _Runnable(self.response)


class _ToolCapableModel(BaseChatModel):
    model_name: str = "memory-test"
    provider: str = "custom"
    invocations: ClassVar[int] = 0

    @property
    def _llm_type(self) -> str:
        return "test_tool_capable"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        del messages, stop, run_manager, kwargs
        type(self).invocations += 1
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        del tools, tool_choice, kwargs
        return RunnableLambda(lambda _messages: AIMessage(content="", tool_calls=[]))


def test_invoke_text_extracts_message_content_and_usage():
    result = invoke_text(
        _Model(AIMessage(
            content="hello",
            usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        )),
        [{"role": "user", "content": "hi"}],
        operation="test_text",
        profile="default",
    )

    assert result.text == "hello"
    assert result.usage.usage_returned is True
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 2
    assert result.usage.total_tokens == 5


def test_invoke_text_keeps_missing_usage_unknown():
    result = invoke_text(
        _Model(AIMessage(content="hello")),
        [{"role": "user", "content": "hi"}],
        operation="test_text",
        profile="default",
    )

    assert result.text == "hello"
    assert result.usage.usage_returned is False
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.total_tokens is None


def test_runtime_normalizes_langchain_openai_adapter_to_custom_provider():
    model = _Model(AIMessage(content="hello"))
    model.provider = "openai"

    assert model_provider(model) == "custom"


def test_invoke_text_rejects_unextractable_response():
    with pytest.raises(TextResponseError):
        invoke_text(
            _Model(object()),
            [{"role": "user", "content": "hi"}],
            operation="test_text",
            profile="default",
        )


def test_invoke_structured_validates_native_payload():
    model = _Model({"value": "native"})

    result = invoke_structured(
        model,
        _Payload,
        [{"role": "user", "content": "return a value"}],
        operation="test_structured",
        profile="default",
    )

    assert result.value == _Payload(value="native")
    assert result.mode == "native"
    assert model.structured_schema is _Payload


def test_invoke_structured_uses_explicit_text_compatibility_fallback():
    class TextOnlyModel(_Model):
        def with_structured_output(self, schema):
            raise NotImplementedError("structured output is not supported")

        def invoke(self, messages):
            return AIMessage(content=json.dumps({"value": "fallback"}))

    result = invoke_structured(
        TextOnlyModel(None),
        _Payload,
        [{"role": "user", "content": "return a value"}],
        operation="test_structured",
        profile="default",
        text_fallback=lambda text: _Payload.model_validate(json.loads(text)),
    )

    assert result.value == _Payload(value="fallback")
    assert result.mode == "text_compat"


def test_invoke_structured_without_fallback_exposes_capability_error():
    class TextOnlyModel(_Model):
        def with_structured_output(self, schema):
            raise NotImplementedError("structured output is not supported")

    with pytest.raises(StructuredOutputCapabilityError):
        invoke_structured(
            TextOnlyModel(None),
            _Payload,
            [{"role": "user", "content": "return a value"}],
            operation="test_structured",
            profile="default",
        )


def test_invoke_structured_converts_capability_failure_during_invoke():
    class BoundTextOnlyModel(_Model):
        def with_structured_output(self, schema):
            class _UnsupportedRunnable:
                def invoke(self, messages):
                    raise NotImplementedError("tool calling is not supported")

            return _UnsupportedRunnable()

    with pytest.raises(StructuredOutputCapabilityError):
        invoke_structured(
            BoundTextOnlyModel(None),
            _Payload,
            [{"role": "user", "content": "return a value"}],
            operation="test_structured",
            profile="default",
        )


def test_invoke_structured_rejects_invalid_native_payload():
    with pytest.raises(StructuredOutputValidationError):
        invoke_structured(
            _Model({"unexpected": "field"}),
            _Payload,
            [{"role": "user", "content": "return a value"}],
            operation="test_structured",
            profile="default",
        )


def test_runtime_observed_chat_model_preserves_tool_bound_responses():
    raw = _ToolCapableModel()
    model = instrument_chat_model(raw, operation="memory_reflection", profile="memory")

    assert isinstance(model, RuntimeObservedChatModel)
    assert model.invoke([{"role": "user", "content": "remember"}]).content == "ok"
    bound = model.bind_tools([{"type": "function", "function": {"name": "Memory"}}])
    response = bound.invoke([{"role": "user", "content": "remember"}])

    assert isinstance(response, AIMessage)
    assert response.tool_calls == []
