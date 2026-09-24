"""Small, fail-open observation facade with OpenInference span semantics."""

from __future__ import annotations

import json
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .privacy import redact_text, safe_span_attributes
from .semantic import OPENINFERENCE_SPAN_KIND, SpanKind


_TRACER = trace.get_tracer("hardware_database")


class Observation:
    def __init__(self, name: str, *, kind: SpanKind | str = SpanKind.CHAIN, context=None, attributes=None):
        self.name = name
        self.kind = str(kind)
        self.context = context
        self.attributes = {
            OPENINFERENCE_SPAN_KIND: self.kind,
            **safe_span_attributes(attributes),
        }
        self.span = None
        self._scope = None
        self._ended = False
        self._error_recorded = False
        self._status_recorded = False

    def start(self) -> "Observation":
        if self.span is not None:
            return self
        try:
            self.span = _TRACER.start_span(self.name, context=self.context, attributes=self.attributes)
            self._scope = trace.use_span(self.span, end_on_exit=False)
            self._scope.__enter__()
        except Exception:
            # Telemetry must never become a business dependency.
            self.span = trace.INVALID_SPAN
            self._scope = None
        return self

    def __enter__(self) -> "Observation":
        return self.start()

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            if self.span is not None and not self._status_recorded:
                self.span.set_status(Status(StatusCode.OK))
            if self._scope is not None:
                self._scope.__exit__(None, None, None)
            if self.span is not None:
                self.span.end()
        except Exception:
            pass

    def __exit__(self, exc_type, exc_value, _traceback) -> bool:
        if exc_value is not None:
            self.error(exc_value)
        self.end()
        return False

    def set(self, key: str, value: Any) -> None:
        if self.span is None:
            self.start()
        try:
            attrs = safe_span_attributes({key: value})
            for attr_key, attr_value in attrs.items():
                self.span.set_attribute(attr_key, attr_value)
        except Exception:
            pass

    @staticmethod
    def _content_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    def set_content(self, key: str, value: Any, *, content_kind: str) -> None:
        """Set an OpenInference content attribute behind the privacy gate."""

        if self.span is None:
            self.start()
        try:
            attrs = safe_span_attributes(
                {key: self._content_value(value)},
                content_kind=content_kind,
            )
            for attr_key, attr_value in attrs.items():
                self.span.set_attribute(attr_key, attr_value)
        except Exception:
            pass

    def set_input(self, value: Any, *, content_kind: str = "query") -> None:
        self.set_content("input.value", value, content_kind=content_kind)
        self.set("input.mime_type", "text/plain")

    def set_output(self, value: Any, *, content_kind: str = "llm") -> None:
        self.set_content("output.value", value, content_kind=content_kind)
        self.set("output.mime_type", "text/plain")

    def outcome(self, status: str) -> None:
        """Record a business outcome while preserving the OTel status."""

        normalized = str(status or "unknown").lower()
        self.set("hdb.status", normalized)
        try:
            if normalized in {"success", "completed", "ok"}:
                self.span.set_status(Status(StatusCode.OK))
                self._status_recorded = True
            elif normalized in {"failed", "error", "cancelled", "canceled"}:
                self.span.set_status(Status(StatusCode.ERROR, normalized))
                self._status_recorded = True
        except Exception:
            pass

    def event(self, name: str, attrs: dict[str, Any] | None = None) -> None:
        if self.span is None:
            self.start()
        try:
            self.span.add_event(name, safe_span_attributes(attrs))
        except Exception:
            pass

    def error(self, exc: BaseException) -> None:
        if self._error_recorded:
            return
        self._error_recorded = True
        try:
            self.span.record_exception(exc)
            self.span.set_status(Status(StatusCode.ERROR, redact_text(str(exc))[:500]))
            self._status_recorded = True
            self.span.set_attribute("error.type", type(exc).__name__)
            self.span.set_attribute("error.message", redact_text(str(exc))[:1000])
        except Exception:
            pass

    def tokens(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        if input_tokens is not None:
            self.set("llm.token_count.prompt", int(input_tokens))
            self.set("gen_ai.usage.input_tokens", int(input_tokens))
        if output_tokens is not None:
            self.set("llm.token_count.completion", int(output_tokens))
            self.set("gen_ai.usage.output_tokens", int(output_tokens))
        if total_tokens is not None or input_tokens is not None or output_tokens is not None:
            total = (
                int(total_tokens)
                if total_tokens is not None
                else int(input_tokens or 0) + int(output_tokens or 0)
            )
            self.set("llm.token_count.total", total)
            self.set("gen_ai.usage.total_tokens", total)

    def set_token_usage(self, summary: Any) -> None:
        """Attach an aggregate LLM usage summary to a parent chain/agent span."""

        if summary is None:
            return

        def read(name: str, default=None):
            if isinstance(summary, dict):
                return summary.get(name, default)
            return getattr(summary, name, default)

        input_tokens = read("prompt_tokens", read("input_tokens"))
        output_tokens = read("completion_tokens", read("output_tokens"))
        total_tokens = read("total_tokens")
        if input_tokens is None and output_tokens is None and total_tokens is None:
            return
        self.tokens(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        call_count = read("call_count")
        if call_count is not None:
            self.set("hdb.llm.call_count", int(call_count))
        usage_returned_count = read("usage_returned_count")
        if usage_returned_count is not None:
            self.set("hdb.llm.usage_returned_count", int(usage_returned_count))

    def hit_count(self, count: int) -> None:
        self.set("hdb.retrieval.hit_count", int(count))
        self.set("retrieval.documents.count", int(count))

    def score(self, score: float) -> None:
        self.set("hdb.evaluation.score", float(score))


class _Observe:
    @staticmethod
    def _make(kind: SpanKind, name: str, *, context=None, **attributes: Any) -> Observation:
        return Observation(name, kind=kind, context=context, attributes=attributes)

    def agent(self, name: str, *, context=None, **attributes: Any) -> Observation:
        return self._make(SpanKind.AGENT, name, context=context, **attributes)

    def chain(self, name: str, *, context=None, **attributes: Any) -> Observation:
        return self._make(SpanKind.CHAIN, name, context=context, **attributes)

    def evaluator(self, name: str, *, context=None, **attributes: Any) -> Observation:
        return self._make(SpanKind.EVALUATOR, name, context=context, **attributes)

    def llm(self, name: str, *, context=None, **attributes: Any) -> Observation:
        if attributes.get("model") and "llm.model_name" not in attributes:
            attributes["llm.model_name"] = attributes["model"]
        if attributes.get("provider") and "llm.provider" not in attributes:
            attributes["llm.provider"] = attributes["provider"]
        return self._make(SpanKind.LLM, name, context=context, **attributes)

    def retriever(self, name: str, *, context=None, **attributes: Any) -> Observation:
        return self._make(SpanKind.RETRIEVER, name, context=context, **attributes)

    def reranker(self, name: str, *, context=None, **attributes: Any) -> Observation:
        return self._make(SpanKind.RERANKER, name, context=context, **attributes)

    def tool(self, name: str, *, context=None, **attributes: Any) -> Observation:
        return self._make(SpanKind.TOOL, name, context=context, **attributes)

    def prompt(self, name: str, *, context=None, **attributes: Any) -> Observation:
        return self._make(SpanKind.PROMPT, name, context=context, **attributes)


observe = _Observe()
