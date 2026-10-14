"""Structured logging correlation helpers."""

from __future__ import annotations

import json
import logging
import copy
from datetime import datetime, timezone

import src.settings as settings

from .context import current_span_id, current_trace_id
from opentelemetry.sdk._logs import LoggingHandler

from .privacy import exception_attributes, is_secret_key, redact_text, redact_value


class RedactingLoggingHandler(LoggingHandler):
    """Sanitize a copy before OTel translation, including structured extras."""

    def emit(self, record: logging.LogRecord) -> None:
        safe_record = copy.copy(record)
        safe_record.msg = redact_text(record.getMessage())
        safe_record.args = ()
        safe_record.exc_info = None
        safe_record.exc_text = None
        safe_record.stack_info = redact_text(record.stack_info) if record.stack_info else None
        for key, value in list(safe_record.__dict__.items()):
            if is_secret_key(key):
                del safe_record.__dict__[key]
            elif key not in {"msg", "args", "exc_info", "exc_text"}:
                safe_record.__dict__[key] = redact_value(value)
        if record.exc_info and record.exc_info[1] is not None:
            safe_record.__dict__.update(exception_attributes(record.exc_info[1]))
        super().emit(safe_record)


class TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id()
        record.span_id = current_span_id()
        record.service = settings.OTEL_SERVICE_NAME
        return True


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", settings.OTEL_SERVICE_NAME),
            "trace_id": getattr(record, "trace_id", ""),
            "span_id": getattr(record, "span_id", ""),
            "event": getattr(record, "event", ""),
            "message": redact_text(record.getMessage()),
        }
        for key in ("retriever", "duration_ms", "hit_count", "status", "worker_id", "run_id"):
            if hasattr(record, key):
                payload[key] = redact_text(getattr(record, key)) if isinstance(getattr(record, key), str) else getattr(record, key)
        return json.dumps(redact_value(payload), ensure_ascii=False, default=str)
