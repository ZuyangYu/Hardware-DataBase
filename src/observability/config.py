"""Typed view of the application observability settings."""

from __future__ import annotations

from dataclasses import dataclass

import src.settings as settings


@dataclass(frozen=True)
class ObservabilityConfig:
    enabled: bool
    service_name: str
    endpoint: str
    environment: str
    service_version: str
    sample_ratio: float
    capture_content: bool
    capture_query: bool
    capture_evidence: bool
    capture_llm_content: bool
    content_max_chars: int
    log_format: str
    metrics_enabled: bool
    traces_enabled: bool
    logs_enabled: bool
    phoenix_project: str


def get_config(*, service_name: str | None = None) -> ObservabilityConfig:
    """Read a fresh snapshot so hot-reloaded settings take effect."""

    return ObservabilityConfig(
        enabled=bool(settings.OBS_ENABLED),
        service_name=service_name or settings.OTEL_SERVICE_NAME,
        endpoint=str(settings.OTEL_EXPORTER_OTLP_ENDPOINT or ""),
        environment=str(settings.OBS_ENVIRONMENT),
        service_version=str(settings.OBS_SERVICE_VERSION),
        sample_ratio=max(0.0, min(1.0, float(settings.OBS_TRACE_SAMPLE_RATIO))),
        capture_content=bool(settings.OBS_CAPTURE_CONTENT),
        capture_query=bool(settings.OBS_CAPTURE_QUERY),
        capture_evidence=bool(settings.OBS_CAPTURE_EVIDENCE),
        capture_llm_content=bool(settings.OBS_CAPTURE_LLM_CONTENT),
        content_max_chars=max(1000, int(settings.OBS_CONTENT_MAX_CHARS)),
        log_format=str(settings.OBS_LOG_FORMAT or "json"),
        metrics_enabled=bool(settings.OBS_METRICS_ENABLED),
        traces_enabled=bool(settings.OBS_TRACES_ENABLED),
        logs_enabled=bool(settings.OBS_LOGS_ENABLED),
        phoenix_project=str(settings.OBS_PHOENIX_PROJECT or "hardware-database"),
    )
