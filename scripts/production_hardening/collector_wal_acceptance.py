"""Isolated Collector 0.128.0: backend 503 → durable queue → SIGKILL → replay."""
from __future__ import annotations

import gzip
import json
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExportResult


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Collector acceptance condition timed out")


def main():
    root = Path(__file__).resolve().parents[2]
    artifact = root / "storage/evaluations/production-feedback-p2-20261008"
    artifact.mkdir(parents=True, exist_ok=True)
    state = {"offline": True, "failures": 0, "received": set(), "requests": 0}
    lock = threading.Lock()

    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            with lock:
                state["requests"] += 1
                offline = state["offline"]
            if offline:
                with lock:
                    state["failures"] += 1
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            request = ExportTraceServiceRequest.FromString(body)
            ids = {s.span_id.hex() for r in request.resource_spans for scope in r.scope_spans for s in scope.spans}
            with lock:
                state["received"].update(ids)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    backend_port, receiver_port, grpc_port, metrics_port = (port() for _ in range(4))
    backend = ThreadingHTTPServer(("127.0.0.1", backend_port), Backend)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    name = "hdb-wal-contract-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="hdb-wal-", dir="/tmp/opencode") as tmp:
        directory = Path(tmp)
        storage = directory / "wal"
        storage.mkdir()
        # Collector runs as 10001:10001. The enclosing fixture directory is
        # private (0700); permit that container UID inside this disposable mount.
        storage.chmod(0o777)
        config = yaml.safe_load((root / "deploy/observability/otel-collector/config.yaml").read_text())
        config["receivers"]["otlp"]["protocols"]["http"]["endpoint"] = f"127.0.0.1:{receiver_port}"
        config["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"] = f"127.0.0.1:{grpc_port}"
        config["exporters"] = {"otlphttp/phoenix": config["exporters"]["otlphttp/phoenix"]}
        config["exporters"]["otlphttp/phoenix"].update(endpoint=f"http://127.0.0.1:{backend_port}", timeout="2s")
        config["exporters"]["otlphttp/phoenix"]["sending_queue"]["num_consumers"] = 1
        config["processors"]["batch"].update(send_batch_size=1, send_batch_max_size=1)
        config["service"]["pipelines"] = {"traces": config["service"]["pipelines"]["traces"]}
        config["service"]["pipelines"]["traces"]["exporters"] = ["otlphttp/phoenix"]
        config["service"]["telemetry"]["metrics"]["readers"][0]["pull"]["exporter"]["prometheus"]["port"] = metrics_port
        config_file = directory / "config.yaml"
        config_file.write_text(yaml.safe_dump(config))
        args = ["docker", "run", "-d", "--name", name, "--network", "host", "-e", "OBS_PHOENIX_PROJECT=fixture",
                "-v", f"{config_file}:/etc/otelcol-contrib/config.yaml:ro", "-v", f"{storage}:/var/lib/otelcol-storage",
                "otel/opentelemetry-collector-contrib:0.128.0", "--config=/etc/otelcol-contrib/config.yaml"]
        exporter = None
        try:
            subprocess.run(args, check=True, capture_output=True)
            metrics_url = f"http://127.0.0.1:{metrics_port}/metrics"
            wait_for(lambda: httpx.get(metrics_url, timeout=1).status_code == 200)
            provider = TracerProvider()
            spans = [provider.get_tracer("wal-fixture").start_span(f"fixture-{n}") for n in range(10)]
            for span in spans:
                span.end()
            expected = {f"{s.get_span_context().span_id:016x}" for s in spans}
            exporter = OTLPSpanExporter(endpoint=f"http://127.0.0.1:{receiver_port}/v1/traces", timeout=5)
            assert exporter.export(spans) == SpanExportResult.SUCCESS
            def queued():
                text = httpx.get(metrics_url, timeout=1).text
                (artifact / "collector-self-before.txt").write_text(text)
                values = re.findall(r'^otelcol_exporter_queue_size\{[^\n]*\}\s+(\d+(?:\.\d+)?)$', text, re.M)
                return text if any(float(v) >= 1 for v in values) and state["failures"] else None
            before = wait_for(queued)
            wal_bytes = sum(p.stat().st_size for p in storage.rglob("*") if p.is_file())
            assert wal_bytes > 0
            subprocess.run(["docker", "kill", "--signal", "KILL", name], check=True, capture_output=True)
            with lock:
                state["offline"] = False
            started = time.monotonic()
            subprocess.run(["docker", "start", name], check=True, capture_output=True)
            wait_for(lambda: expected.issubset(state["received"]))
            elapsed = time.monotonic() - started
            after = httpx.get(metrics_url, timeout=2).text
            (artifact / "collector-self-before.txt").write_text(before)
            (artifact / "collector-self-after.txt").write_text(after)
            result = {"collector_version": "0.128.0", "sent_to_receiver": len(expected), "receiver_acknowledged": True,
                      "persisted_wal_bytes": wal_bytes, "backend_503_requests": state["failures"], "unique_exported_after_restart": len(state["received"]),
                      "sigkill_recovery_seconds": round(elapsed, 3), "lost_queued_spans": len(expected - state["received"]),
                      "evidence_scope": "accepted_and_persistently_queued_spans", "fixture_only": True,
                      "untested_failure_modes": ["queue_full", "disk_full", "sdk_before_export"]}
            (artifact / "collector-wal.json").write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result))
            provider.shutdown()
        finally:
            if exporter:
                exporter.shutdown()
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            (artifact / "collector-wal.log").write_text(logs.stdout + logs.stderr)
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
            backend.shutdown()
            backend.server_close()


if __name__ == "__main__":
    main()
