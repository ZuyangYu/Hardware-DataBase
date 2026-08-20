"""OpenTelemetry process bootstrap with fail-open exporters."""

from __future__ import annotations

import os
import logging
import socket
import threading
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

from .config import get_config


_LOCK = threading.RLock()
_INITIALIZED = False
_SHUTDOWN = False
_TRACE_PROVIDER: TracerProvider | None = None
_METER_PROVIDER: MeterProvider | None = None
_LOG_PROVIDER: LoggerProvider | None = None
_LOG_HANDLER: logging.Handler | None = None
_INSTRUMENTED_CLIENTS = False


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
            sampler=TraceIdRatioBased(config.sample_ratio),
        )
        if config.traces_enabled:
            exporter = span_exporter
            if exporter is None and config.endpoint:
                try:
                    exporter = OTLPSpanExporter(**_endpoint_args(config.endpoint))
                except Exception:
                    exporter = None
            if exporter is not None:
                tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
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
            meter_provider = MeterProvider(resource=resource, metric_readers=readers)
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
                log_handler = LoggingHandler(level=logging.NOTSET, logger_provider=log_provider)
                logging.getLogger("RAG").addHandler(log_handler)
                _LOG_PROVIDER = log_provider
                _LOG_HANDLER = log_handler
            except Exception:
                # Log export is strictly best-effort; the JSON console/file
                # handlers remain the local source of truth.
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
                logging.getLogger("RAG").removeHandler(_LOG_HANDLER)
        except Exception:
            pass
