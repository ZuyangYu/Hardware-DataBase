"""Compare SDK spans killed before batch export with a graceful flush.

The receiver only sees already-exported bytes; Collector WAL cannot recover
the first child's in-process queue. Uses synthetic spans and a local receiver.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def child(endpoint):
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5),
        schedule_delay_millis=60000, max_queue_size=1024, max_export_batch_size=512))
    tracer = provider.get_tracer("preexport-fixture")
    for _ in range(10):
        with tracer.start_as_current_span("preexport"):
            pass
    print(json.dumps({"ended_spans": 10, "batch_delay_ms": 60000}), flush=True)
    sys.stdin.readline()  # Parent either SIGKILLs or explicitly requests flush.
    provider.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child-endpoint")
    args = parser.parse_args()
    if args.child_endpoint:
        child(args.child_endpoint)
        return

    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            req = ExportTraceServiceRequest.FromString(body)
            received.extend(s.span_id.hex() for r in req.resource_spans for scope in r.scope_spans for s in scope.spans)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
    results, processes = [], []
    try:
        for mode in ("sigkill", "graceful_shutdown"):
            before = len(received)
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child-endpoint", endpoint],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(process)
            record = json.loads(process.stdout.readline())
            assert record["ended_spans"] == 10
            if mode == "sigkill":
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            else:
                _, stderr = process.communicate(input="flush\n", timeout=20)
                assert process.returncode == 0, stderr
            count = len(received)-before
            assert count == (0 if mode == "sigkill" else 10)
            results.append({"mode": mode, "sdk_ended_spans": 10, "receiver_spans": count, "unexported_spans": 10-count})
        artifact = Path(__file__).resolve().parents[2] / "storage/evaluations/production-feedback-p2-20261008"
        artifact.mkdir(parents=True, exist_ok=True)
        result = {"fixture_only": True, "collector_wal_can_recover_sdk_memory": False, "runs": results}
        (artifact/"sdk-preexport.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(result))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
