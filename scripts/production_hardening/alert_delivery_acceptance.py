"""Real isolated Alertmanager → HTTP webhook firing/resolved delivery contract."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml

from collector_wal_acceptance import port, wait_for


def main():
    artifact = Path(__file__).resolve().parents[2] / 'storage/evaluations/production-feedback-p2-20261008'
    artifact.mkdir(parents=True, exist_ok=True)
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *_):
            pass
    webhook_port, api_port = port(), port()
    server = ThreadingHTTPServer(('127.0.0.1', webhook_port), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    name = 'hdb-alert-contract-' + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix='hdb-alert-', dir='/tmp/opencode') as tmp:
        config = Path(tmp)/'alertmanager.yml'
        config.write_text(yaml.safe_dump({'route': {'receiver': 'fixture', 'group_by': ['alertname'], 'group_wait': '1s', 'group_interval': '1s', 'repeat_interval': '1m'},
            'receivers': [{'name': 'fixture', 'webhook_configs': [{'url': f'http://127.0.0.1:{webhook_port}/alerts', 'send_resolved': True}]}]}))
        try:
            subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'host', '-v', f'{config}:/etc/alertmanager/alertmanager.yml:ro',
                            'prom/alertmanager:v0.28.1', '--config.file=/etc/alertmanager/alertmanager.yml', '--storage.path=/tmp/alerts',
                            f'--web.listen-address=127.0.0.1:{api_port}', '--cluster.listen-address='], check=True, capture_output=True)
            url = f'http://127.0.0.1:{api_port}'
            wait_for(lambda: httpx.get(url+'/-/ready', timeout=1).status_code == 200)
            start = datetime.now(timezone.utc)-timedelta(seconds=2)
            alert = {'labels': {'alertname': 'HdbCostAccountingLoss', 'severity': 'critical', 'instance': 'isolated-fixture'},
                     'annotations': {'summary': 'Cost accounting fixture coverage alert'}, 'startsAt': start.isoformat(),
                     'endsAt': (start+timedelta(minutes=10)).isoformat()}
            fired = time.monotonic()
            httpx.post(url+'/api/v2/alerts', json=[alert], timeout=5).raise_for_status()
            wait_for(lambda: any(r['status'] == 'firing' for r in received))
            firing_seconds = time.monotonic()-fired
            alert['endsAt'] = datetime.now(timezone.utc).isoformat()
            httpx.post(url+'/api/v2/alerts', json=[alert], timeout=5).raise_for_status()
            wait_for(lambda: any(r['status'] == 'resolved' for r in received))
            result = {'alertmanager_version': '0.28.1', 'fixture_only': True, 'notification_target': 'isolated_http_webhook',
                      'firing_received': True, 'resolved_received': True, 'firing_delivery_seconds': round(firing_seconds, 3),
                      'messages': received, 'rule_evaluation_evidence': 'separate_promtool_tests', 'production_receiver_verified': False}
            (artifact/'alert-delivery.json').write_text(json.dumps(result, indent=2)+'\n')
            print(json.dumps({k: v for k, v in result.items() if k != 'messages'}))
        finally:
            logs = subprocess.run(['docker', 'logs', name], capture_output=True, text=True)
            (artifact/'alert-delivery.log').write_text(logs.stdout+logs.stderr)
            subprocess.run(['docker', 'rm', '-f', name], check=False, capture_output=True)
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
