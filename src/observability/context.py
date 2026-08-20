"""W3C trace-context propagation across threads and durable queues."""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Iterator

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import format_span_id, format_trace_id


@dataclass(frozen=True)
class TraceIdentity:
    trace_id: str = ""
    span_id: str = ""


def inject_trace_context(carrier: dict[str, str] | None = None) -> dict[str, str]:
    """Inject the current span into a JSON-safe carrier for SQLite/HTTP."""

    target = carrier if carrier is not None else {}
    try:
        propagate.inject(target)
    except Exception:
        return target
    return {str(key): str(value) for key, value in target.items() if value}


def extract_trace_context(carrier: dict[str, str] | None):
    """Extract a W3C parent context; invalid/missing carriers become no-op."""

    try:
        return propagate.extract(carrier or {})
    except Exception:
        return otel_context.get_current()


def _identity_for_span(span) -> TraceIdentity:
    context = span.get_span_context() if span is not None else None
    if context is None or not context.is_valid:
        return TraceIdentity()
    return TraceIdentity(
        trace_id=format_trace_id(context.trace_id),
        span_id=format_span_id(context.span_id),
    )


def trace_identity_from_context(context) -> TraceIdentity:
    try:
        return _identity_for_span(trace.get_current_span(context))
    except Exception:
        return TraceIdentity()


def current_trace_identity() -> TraceIdentity:
    return _identity_for_span(trace.get_current_span())


def current_trace_id() -> str:
    return current_trace_identity().trace_id


def current_span_id() -> str:
    return current_trace_identity().span_id


@contextmanager
def use_trace_context(context) -> Iterator[None]:
    token = otel_context.attach(context)
    try:
        yield
    finally:
        otel_context.detach(token)


def run_with_context(fn: Callable[..., Any], context, *args: Any, **kwargs: Any) -> Any:
    with use_trace_context(context):
        return fn(*args, **kwargs)


def start_thread_with_current_context(
    target: Callable[..., Any],
    *args: Any,
    daemon: bool | None = None,
    name: str | None = None,
    **kwargs: Any,
) -> threading.Thread:
    """Start a thread with both Python and OTel ContextVars copied."""

    thread = thread_with_current_context(target, *args, daemon=daemon, name=name, **kwargs)
    thread.start()
    return thread


def thread_with_current_context(
    target: Callable[..., Any],
    *args: Any,
    daemon: bool | None = None,
    name: str | None = None,
    **kwargs: Any,
) -> threading.Thread:
    """Build, but do not start, a thread carrying the current context."""

    python_context = contextvars.copy_context()
    runner = partial(target, *args, **kwargs)
    return threading.Thread(
        target=lambda: python_context.run(runner),
        daemon=daemon,
        name=name,
    )


def submit_with_current_context(executor, fn: Callable[..., Any], *args: Any, **kwargs: Any):
    """Submit work to an executor without losing the parent trace/span."""

    python_context = contextvars.copy_context()
    runner = partial(fn, *args, **kwargs)
    return executor.submit(lambda: python_context.run(runner))
