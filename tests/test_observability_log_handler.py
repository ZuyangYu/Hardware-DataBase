"""观测日志导出：OTel handler 必须挂到所有产生业务日志的 logger 上，且每条只导出一次。

回归背景：handler 原先只挂在 ``"RAG"`` 上，API 进程里 uvicorn 自己的
``uvicorn``/``uvicorn.error``/``uvicorn.access`` logger 拿不到 handler，导致
Grafana/Loki 里查不到 API 的任何日志。改成多 logger 挂载后，还要防止
``uvicorn.error``/``uvicorn.access`` 向父 logger 传播时被同一个 handler 重复导出。
"""

from __future__ import annotations

import logging
import unittest
from collections import Counter
from types import SimpleNamespace

import src.observability.bootstrap as bootstrap
from src.core.logger import logger as rag_logger
from src.observability.bootstrap import init_observability, shutdown_observability

_LOGGERS = ("RAG", "uvicorn", "uvicorn.error", "uvicorn.access")


class _FakeConfig(SimpleNamespace):
    pass


class ObservabilityLogHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        # 其它测试可能已用真实配置初始化过观测（_INITIALIZED=True），这里先关停并复位，
        # 保证本测试拿到的是假 handler。
        if bootstrap._INITIALIZED:
            shutdown_observability()
        self.saved_globals = (
            bootstrap._INITIALIZED,
            bootstrap._SHUTDOWN,
            bootstrap._LOG_HANDLER,
            bootstrap._LOG_PROVIDER,
        )
        bootstrap._INITIALIZED = False
        bootstrap._SHUTDOWN = False
        bootstrap._LOG_HANDLER = None
        bootstrap._LOG_PROVIDER = None
        self.emitted: list[str] = []
        self.saved = {
            name: (
                list(logging.getLogger(name).handlers),
                logging.getLogger(name).disabled,
                logging.getLogger(name).propagate,
            )
            for name in _LOGGERS
        }

        def _emit(record: logging.LogRecord) -> None:
            self.emitted.append(record.getMessage())

        class _Handler(logging.Handler):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__()

            def emit(self, record: logging.LogRecord) -> None:
                _emit(record)

        self._handler_cls = _Handler
        self._original = bootstrap.LoggingHandler
        bootstrap.LoggingHandler = _Handler  # type: ignore[assignment]
        self._original_get_config = bootstrap.get_config
        bootstrap.get_config = lambda *a, **k: _FakeConfig(  # type: ignore[assignment]
            enabled=True,
            traces_enabled=False,
            metrics_enabled=False,
            logs_enabled=True,
            endpoint="http://127.0.0.1:1",
            sample_ratio=1.0,
            phoenix_project="hardware-database",
        )

    def tearDown(self) -> None:
        shutdown_observability()
        bootstrap.LoggingHandler = self._original  # type: ignore[assignment]
        bootstrap.get_config = self._original_get_config  # type: ignore[assignment]
        (
            bootstrap._INITIALIZED,
            bootstrap._SHUTDOWN,
            bootstrap._LOG_HANDLER,
            bootstrap._LOG_PROVIDER,
        ) = self.saved_globals
        for name, (handlers, disabled, propagate) in self.saved.items():
            target = logging.getLogger(name)
            for handler in list(target.handlers):
                if isinstance(handler, self._handler_cls):
                    target.removeHandler(handler)
            target.disabled = disabled
            target.propagate = propagate
            target.handlers = handlers

    def test_handler_reaches_every_business_logger_exactly_once(self) -> None:
        init_observability("probe", service_version="0", environment="test")

        for name in _LOGGERS:
            self.assertTrue(
                any(isinstance(h, self._handler_cls) for h in logging.getLogger(name).handlers),
                f"{name} 未挂上 OTel 日志 handler",
            )

        rag_logger.info("probe-business")
        logging.getLogger("uvicorn").error("probe-uvicorn")
        logging.getLogger("uvicorn.error").error("probe-uvicorn-error")
        logging.getLogger("uvicorn.access").error("probe-uvicorn-access")

        counts = Counter(self.emitted)
        expected = {
            "probe-business": 1,
            "probe-uvicorn": 1,
            "probe-uvicorn-error": 1,
            "probe-uvicorn-access": 1,
        }
        self.assertEqual(dict(counts), expected, "每条日志必须恰好导出一次（无缺失、无重复）")

    def test_shutdown_removes_handler_from_every_logger(self) -> None:
        init_observability("probe", service_version="0", environment="test")
        shutdown_observability()
        for name in _LOGGERS:
            self.assertFalse(
                any(isinstance(h, self._handler_cls) for h in logging.getLogger(name).handlers),
                f"shutdown 后 {name} 仍残留 handler",
            )

    def test_handler_survives_uvicorn_dictconfig_reconfiguration(self) -> None:
        """API 进程的真实顺序：导入期挂 handler → uvicorn dictConfig 重建 logger → 需重挂。"""
        init_observability("probe", service_version="0", environment="test")
        attached = any(
            isinstance(h, self._handler_cls) for h in logging.getLogger("uvicorn.access").handlers
        )
        self.assertTrue(attached, "导入期应当挂上 handler")

        # 模拟 uvicorn.configure_logging()：把它配置的 logger 的 handler 全部替换掉
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).handlers = [logging.StreamHandler()]

        from src.observability import attach_log_handler

        self.assertTrue(attach_log_handler(), "已初始化时必须能重新挂载")
        for name in _LOGGERS:
            self.assertTrue(
                any(isinstance(h, self._handler_cls) for h in logging.getLogger(name).handlers),
                f"重挂后 {name} 仍缺 OTel handler（API 日志会丢）",
            )
        # 幂等：重复挂不会叠加多个 handler
        attach_log_handler()
        for name in _LOGGERS:
            self.assertEqual(
                sum(1 for h in logging.getLogger(name).handlers if isinstance(h, self._handler_cls)),
                1,
                f"{name} 上 OTel handler 重复",
            )

        rag_logger.info("probe-after-reconfig")
        logging.getLogger("uvicorn.access").error("probe-access-after-reconfig")
        counts = Counter(self.emitted)
        self.assertEqual(counts["probe-after-reconfig"], 1)
        self.assertEqual(counts["probe-access-after-reconfig"], 1)


if __name__ == "__main__":
    unittest.main()
