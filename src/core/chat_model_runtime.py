"""Small, provider-neutral invocation boundary for LangChain chat models.

The model factory owns provider construction and retry configuration. This
module owns response normalization and one observation/metric record per
actual model invocation. Domain modules decide what a failed call means for
their workflow; this module never changes business state or fallback policy.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ConfigDict, Field

from src.observability import observe
from src.observability.metrics import record_llm


class ChatModelLike(Protocol):
    """The narrow model surface used by application call sites."""

    def invoke(self, messages: object, **kwargs: object) -> object: ...

    def stream(self, messages: object, **kwargs: object) -> Iterable[object]: ...

    def with_structured_output(self, schema: type[BaseModel]) -> object: ...


class ChatModelRuntimeError(RuntimeError):
    """Stable base error for a failed runtime boundary."""


class ModelInvocationError(ChatModelRuntimeError):
    """The provider/runnable raised while executing a model call."""


class TextResponseError(ChatModelRuntimeError):
    """The provider returned a response with no extractable text."""


class StructuredOutputCapabilityError(ChatModelRuntimeError):
    """The provider or runnable cannot provide native structured output."""


class StructuredOutputValidationError(ChatModelRuntimeError):
    """A native or compatibility structured response failed Pydantic validation."""


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_returned: bool = False


@dataclass(frozen=True)
class TextCallResult:
    text: str
    response: Any
    usage: TokenUsage
    provider: str
    model: str
    operation: str
    profile: str


StructuredT = TypeVar("StructuredT", bound=BaseModel)


@dataclass(frozen=True)
class StructuredCallResult(Generic[StructuredT]):
    value: StructuredT
    mode: str
    response: Any
    usage: TokenUsage
    provider: str
    model: str
    operation: str
    profile: str


def invoke_observed(
    runnable: Any,
    messages: Any,
    *,
    operation: str,
    profile: str,
    identity_model: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Invoke an arbitrary LangChain runnable with the shared LLM observation.

    Structured/tool-bound runnables can return an ``AIMessage`` whose text is
    intentionally empty. They therefore cannot use ``invoke_text`` merely for
    instrumentation; this helper records the call while preserving the full
    provider response for LangMem/tool orchestration.
    """

    response, _usage = _observed_invoke(
        runnable,
        messages,
        operation=operation,
        profile=profile,
        streaming=False,
        identity_model=identity_model,
        **kwargs,
    )
    return response


class _RuntimeObservedRunnable(Runnable[Any, Any]):
    """Proxy a tool-bound runnable without changing its response contract."""

    def __init__(self, runnable: Any, *, identity_model: Any, operation: str, profile: str):
        self._runnable = runnable
        self._identity_model = identity_model
        self._operation = operation
        self._profile = profile

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        call_kwargs = dict(kwargs)
        if config is not None:
            call_kwargs["config"] = config
        return invoke_observed(
            self._runnable,
            input,
            operation=self._operation,
            profile=self._profile,
            identity_model=self._identity_model,
            **call_kwargs,
        )


class RuntimeObservedChatModel(BaseChatModel):
    """BaseChatModel proxy that observes direct and tool-bound invocations."""

    wrapped_model: Any = Field(exclude=True)
    operation: str = Field(default="memory_reflection", exclude=True)
    profile: str = Field(default="memory", exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "hdb_runtime_observed_chat_model"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "provider": model_provider(self.wrapped_model),
            "model": model_name(self.wrapped_model),
            "profile": self.profile,
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        call_kwargs = dict(kwargs)
        if stop is not None:
            call_kwargs["stop"] = stop
        response = invoke_observed(
            self.wrapped_model,
            messages,
            operation=self.operation,
            profile=self.profile,
            identity_model=self.wrapped_model,
            **call_kwargs,
        )
        if isinstance(response, ChatResult):
            return response
        if isinstance(response, BaseMessage):
            message = response
        else:
            message = AIMessage(content=extract_text(response) or "")
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: str | None = None, **kwargs: Any) -> Runnable[Any, Any]:
        bound = self.wrapped_model.bind_tools(tools, tool_choice=tool_choice, **kwargs)
        return _RuntimeObservedRunnable(
            bound,
            identity_model=self.wrapped_model,
            operation=self.operation,
            profile=self.profile,
        )

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable[Any, Any]:
        bound = self.wrapped_model.with_structured_output(schema, **kwargs)
        return _RuntimeObservedRunnable(
            bound,
            identity_model=self.wrapped_model,
            operation=self.operation,
            profile=self.profile,
        )


def instrument_chat_model(
    model: Any,
    *,
    operation: str,
    profile: str,
) -> Any:
    """Wrap a real LangChain chat model while leaving injected test doubles intact."""

    if isinstance(model, RuntimeObservedChatModel):
        return model
    if isinstance(model, BaseChatModel):
        return RuntimeObservedChatModel(
            wrapped_model=model,
            operation=operation,
            profile=profile,
        )
    return model


def invoke_text(
    model: ChatModelLike,
    messages: Sequence[Mapping[str, Any]],
    *,
    operation: str,
    profile: str,
    **kwargs: Any,
) -> TextCallResult:
    """Invoke a chat model and normalize its response to text."""

    response, usage = _observed_invoke(
        model,
        messages,
        operation=operation,
        profile=profile,
        streaming=False,
        **kwargs,
    )
    text = extract_text(response)
    if text is None or not text.strip():
        raise TextResponseError(
            f"{operation} returned no extractable text from {_model_identity(model)}"
        )
    return TextCallResult(
        text=text,
        response=response,
        usage=usage,
        provider=model_provider(model),
        model=model_name(model),
        operation=operation,
        profile=profile,
    )


def invoke_structured(
    model: ChatModelLike,
    schema: type[StructuredT],
    messages: Sequence[Mapping[str, Any]],
    *,
    operation: str,
    profile: str,
    text_fallback: Callable[[str], StructuredT | BaseModel | Mapping[str, Any]] | None = None,
    **kwargs: Any,
) -> StructuredCallResult[StructuredT]:
    """Prefer native structured output and use only an explicit text fallback."""

    try:
        structured_model = model.with_structured_output(schema)
    except Exception as exc:
        if not _is_structured_capability_error(exc):
            raise ModelInvocationError(
                f"{operation} structured capability probe failed: {exc}"
            ) from exc
        if text_fallback is not None:
            return _invoke_text_fallback(
                model, schema, messages, operation=operation, profile=profile,
                text_fallback=text_fallback, **kwargs,
            )
        raise StructuredOutputCapabilityError(
            f"{operation} provider does not support structured output: {exc}"
        ) from exc

    if structured_model is None or not hasattr(structured_model, "invoke"):
        error = TypeError("with_structured_output returned no invokable runnable")
        if text_fallback is not None:
            return _invoke_text_fallback(
                model, schema, messages, operation=operation, profile=profile,
                text_fallback=text_fallback, **kwargs,
            )
        raise StructuredOutputCapabilityError(str(error)) from error

    try:
        response, usage = _observed_invoke(
            structured_model,
            messages,
            operation=operation,
            profile=profile,
            streaming=False,
            identity_model=model,
            **kwargs,
        )
    except ModelInvocationError as exc:
        if text_fallback is not None and _is_structured_capability_error(exc):
            return _invoke_text_fallback(
                model, schema, messages, operation=operation, profile=profile,
                text_fallback=text_fallback, **kwargs,
            )
        if _is_structured_capability_error(exc):
            raise StructuredOutputCapabilityError(
                f"{operation} provider does not support structured output: {exc}"
            ) from exc
        raise

    try:
        value = coerce_structured_response(response, schema)
    except StructuredOutputValidationError:
        raise
    except Exception as exc:
        raise StructuredOutputValidationError(
            f"{operation} structured response is invalid: {exc}"
        ) from exc
    return StructuredCallResult(
        value=value,
        mode="native",
        response=response,
        usage=usage,
        provider=model_provider(model),
        model=model_name(model),
        operation=operation,
        profile=profile,
    )


def stream_text(
    model: ChatModelLike,
    messages: Sequence[Mapping[str, Any]],
    *,
    operation: str,
    profile: str,
    **kwargs: Any,
) -> Iterable[str]:
    """Yield normalized text chunks with one stream observation."""

    provider = model_provider(model)
    model_id = model_name(model)
    started = time.monotonic()
    first_delta_at: float | None = None
    usage = _empty_usage_accumulator()
    status = "success"
    observation = observe.llm(
        "hdb.llm.runtime", provider=provider, model=model_id,
        stage=operation, profile=profile, streaming=True,
    )
    try:
        with observation:
            try:
                stream = model.stream(messages, **kwargs)
                for chunk in stream:
                    _accumulate_usage(usage, chunk)
                    text = extract_text(chunk) or ""
                    if text:
                        if first_delta_at is None:
                            first_delta_at = time.monotonic()
                        yield text
                if usage["returned"]:
                    observation.tokens(
                        input_tokens=usage["input"], output_tokens=usage["output"],
                        total_tokens=usage["total"],
                    )
                observation.set("hdb.usage_returned", bool(usage["returned"]))
                observation.outcome("success")
            except Exception as exc:
                status = "error"
                wrapped = ModelInvocationError(
                    f"{operation} stream failed for {_model_identity(model)}: {exc}"
                )
                observation.error(wrapped)
                observation.outcome("error")
                raise wrapped from exc
    finally:
        record_llm(
            provider=provider, status=status,
            duration_s=time.monotonic() - started, streaming=True,
            ttft_s=(first_delta_at - started if first_delta_at is not None else None),
        )


def extract_text(response: Any) -> str | None:
    """Extract text from common LangChain/provider response shapes only."""

    if isinstance(response, str):
        return response
    if response is None:
        return None
    if isinstance(response, Mapping):
        for key in ("content", "text", "output_text", "delta"):
            if key in response:
                value = extract_text(response[key])
                if value is not None:
                    return value
        return None
    if isinstance(response, (list, tuple)):
        parts: list[str] = []
        for item in response:
            value = extract_text(item)
            if value is not None:
                parts.append(value)
        return "".join(parts) if parts else None
    content = getattr(response, "content", None)
    if content is not None:
        value = extract_text(content)
        if value is not None:
            return value
    text = getattr(response, "text", None)
    if text is not None and not callable(text):
        value = extract_text(text)
        if value is not None:
            return value
    return None


def coerce_structured_response(response: Any, schema: type[StructuredT]) -> StructuredT:
    """Validate all structured response shapes through the requested schema."""

    if isinstance(response, schema):
        return response
    payload: Any = response
    if isinstance(payload, Mapping) and "parsed" in payload:
        payload = payload["parsed"]
    if isinstance(payload, schema):
        return payload
    if isinstance(payload, Mapping):
        try:
            return schema.model_validate(payload)
        except Exception as exc:
            raise StructuredOutputValidationError(str(exc)) from exc
    text = extract_text(payload)
    if text is None:
        raise StructuredOutputValidationError(
            f"response type {type(response).__name__} has no structured payload"
        )
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputValidationError(
            f"structured response is not JSON: {exc}"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise StructuredOutputValidationError("structured response JSON is not an object")
    try:
        return schema.model_validate(decoded)
    except Exception as exc:
        raise StructuredOutputValidationError(str(exc)) from exc


def model_provider(model: Any) -> str:
    value = getattr(model, "provider", None) or getattr(model, "model_provider", None)
    if value is None:
        module = type(model).__module__.casefold()
        if "ollama" in module:
            return "ollama"
        if "openai" in module:
            return "custom"
        return "unknown"
    provider = str(getattr(value, "value", value) or "unknown").strip().casefold()
    # LangChain names the OpenAI-compatible adapter ``openai`` even when the
    # application profile deliberately exposes that transport as ``custom``.
    return "custom" if provider == "openai" else provider


def model_name(model: Any) -> str:
    for attribute in ("model", "model_name", "name"):
        value = getattr(model, attribute, None)
        if value and not callable(value):
            return str(value)
    return "unknown"


def _observed_invoke(
    runnable: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    operation: str,
    profile: str,
    streaming: bool,
    identity_model: Any | None = None,
    **kwargs: Any,
) -> tuple[Any, TokenUsage]:
    identity = identity_model or runnable
    provider = model_provider(identity)
    model_id = model_name(identity)
    started = time.monotonic()
    status = "success"
    observation = observe.llm(
        "hdb.llm.runtime", provider=provider, model=model_id,
        stage=operation, profile=profile, streaming=streaming,
    )
    try:
        with observation:
            try:
                response = runnable.invoke(messages, **kwargs)
                usage = normalize_usage(response)
                observation.set("hdb.usage_returned", usage.usage_returned)
                if usage.usage_returned:
                    observation.tokens(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.total_tokens,
                    )
                text = extract_text(response)
                if text is not None:
                    observation.set_output(text, content_kind="llm")
                observation.outcome("success")
                return response, usage
            except Exception as exc:
                status = "error"
                wrapped = ModelInvocationError(
                    f"{operation} invocation failed for {_model_identity(identity)}: {exc}"
                )
                observation.error(wrapped)
                observation.outcome("error")
                raise wrapped from exc
    finally:
        record_llm(
            provider=provider, status=status,
            duration_s=time.monotonic() - started, streaming=streaming,
        )


def normalize_usage(response: Any) -> TokenUsage:
    candidates: list[Any] = []
    if isinstance(response, Mapping):
        candidates.extend([
            response.get("usage_metadata"), response.get("usage"),
            response.get("token_usage"), response.get("response_metadata"),
        ])
    else:
        candidates.extend([
            getattr(response, "usage_metadata", None),
            getattr(response, "usage", None),
            getattr(response, "response_metadata", None),
        ])
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        nested = candidate.get("token_usage") if isinstance(candidate.get("token_usage"), Mapping) else candidate
        input_tokens = _int_or_none(nested.get("input_tokens", nested.get("prompt_tokens")))
        output_tokens = _int_or_none(nested.get("output_tokens", nested.get("completion_tokens")))
        total_tokens = _int_or_none(nested.get("total_tokens"))
        if input_tokens is None and output_tokens is None and total_tokens is None:
            continue
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return TokenUsage(input_tokens, output_tokens, total_tokens, True)
    return TokenUsage()


def _invoke_text_fallback(
    model: ChatModelLike,
    schema: type[StructuredT],
    messages: Sequence[Mapping[str, Any]],
    *,
    operation: str,
    profile: str,
    text_fallback: Callable[[str], StructuredT | BaseModel | Mapping[str, Any]],
    **kwargs: Any,
) -> StructuredCallResult[StructuredT]:
    text_result = invoke_text(
        model, messages, operation=operation, profile=profile, **kwargs,
    )
    try:
        fallback_value = text_fallback(text_result.text)
        value = coerce_structured_response(fallback_value, schema)
    except Exception as exc:
        if isinstance(exc, StructuredOutputValidationError):
            raise
        raise StructuredOutputValidationError(
            f"{operation} text compatibility payload is invalid: {exc}"
        ) from exc
    return StructuredCallResult(
        value=value, mode="text_compat", response=text_result.response,
        usage=text_result.usage, provider=text_result.provider,
        model=text_result.model, operation=operation, profile=profile,
    )


def _is_structured_capability_error(exc: BaseException) -> bool:
    if isinstance(exc, (AttributeError, NotImplementedError, TypeError, StructuredOutputCapabilityError)):
        return True
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "structured output", "structured-output", "with_structured_output",
            "bind_tools", "tool calling", "tool-calling", "function calling",
            "unsupported tool", "does not support tools", "not support",
        )
    )


def _model_identity(model: Any) -> str:
    return f"{model_provider(model)}/{model_name(model)}"


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _empty_usage_accumulator() -> dict[str, Any]:
    return {"input": None, "output": None, "total": None, "returned": False}


def _accumulate_usage(accumulator: dict[str, Any], response: Any) -> None:
    usage = normalize_usage(response)
    if not usage.usage_returned:
        return
    accumulator["returned"] = True
    for key, value in (
        ("input", usage.input_tokens), ("output", usage.output_tokens),
        ("total", usage.total_tokens),
    ):
        if value is not None:
            accumulator[key] = int(accumulator[key] or 0) + value
