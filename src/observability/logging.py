"""Structured logging correlation helpers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import src.settings as settings

from .context import current_span_id, current_trace_id
from .privacy import redact_text


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
        return json.dumps(payload, ensure_ascii=False, default=str)
