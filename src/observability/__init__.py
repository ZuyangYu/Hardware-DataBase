"""Fail-open observability facade for Hardware-DataBase.

Business modules should import the small ``observe`` facade and the context
helpers from this package instead of depending directly on an observability
backend.  The package is intentionally safe to import before bootstrap: the
OpenTelemetry API returns no-op providers until a process opts in.
"""

from .bootstrap import init_observability, instrument_fastapi, shutdown_observability
from .context import (
    current_span_id,
    current_trace_id,
    current_trace_identity,
    extract_trace_context,
    inject_trace_context,
    run_with_context,
    start_thread_with_current_context,
    submit_with_current_context,
    thread_with_current_context,
)
from .tracing import observe

__all__ = [
    "current_span_id",
    "current_trace_id",
    "current_trace_identity",
    "extract_trace_context",
    "init_observability",
    "inject_trace_context",
    "instrument_fastapi",
    "observe",
    "run_with_context",
    "shutdown_observability",
    "start_thread_with_current_context",
    "submit_with_current_context",
    "thread_with_current_context",
]
