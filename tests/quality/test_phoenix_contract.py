"""Opt-in integration against an isolated Phoenix 19.13.0 instance."""
from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from src.quality.phoenix_sync import PhoenixAnnotationClient, PhoenixRetry

ENDPOINT = os.getenv("HDB_TEST_PHOENIX_ENDPOINT", "")


@pytest.mark.skipif(not ENDPOINT, reason="HDB_TEST_PHOENIX_ENDPOINT is not configured")
def test_late_span_idempotent_identifiers_and_revision_history():
    project = f"hdb-quality-contract-{uuid.uuid4().hex[:12]}"
    provider = TracerProvider(resource=Resource.create({"openinference.project.name": project}))
    tracer = provider.get_tracer("quality-contract")
    span = tracer.start_span("fixed-answer", attributes={"openinference.span.kind": "CHAIN"})
    span_id = f"{span.get_span_context().span_id:016x}"
    client = PhoenixAnnotationClient(endpoint=ENDPOINT, project=project)
    payload = {"identifier": f"feedback:{uuid.uuid4().hex}", "name": "user_feedback", "annotator_kind": "HUMAN",
               "label": "unhelpful", "score": 0, "metadata": {"case_id": "contract-case"}}
    with pytest.raises(PhoenixRetry, match="404"):
        client.sync(span_id, payload)
    span.end()
    exporter = OTLPSpanExporter(endpoint=f"{ENDPOINT}/v1/traces", timeout=10)
    from opentelemetry.sdk.trace.export import SpanExportResult

    assert exporter.export([span]) == SpanExportResult.SUCCESS
    deadline = time.monotonic() + 20
    while True:
        try:
            client.sync(span_id, payload)
            break
        except PhoenixRetry:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    client.sync(span_id, payload)
    with httpx.Client(base_url=ENDPOINT, timeout=10) as http:
        rows = http.get(f"/v1/projects/{project}/span_annotations", params={"span_ids": span_id, "limit": 100}).json()["data"]
        assert len(rows) == 1
        first_id = rows[0]["id"]
        client.sync(span_id, {**payload, "identifier": f"feedback:{uuid.uuid4().hex}", "label": "withdrawn", "score": None})
        rows = http.get(f"/v1/projects/{project}/span_annotations", params={"span_ids": span_id, "limit": 100}).json()["data"]
        assert len(rows) == 2
        assert next(row for row in rows if row["identifier"] == payload["identifier"])["id"] == first_id
        assert next(row for row in rows if row["result"]["label"] == "withdrawn")["result"]["score"] is None
    exporter.shutdown()
    provider.shutdown()
