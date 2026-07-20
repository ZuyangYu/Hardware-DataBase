# src/core/logger.py
import contextvars
import logging
import os
from datetime import datetime

import config.settings

os.makedirs(config.settings.LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(config.settings.LOG_DIR, f"rag_{datetime.now().strftime('%Y-%m-%d')}.log")

logger = logging.getLogger("RAG")
logger.setLevel(getattr(logging, getattr(config.settings, "LOG_LEVEL", "INFO").upper(), logging.INFO))
logger.propagate = False
formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(trace_id)s%(message)s")


# ---- per-query 关联上下文 ----
# trace_id 把一次查询的日志行串起来，并与 query_traces.metadata_json 同值，使 DB 行 ↔ 日志行可 join。
# query_error 让后端在 except 里把真实错误透传给 streamlit 记录层，避免靠 fallback 字符串前缀误判 status。
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
_query_error_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("query_error", default=None)


class TraceIdFilter(logging.Filter):
    """给每条日志注入 [trace_id] 前缀；未设置时不加前缀，启动期日志不受影响。"""

    def filter(self, record):
        tid = _trace_id_var.get("")
        record.trace_id = f"[{tid}] " if tid else ""
        return True


def set_trace_id(trace_id: str) -> None:
    _trace_id_var.set(str(trace_id or ""))


def get_trace_id() -> str:
    return _trace_id_var.get("")


def clear_trace_id() -> None:
    _trace_id_var.set("")


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
    level = getattr(logging, getattr(config.settings, "LOG_LEVEL", "INFO").upper(), logging.INFO)
    logger.setLevel(level)


# 清除已有的处理器
logger.handlers.clear()

# 控制台处理器
ch = logging.StreamHandler()
ch.setFormatter(formatter)
ch.addFilter(TraceIdFilter())
logger.addHandler(ch)

# 文件处理器
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setFormatter(formatter)
fh.addFilter(TraceIdFilter())
logger.addHandler(fh)

# 导出函数
log = logger.info
warn = logger.warning
error = logger.error
debug = logger.debug
