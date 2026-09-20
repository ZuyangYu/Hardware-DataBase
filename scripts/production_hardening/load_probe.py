"""Fixed-arrival-rate probe for capacity baselines and overload tests.

Two modes:

- ``health``: fixed-rate ``GET /health`` probes; measures API availability and latency.
- ``turn``: fixed-rate turn submissions (login -> session -> turn -> start -> poll to
  terminal state); measures submit latency and end-to-end turn time.  Polling never
  blocks the arrival schedule; in-flight submissions are bounded by ``--max-inflight``
  and shed requests are counted, not retried.

Run against a test deployment only.  Requires a valid user; credentials are read from
``--username``/``--password`` or the environment (HDB_PROBE_USER/HDB_PROBE_PASSWORD).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


class _Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base_url = args.base_url.rstrip("/")
        self._lock = threading.Lock()
        self.samples: list[dict[str, Any]] = []
        self._inflight = 0

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=self.args.timeout, follow_redirects=True)

    def authenticate(self) -> str:
        if self.args.token:
            return self.args.token
        username = self.args.username or os.environ.get("HDB_PROBE_USER", "")
        password = self.args.password or os.environ.get("HDB_PROBE_PASSWORD", "")
        if not username or not password:
            raise SystemExit("turn mode requires --token or --username/--password (or HDB_PROBE_USER/HDB_PROBE_PASSWORD)")
        with self._client() as client:
            response = client.post("/api/v1/login", json={"username": username, "password": password})
            response.raise_for_status()
            return str(response.json()["token"])

    def _record(self, sample: dict[str, Any]) -> None:
        with self._lock:
            self.samples.append(sample)

    def _try_admit(self) -> bool:
        with self._lock:
            if self._inflight >= self.args.max_inflight:
                return False
            self._inflight += 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def probe_health(self) -> None:
        started = time.monotonic()
        try:
            with self._client() as client:
                response = client.get("/health")
            self._record(
                {"kind": "health", "status": response.status_code, "latency_s": time.monotonic() - started}
            )
        except Exception as exc:  # noqa: BLE001 - probe records any transport failure
            self._record({"kind": "health", "status": 0, "latency_s": time.monotonic() - started, "error": str(exc)})

    def probe_turn(self, token: str, session_ids: list[int]) -> None:
        if not self._try_admit():
            self._record({"kind": "turn", "shed": True})
            return
        started = time.monotonic()
        headers = {"Authorization": f"Bearer {token}"}
        sample: dict[str, Any] = {"kind": "turn", "shed": False, "ttft_s": None}
        try:
            with self._client() as client:
                client.headers.update(headers)
                session_id = random.choice(session_ids)
                body = {"query": self.args.query, "source_scope": "knowledge_base_only"}
                response = client.post(f"/api/v1/conversations/{session_id}/turns", json=body)
                sample["submit_status"] = response.status_code
                if response.status_code != 201:
                    sample["error"] = response.text[:300]
                    return
                turn_id = str(response.json()["turn"]["id"])
                sample["turn_id"] = turn_id
                start = client.post(f"/api/v1/turns/{turn_id}/start")
                sample["start_status"] = start.status_code
                deadline = time.monotonic() + self.args.turn_timeout
                if self.args.ttft:
                    self._stream_ttft(client, turn_id, sample, started, deadline)
                    if sample.get("terminal_status"):
                        return
                while time.monotonic() < deadline:
                    detail = client.get(f"/api/v1/turns/{turn_id}")
                    if detail.status_code != 200:
                        time.sleep(self.args.poll_interval)
                        continue
                    status = str(detail.json().get("status") or "")
                    if status in TERMINAL_STATUSES:
                        sample["terminal_status"] = status
                        break
                    time.sleep(self.args.poll_interval)
                else:
                    sample["terminal_status"] = "timeout"
        except Exception as exc:  # noqa: BLE001
            sample["error"] = str(exc)
        finally:
            sample["e2e_s"] = time.monotonic() - started
            self._release()
            self._record(sample)

    def _stream_ttft(
        self,
        client: httpx.Client,
        turn_id: str,
        sample: dict[str, Any],
        started: float,
        deadline: float,
    ) -> None:
        """Read the persisted event stream to measure first-token latency.

        The stream is opened right after ``/start``; a terminal event observed
        here also supplies the terminal status so no polling is needed.
        """
        event_type = ""
        try:
            with client.stream(
                "GET", f"/api/v1/turns/{turn_id}/events", timeout=self.args.turn_timeout
            ) as response:
                if response.status_code != 200:
                    return
                for line in response.iter_lines():
                    if time.monotonic() > deadline:
                        sample["terminal_status"] = "timeout"
                        return
                    if not line:
                        continue
                    if line.startswith("event: "):
                        event_type = line[len("event: ") :].strip()
                        continue
                    if not line.startswith("data: "):
                        continue
                    if event_type == "delta" and sample.get("ttft_s") is None:
                        sample["ttft_s"] = time.monotonic() - started
                    elif event_type in {"turn_end", "error"}:
                        try:
                            payload = json.loads(line[len("data: ") :])
                        except Exception:
                            payload = {}
                        sample["terminal_status"] = str(
                            payload.get("status") or ("failed" if event_type == "error" else "completed")
                        )
                        return
        except Exception:
            # Fall back to polling; TTFT simply stays unmeasured for this turn.
            return

    def run(self) -> dict[str, Any]:
        token = ""
        session_ids: list[int] = []
        if self.args.mode == "turn":
            token = self.authenticate()
            with self._client() as client:
                client.headers.update({"Authorization": f"Bearer {token}"})
                for index in range(self.args.sessions):
                    response = client.post(
                        "/api/v1/conversations",
                        json={"kb_name": self.args.kb, "title": f"load-probe-{index}"},
                    )
                    response.raise_for_status()
                    session_ids.append(int(response.json()["id"]))

        total = max(1, int(self.args.rate * self.args.duration))
        interval = 1.0 / self.args.rate
        started = time.monotonic()
        threads: list[threading.Thread] = []
        for index in range(total):
            target = started + index * interval
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if time.monotonic() - started < self.args.warmup:
                continue
            if self.args.mode == "health":
                self.probe_health()
            else:
                thread = threading.Thread(target=self.probe_turn, args=(token, session_ids), daemon=True)
                thread.start()
                threads.append(thread)
        for thread in threads:
            thread.join(timeout=self.args.turn_timeout + 30)

        return self._report(session_ids)

    def _report(self, session_ids: list[int]) -> dict[str, Any]:
        with self._lock:
            samples = list(self.samples)
        health = [s for s in samples if s.get("kind") == "health"]
        turns = [s for s in samples if s.get("kind") == "turn"]
        report: dict[str, Any] = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.args.mode,
            "target_rate": self.args.rate,
            "duration_s": self.args.duration,
            "warmup_s": self.args.warmup,
            "sessions": session_ids,
            "health": {
                "probes": len(health),
                "errors": sum(1 for s in health if int(s.get("status") or 0) == 0 or int(s.get("status") or 0) >= 500),
                "latency": _summary([float(s["latency_s"]) for s in health]),
            },
            "turns": {
                "submitted": len(turns),
                "shed": sum(1 for s in turns if s.get("shed")),
                "accepted": sum(1 for s in turns if int(s.get("submit_status") or 0) == 201),
                "rejected": sum(1 for s in turns if 400 <= int(s.get("submit_status") or 0) < 500),
                "completed": sum(1 for s in turns if s.get("terminal_status") == "completed"),
                "failed": sum(1 for s in turns if s.get("terminal_status") in {"failed", "timeout"}),
                "submit_latency": _summary(
                    [float(s["e2e_s"]) for s in turns if s.get("submit_status") in {200, 201, 202}]
                ),
                "e2e_latency": _summary([float(s["e2e_s"]) for s in turns if s.get("terminal_status")]),
                "ttft_latency": _summary(
                    [float(s["ttft_s"]) for s in turns if s.get("ttft_s") is not None]
                ),
            },
        }
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mode", choices=("health", "turn"), default="health")
    parser.add_argument("--rate", type=float, default=1.0, help="arrival rate (requests/second)")
    parser.add_argument("--duration", type=float, default=60.0, help="measurement duration in seconds")
    parser.add_argument("--warmup", type=float, default=0.0, help="warmup seconds excluded from arrival count")
    parser.add_argument("--max-inflight", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--turn-timeout", type=float, default=600.0, help="turn completion timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--ttft",
        action="store_true",
        help="read the persisted event stream to measure first-token latency (turn mode)",
    )
    parser.add_argument("--sessions", type=int, default=4, help="pre-created sessions for turn mode")
    parser.add_argument("--kb", default="__general__", help="knowledge base name for load sessions")
    parser.add_argument("--query", default="压测探测：请忽略此问题。")
    parser.add_argument("--token", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--output", default="", help="JSON report path; defaults to stdout")
    args = parser.parse_args(argv)

    report = _Probe(args).run()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
