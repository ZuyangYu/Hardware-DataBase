"""Credentials must not escape through any telemetry export path."""

import json
import logging

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

import src.settings as settings
from src.observability import tracing
from src.observability.bootstrap import RedactingSpanExporter
from src.observability.logging import RedactingLoggingHandler
from src.observability.privacy import redact_text, safe_span_attributes

SECRET = "FAKE_CREDENTIAL_MUST_NOT_ESCAPE"


@pytest.mark.parametrize("text", [
    f"Authorization: Bearer {SECRET}",
    f"Authorization: Basic {SECRET}",
    f'{{"nested": {{"api_key": "{SECRET}"}}}}',
    f"AGENT_CUSTOM_API_KEY='{SECRET}'",
    f"https://example.invalid/?access_token={SECRET}&page=1",
    f"postgresql://user:{SECRET}@localhost/database",
    f"Cookie: session={SECRET}; csrf={SECRET}",
])
def test_redacts_credential_forms(text):
    assert SECRET not in redact_text(text)


def test_content_opt_in_never_opts_credentials_in(monkeypatch):
    monkeypatch.setattr(settings, "OBS_CAPTURE_CONTENT", True)
    attrs = safe_span_attributes({
        "AGENT_CUSTOM_API_KEY": SECRET,
        "http.set-cookie": SECRET,
        "input.value": f"question api_key={SECRET}",
        "nested": {"access_token": SECRET, "useful": "diagnostic"},
    }, content_kind="query")
    assert SECRET not in json.dumps(attrs)
    assert "diagnostic" in str(attrs)


def test_otlp_log_body_extras_and_exception_are_redacted():
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = RedactingLoggingHandler(logger_provider=provider)
    try:
        try:
            raise ValueError(f"Authorization: Bearer {SECRET}")
        except ValueError:
            import sys
            record = logging.LogRecord("privacy", logging.ERROR, __file__, 1,
                                       "api_key=%s", (SECRET,), sys.exc_info())
        record.details = {"password": SECRET, "stage": "retrieval"}
        handler.handle(record)
        log_record = exporter.get_finished_logs()[0].log_record
        assert SECRET not in str(log_record.body)
        assert SECRET not in str(log_record.attributes)
        assert "ValueError" in str(log_record.attributes)
        # The input record is still usable by other handlers.
        assert SECRET in record.getMessage()
    finally:
        handler.close()
        provider.shutdown()


@pytest.mark.parametrize("automatic", [False, True])
def test_exception_event_and_attributes_are_redacted(monkeypatch, automatic):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(exporter)))
    tracer = provider.get_tracer("privacy-test")
    monkeypatch.setattr(tracing, "_TRACER", tracer)
    try:
        if automatic:
            with tracer.start_as_current_span("automatic") as span:
                span.record_exception(ValueError(f"api_key={SECRET}"))
                span.set_attribute("http.url", f"https://example.invalid/?token={SECRET}")
                span.set_status(Status(StatusCode.ERROR, f"api_key={SECRET}"))
        else:
            with tracing.observe.chain("application") as observation:
                observation.error(ValueError(f"api_key={SECRET}"))
        span = exporter.get_finished_spans()[0]
        assert SECRET not in str(span.attributes)
        assert SECRET not in str(span.status.description)
        assert SECRET not in str([dict(event.attributes) for event in span.events])
        assert any(event.name == "exception" for event in span.events)
    finally:
        provider.shutdown()


@pytest.mark.parametrize("kind,flag", [
    ("query", "OBS_CAPTURE_QUERY"), ("llm", "OBS_CAPTURE_LLM_CONTENT"), ("evidence", "OBS_CAPTURE_EVIDENCE"),
])
def test_export_retains_only_opted_in_content(monkeypatch, kind, flag):
    for key in ("OBS_CAPTURE_CONTENT", "OBS_CAPTURE_QUERY", "OBS_CAPTURE_LLM_CONTENT", "OBS_CAPTURE_EVIDENCE"):
        monkeypatch.setattr(settings, key, key == flag)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(exporter)))
    monkeypatch.setattr(tracing, "_TRACER", provider.get_tracer("capture-test"))
    try:
        with tracing.observe.chain("allowed") as observation:
            observation.set_input(f"diagnostic api_key={SECRET}", content_kind=kind)
        with tracing.observe.chain("blocked") as observation:
            observation.set_input("private", content_kind="llm" if kind != "llm" else "query")
        allowed, blocked = exporter.get_finished_spans()
        assert "diagnostic" in allowed.attributes["input.value"]
        assert SECRET not in allowed.attributes["input.value"]
        assert "input.value" not in blocked.attributes
    finally:
        provider.shutdown()
