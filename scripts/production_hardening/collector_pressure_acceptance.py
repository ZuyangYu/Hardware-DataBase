"""Measure bounded Collector queue-full and runtime WAL disk-full losses."""
from __future__ import annotations

import gzip
import json
import re
import secrets
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider

from collector_wal_acceptance import port, wait_for


def number(text, name):
    return sum(float(v) for v in re.findall(r'^'+re.escape(name)+r'(?:\{[^\n]*\})?\s+(\d+(?:\.\d+)?)$', text, re.M))


def run(mode, root, artifact):
    state = {'offline': True, 'received': set(), 'failures': 0}
    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            if state['offline']:
                state['failures'] += 1
                self.send_response(503)
            else:
                if self.headers.get('Content-Encoding') == 'gzip':
                    body = gzip.decompress(body)
                req = ExportTraceServiceRequest.FromString(body)
                state['received'].update(s.span_id.hex() for resource in req.resource_spans for scope in resource.scope_spans for s in scope.spans)
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-protobuf')
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *_):
            pass
    backend_port, otlp_port, grpc_port, self_port = [port() for _ in range(4)]
    server = ThreadingHTTPServer(('127.0.0.1', backend_port), Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    name = 'hdb-pressure-contract-'+uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix='hdb-pressure-', dir='/tmp/opencode') as tmp:
        config = yaml.safe_load((root/'deploy/observability/otel-collector/config.yaml').read_text())
        config['receivers']['otlp']['protocols']['http']['endpoint'] = f'127.0.0.1:{otlp_port}'
        config['receivers']['otlp']['protocols']['grpc']['endpoint'] = f'127.0.0.1:{grpc_port}'
        config['processors']['batch'].update(send_batch_size=1, send_batch_max_size=1)
        exporter_config = config['exporters']['otlphttp/phoenix']
        exporter_config.update(endpoint=f'http://127.0.0.1:{backend_port}', timeout='1s')
        exporter_config['sending_queue'].update(queue_size=1 if mode == 'queue_full' else 10000, num_consumers=1)
        config['exporters'] = {'otlphttp/phoenix': exporter_config}
        config['service']['pipelines'] = {'traces': config['service']['pipelines']['traces']}
        config['service']['pipelines']['traces']['exporters'] = ['otlphttp/phoenix']
        config['service']['telemetry']['metrics']['readers'][0]['pull']['exporter']['prometheus']['port'] = self_port
        file = Path(tmp)/'config.yaml'
        file.write_text(yaml.safe_dump(config))
        size = '512k' if mode == 'disk_full' else '16m'
        provider = TracerProvider()
        exporter = OTLPSpanExporter(endpoint=f'http://127.0.0.1:{otlp_port}/v1/traces', timeout=5)
        try:
            subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'host', '-e', 'OBS_PHOENIX_PROJECT=fixture',
                            '--tmpfs', f'/var/lib/otelcol-storage:rw,size={size},uid=10001,gid=10001',
                            '-v', f'{file}:/etc/otelcol-contrib/config.yaml:ro', 'otel/opentelemetry-collector-contrib:0.128.0',
                            '--config=/etc/otelcol-contrib/config.yaml'], check=True, capture_output=True)
            url = f'http://127.0.0.1:{self_port}/metrics'
            wait_for(lambda: httpx.get(url, timeout=1).status_code == 200)
            spans = []
            for n in range(200):
                span = provider.get_tracer('pressure-fixture').start_span('pressure')
                if mode == 'disk_full':
                    span.set_attribute('fixture.random', secrets.token_hex(8192))
                span.end()
                spans.append(span)
            export_result = str(exporter.export(spans))
            def failed_enqueue():
                text = httpx.get(url, timeout=2).text
                return text if number(text, 'otelcol_exporter_enqueue_failed_spans_total') > 0 else None
            before = wait_for(failed_enqueue)
            state['offline'] = False
            wait_for(lambda: state['received'])
            # Quiet queue confirms recovery; in-flight delivery is then observed.
            wait_for(lambda: number(httpx.get(url, timeout=2).text, 'otelcol_exporter_queue_size') == 0)
            after = httpx.get(url, timeout=2).text
            logs = subprocess.run(['docker', 'logs', name], capture_output=True, text=True)
            log = logs.stdout+logs.stderr
            if mode == 'disk_full':
                assert 'no space left on device' in log.lower(), 'runtime disk-full fault was not observed'
            refused = number(before, 'otelcol_exporter_enqueue_failed_spans_total')
            accepted = number(before, 'otelcol_receiver_accepted_spans_total')
            expected = {f'{s.get_span_context().span_id:016x}' for s in spans}
            missing = expected-state['received']
            result = {'collector_version': '0.128.0', 'failure_mode': mode, 'fixture_only': True, 'sdk_attempted_spans': len(spans),
                      'sdk_export_result': export_result, 'receiver_accepted_spans': accepted, 'enqueue_failed_spans': refused,
                      'exported_unique_spans_after_backend_recovery': len(state['received']), 'undelivered_spans': len(missing),
                      'loss_count_explained_by_enqueue_failures': len(missing) == refused,
                      'backend_503_requests': state['failures'], 'runtime_enospc_verified': mode == 'disk_full',
                      'wal_tmpfs_fixture_bytes': 512*1024 if mode == 'disk_full' else 16*1024*1024}
            assert len(missing) == refused and refused > 0, result
            (artifact/f'collector-{mode}.json').write_text(json.dumps(result, indent=2)+'\n')
            (artifact/f'collector-{mode}-before.txt').write_text(before)
            (artifact/f'collector-{mode}-after.txt').write_text(after)
            (artifact/f'collector-{mode}.log').write_text(log)
            return result
        finally:
            exporter.shutdown()
            provider.shutdown()
            subprocess.run(['docker', 'rm', '-f', name], check=False, capture_output=True)
            server.shutdown()
            server.server_close()


def main():
    root = Path(__file__).resolve().parents[2]
    artifact = root/'storage/evaluations/production-feedback-p2-20261008'
    artifact.mkdir(parents=True, exist_ok=True)
    print(json.dumps([run(mode, root, artifact) for mode in ('queue_full', 'disk_full')]))


if __name__ == '__main__':
    main()
