"""Regression contracts for configuration, health and actual SDK metric buckets."""

import json
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import src.settings as settings
from src.observability import health
from src.observability.bootstrap import LATENCY_BUCKETS_SECONDS, latency_views


def test_reload_preserves_deployment_wiring_but_applies_hot_settings(tmp_path):
    path = tmp_path / "config.env"
    path.write_text("RESOURCE_GOVERNOR_SCOPE=process\nOTEL_EXPORTER_OTLP_ENDPOINT=\nAGENT_TEMPERATURE=0.2\n")
    code = '''
import json
import src.settings as s
import os
before = s.CONFIG_REVISION
os.environ["MCP_CREDENTIALS_KEY"] = "FAKE_ROTATED_KEY"
with open(s.ENV_FILE_PATH, "a") as f:
    f.write("AGENT_TEMPERATURE=0.6\\nAGENT_CHECKPOINT_BACKEND=postgres\\nOBS_ENABLED=false\\n")
s.reload_settings()
print(json.dumps({"scope": s.RESOURCE_GOVERNOR_SCOPE, "endpoint": s.OTEL_EXPORTER_OTLP_ENDPOINT,
                  "temperature": s.AGENT_TEMPERATURE, "backend": s.AGENT_CHECKPOINT_BACKEND,
                  "observed": s.OBS_ENABLED, "credential_key": s.MCP_CREDENTIALS_KEY,
                  "revision_changed": s.CONFIG_REVISION != before}))
'''
    env = dict(os.environ, HDB_ENV_PATH=str(path), RESOURCE_GOVERNOR_SCOPE="deployment",
               OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4317", OBS_ENABLED="true",
               AGENT_CHECKPOINT_BACKEND="sqlite", MCP_CREDENTIALS_KEY="FAKE_STARTUP_KEY")
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data == {"scope": "deployment", "endpoint": "http://otel-collector:4317", "temperature": 0.6,
                    "backend": "sqlite", "observed": True, "credential_key": "FAKE_STARTUP_KEY",
                    "revision_changed": True}


@pytest.mark.parametrize("status,expected", [(200, "up"), (204, "up"), (301, "down"),
                                           (401, "down"), (403, "down"), (404, "down"),
                                           (429, "down"), (500, "down")])
def test_http_probe_requires_success(monkeypatch, status, expected):
    get = Mock(return_value=Mock(status_code=status))
    monkeypatch.setattr(health.requests, "get", get)
    assert health._probe_http("llm", "https://example.invalid/models")["status"] == expected
    assert get.call_args.kwargs["allow_redirects"] is False


def test_custom_model_probe_sends_authentication(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_LLM_PROVIDER", settings.Provider.CUSTOM)
    monkeypatch.setattr(settings, "AGENT_CUSTOM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(settings, "AGENT_CUSTOM_API_KEY", "FAKE_TEST_TOKEN")
    probe = Mock(return_value={"status": "up"})
    monkeypatch.setattr(health, "_probe_http", probe)
    health.check_dependencies()
    llm_call = next(call for call in probe.call_args_list if call.args[0] == "llm")
    assert llm_call.args[1] == "https://example.invalid/v1/models"
    assert llm_call.kwargs["headers"] == {"Authorization": "Bearer FAKE_TEST_TOKEN"}


def test_seconds_histogram_exports_subsecond_and_long_tail_buckets():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], views=latency_views())
    try:
        instrument = provider.get_meter("test").create_histogram("latency", unit="s")
        for value in (0.02, 0.2, 1.5, 12, 90):
            instrument.record(value)
        data = reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics[0].data.data_points[0]
        assert data.explicit_bounds == LATENCY_BUCKETS_SECONDS
        assert data.count == 5
        assert sum(data.bucket_counts) == 5
        assert data.bucket_counts[2] == 1  # 10-25ms
        assert data.bucket_counts[5] == 1  # 100-250ms
    finally:
        provider.shutdown()
