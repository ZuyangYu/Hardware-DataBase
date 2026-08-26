# src/core/logger.py
import logging
import os
import contextvars
from datetime import datetime

import src.settings
from src.observability.context import current_span_id, current_trace_id
from src.observability.logging import StructuredJsonFormatter, TraceContextFilter

os.makedirs(src.settings.LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(src.settings.LOG_DIR, f"rag_{datetime.now().strftime('%Y-%m-%d')}.log")

logger = logging.getLogger("RAG")
logger.setLevel(getattr(logging, getattr(src.settings, "LOG_LEVEL", "INFO").upper(), logging.INFO))
logger.propagate = False
formatter = StructuredJsonFormatter()


# query_error 让后端在 except 里把真实错误透传给记录层，避免靠 fallback
# 字符串前缀误判 status。Trace ID 本身由 OTel 当前 Span 提供。
_query_error_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("query_error", default=None)


class TraceIdFilter(logging.Filter):
    """Compatibility alias; the canonical implementation reads OTel context."""

    def filter(self, record):
        record.trace_id = current_trace_id()
        record.span_id = current_span_id()
        record.service = src.settings.OTEL_SERVICE_NAME
        return True


def set_trace_id(trace_id: str) -> None:
    """Deprecated compatibility shim; OTel remains the source of truth."""
    return None


def get_trace_id() -> str:
    return current_trace_id()


def clear_trace_id() -> None:
    return None


def set_query_error(message: str) -> None:
    """后端 except 里调用，把真实错误透传给记录层（替代脆弱的字符串前缀匹配）。"""
    if _query_error_var.get() is None:
        _query_error_var.set(str(message or ""))


def get_query_error() -> str | None:
    return _query_error_var.get()


def clear_query_error() -> None:
    _query_error_var.set(None)


def apply_log_level() -> None:
    """reload_settings 后热更新日志级别。"""
    level = getattr(logging, getattr(src.settings, "LOG_LEVEL", "INFO").upper(), logging.INFO)
    logger.setLevel(level)


# 清除已有的处理器
logger.handlers.clear()

# 控制台处理器
ch = logging.StreamHandler()
ch.setFormatter(formatter)
ch.addFilter(TraceContextFilter())
logger.addHandler(ch)

# 文件处理器
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setFormatter(formatter)
fh.addFilter(TraceContextFilter())
logger.addHandler(fh)

# 导出函数
log = logger.info
warn = logger.warning
error = logger.error
debug = logger.debug
