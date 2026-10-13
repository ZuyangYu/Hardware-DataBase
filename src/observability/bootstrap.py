"""OpenTelemetry process bootstrap with fail-open exporters."""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import copy
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Link, Status

from .config import get_config
from .logging import RedactingLoggingHandler
from .privacy import safe_span_attributes, redact_text


def _export_attributes(attrs):
    values = dict(attrs or {})
    safe = safe_span_attributes(values)
    for key, default_kind in (("input.value", "query"), ("output.value", "llm")):
        if key in values:
            # Application content is tagged at capture time; automatic LLM
            # spans use the stricter LLM gate for both input and output.
            kind = values.get(f"hdb.content.{key}.kind") or (
                "llm" if values.get("openinference.span.kind") == "LLM" else default_kind
            )
            safe.update(safe_span_attributes({key: values[key]}, content_kind=kind))
    return safe


class RedactingSpanExporter(SpanExporter):
    """Apply the policy to auto-instrumentation as well as application spans."""

    def __init__(self, exporter: SpanExporter) -> None:
        self.exporter = exporter

    def export(self, spans):
        sanitized = []
        for span in spans:
            safe_span = copy.copy(span)
            safe_span._attributes = _export_attributes(span.attributes)
            safe_span._status = Status(span.status.status_code, redact_text(span.status.description) or None)
            safe_span._links = tuple(Link(link.context, _export_attributes(link.attributes)) for link in span.links)
            safe_span._events = tuple(
                Event(redact_text(event.name), _export_attributes(event.attributes), event.timestamp)
                for event in span.events
            )
            safe_span._name = redact_text(span.name)
            sanitized.append(safe_span)
        return self.exporter.export(sanitized)

    def shutdown(self) -> None:
        self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.exporter.force_flush(timeout_millis)


_LOCK = threading.RLock()
_INITIALIZED = False
_SHUTDOWN = False
_TRACE_PROVIDER: TracerProvider | None = None
_METER_PROVIDER: MeterProvider | None = None
_LOG_PROVIDER: LoggerProvider | None = None
_LOG_HANDLER: logging.Handler | None = None
_INSTRUMENTED_CLIENTS = False

# Seconds, rather than the SDK's default millisecond-like boundaries.
LATENCY_BUCKETS_SECONDS = (
    0, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 20, 30, 60, 120, 300, 600,
)


def latency_views() -> list[View]:
    return [View(
        instrument_type=Histogram,
        instrument_unit="s",
        aggregation=ExplicitBucketHistogramAggregation(boundaries=LATENCY_BUCKETS_SECONDS),
    )]


# 需要接入 OTel 日志导出的 logger：
#   "RAG"            业务日志（src/core/logger.py，propagate=False）；
#   "uvicorn" 等     API 进程由 uvicorn 写出的启动/错误/访问日志。
# 不挂 root：避免会向上传播的 logger 把同一条记录导出两次。
_LOG_HANDLER_LOGGERS: tuple[str, ...] = (
    "RAG",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)


def _attach_log_handler(handler: logging.Handler) -> None:
    """把 OTel 日志 handler 挂到所有业务 logger 上（幂等，可重复调用）。

    uvicorn 的 ``dictConfig`` 会**重建它配置的 logger 的 handler 列表**，而
    ``create_app()`` 在模块导入时就已初始化观测（早于 ``uvicorn.run`` 的日志配置），
    所以 API 进程必须在 lifespan 启动后再挂一次，否则 uvicorn 的访问/错误日志
    到不了 Loki。
    """
    for logger_name in _LOG_HANDLER_LOGGERS:
        target = logging.getLogger(logger_name)
        if handler in target.handlers:
            continue
        target.addHandler(handler)
        # uvicorn 可能先把 logger disable 掉后再由 dictConfig 重建
        target.disabled = False
        # uvicorn.access/uvicorn.error 是 uvicorn 的子 logger：同一条记录会先经自己
        # 的 handler，再向上传播给父 logger 上同一个 handler，造成重复导出。
        target.propagate = False


def attach_log_handler() -> bool:
    """（重新）挂载已初始化的日志 handler；未初始化日志导出时返回 False。"""
    with _LOCK:
        if _LOG_HANDLER is None:
            return False
        _attach_log_handler(_LOG_HANDLER)
        return True


def _endpoint_args(endpoint: str) -> dict[str, Any]:
    # The gRPC exporters accept the full http(s) URL.  ``insecure`` is needed
    # for local Collector deployments and ignored by TLS endpoints.
    return {"endpoint": endpoint, "insecure": endpoint.startswith("http://")}


def init_observability(
    service_name: str,
    *,
    service_version: str,
    environment: str,
    span_exporter: SpanExporter | None = None,
    metric_exporter: Any | None = None,
) -> None:
    """Initialise one process-wide OTel provider; all exporter failures are async."""

    global _INITIALIZED, _SHUTDOWN, _TRACE_PROVIDER, _METER_PROVIDER, _LOG_PROVIDER, _LOG_HANDLER, _INSTRUMENTED_CLIENTS
    with _LOCK:
        if _INITIALIZED:
            return
        config = get_config(service_name=service_name)
        if not config.enabled:
            _INITIALIZED = True
            return

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": service_version,
                "deployment.environment.name": environment,
                # Phoenix groups incoming spans by this OpenInference resource
                # attribute.  Keeping it on the application resource makes
                # the project stable even when traces pass through a
                # Collector and no SDK-specific Phoenix helper is used.
                "openinference.project.name": config.phoenix_project,
                "service.instance.id": f"{socket.gethostname()}-{os.getpid()}",
                "host.name": socket.gethostname(),
                "process.pid": os.getpid(),
            }
        )
        tracer_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(config.sample_ratio)),
        )
        if config.traces_enabled:
            exporter = span_exporter
            if exporter is None and config.endpoint:
                try:
                    exporter = OTLPSpanExporter(**_endpoint_args(config.endpoint))
                except Exception:
                    exporter = None
            if exporter is not None:
                tracer_provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(exporter)))
        try:
            trace.set_tracer_provider(tracer_provider)
        except Exception:
            # A test runner or an embedding host may already own the global
            # provider.  The API remains usable with that provider.
            pass
        _TRACE_PROVIDER = tracer_provider

        if config.metrics_enabled:
            exporter = metric_exporter
            if exporter is None and config.endpoint:
                try:
                    exporter = OTLPMetricExporter(**_endpoint_args(config.endpoint))
                except Exception:
                    exporter = None
            readers = [PeriodicExportingMetricReader(exporter)] if exporter is not None else []
            meter_provider = MeterProvider(resource=resource, metric_readers=readers, views=latency_views())
            try:
                metrics.set_meter_provider(meter_provider)
            except Exception:
                pass
            _METER_PROVIDER = meter_provider

        if config.logs_enabled and config.endpoint:
            try:
                log_exporter = OTLPLogExporter(**_endpoint_args(config.endpoint))
                log_provider = LoggerProvider(resource=resource)
                log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
                set_logger_provider(log_provider)
                log_handler = RedactingLoggingHandler(level=logging.NOTSET, logger_provider=log_provider)
                # 只挂 "RAG" 会让 API 进程的日志全丢：uvicorn 自己的 logger 拿不到
                # handler，而应用日志与 worker 共用的 "RAG" logger 又是 propagate=False。
                # 因此把 handler 挂到所有产生业务日志的 logger 上（不挂 root，避免
                # 会向上传播的 logger 重复导出同一条记录）。
                _attach_log_handler(log_handler)
                _LOG_PROVIDER = log_provider
                _LOG_HANDLER = log_handler
                if os.getenv("HDB_OBS_DEBUG"):
                    print(
                        "[observability] DEBUG log handler attached: "
                        + str(
                            [
                                (name, [type(h).__name__ for h in logging.getLogger(name).handlers])
                                for name in _LOG_HANDLER_LOGGERS
                            ]
                        ),
                        file=sys.stderr,
                    )
            except Exception as exc:  # noqa: BLE001 - 见下方说明：best-effort
                # 静默吞掉会让"日志没进 Loki"无从排查，这里必须留痕。
                print(
                    f"[observability] 日志导出初始化失败: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                _LOG_PROVIDER = None
                _LOG_HANDLER = None

        if not _INSTRUMENTED_CLIENTS:
            for instrumentor in (
                HTTPXClientInstrumentor(),
                RequestsInstrumentor(),
            ):
                try:
                    instrumentor.instrument()
                except Exception:
                    pass
            _INSTRUMENTED_CLIENTS = True
        _INITIALIZED = True
        _SHUTDOWN = False


def instrument_fastapi(app) -> None:
    """Instrument one FastAPI app while keeping repeated app factories safe."""

    if getattr(app.state, "hdb_fastapi_instrumented", False):
        return
    if not get_config().enabled:
        app.state.hdb_fastapi_instrumented = False
        return
    try:
        FastAPIInstrumentor.instrument_app(app)
        app.state.hdb_fastapi_instrumented = True
    except Exception:
        # FastAPI instrumentation is useful but never a reason to reject app
        # startup (notably when tests construct several app instances).
        app.state.hdb_fastapi_instrumented = False


def shutdown_observability() -> None:
    """Flush exporters best-effort; business code never waits on backends."""

    global _SHUTDOWN
    with _LOCK:
        if not _INITIALIZED or _SHUTDOWN:
            return
        _SHUTDOWN = True
        try:
            if _TRACE_PROVIDER is not None:
                _TRACE_PROVIDER.force_flush(timeout_millis=2000)
        except Exception:
            pass
        try:
            if _METER_PROVIDER is not None:
                _METER_PROVIDER.force_flush(timeout_millis=2000)
        except Exception:
            pass
        try:
            if _LOG_PROVIDER is not None:
                _LOG_PROVIDER.force_flush(timeout_millis=2000)
            if _LOG_HANDLER is not None:
                for logger_name in _LOG_HANDLER_LOGGERS:
                    logging.getLogger(logger_name).removeHandler(_LOG_HANDLER)
        except Exception:
            pass
