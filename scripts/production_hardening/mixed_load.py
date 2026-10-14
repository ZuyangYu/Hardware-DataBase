"""Fixed-arrival mixed HTTP workload with durable per-operation observations.

Credentials come from HDB_PROBE_USER/HDB_PROBE_PASSWORD. Workload and model
references are frozen in the report. Optional document generation requires a
deployment-specific work-order payload in the workload JSON.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import subprocess
import threading
import time
import uuid

import httpx

from src.evaluation.http_events import iter_sse_events

KINDS = {"chat", "parse", "export", "document"}


def distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None}
    ordered = sorted(values)
    return {"count": len(values), **{f"p{p}": ordered[min(len(values) - 1, max(0, math.ceil(p / 100 * len(values)) - 1))]
                                   for p in (50, 95, 99)}, "max": ordered[-1]}


def summarize(samples: list[dict]) -> dict:
    accepted = [row for row in samples if row.get("accepted")]
    succeeded = [row for row in accepted if row.get("success")]
    failed = [row for row in accepted if not row.get("success")]
    return {
        "scheduled": len(samples), "accepted": len(accepted), "succeeded": len(succeeded),
        "accepted_failures": len(failed), "accepted_failure_rate": len(failed) / len(accepted) if accepted else None,
        "client_shed": sum(bool(row.get("client_shed")) for row in samples),
        "server_rejected": sum(row.get("submit_status") in {409, 429, 503} for row in samples),
        "transport_or_setup_failures": sum(not row.get("accepted") and not row.get("client_shed")
                                            and row.get("submit_status") not in {409, 429, 503} for row in samples),
        "cleanup_failures": sum(row.get("cleanup_status", 204) not in {200, 204, 404} for row in samples),
        **{key: distribution([float(row[key]) for row in accepted if row.get(key) is not None])
           for key in ("e2e_s", "ttft_s", "queue_s", "scheduler_lag_s")},
    }


def validate_workload(spec: dict) -> dict:
    weights = spec.get("weights", {})
    if not weights or set(weights) - KINDS or any(not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
        raise ValueError("weights must contain non-negative chat/parse/export/document values and a positive total")
    if weights.get("document", 0) > 0 and not spec.get("document_payload"):
        raise ValueError("document workload requires a deployment-specific document_payload")
    if weights.get("document", 0) > 0 and not spec.get("document_kb"):
        raise ValueError("document workload requires document_kb")
    fixtures = spec.get("fixtures", [])
    if weights.get("parse", 0) > 0 and not fixtures:
        raise ValueError("parse workload requires fixtures")
    if (weights.get("chat", 0) or weights.get("export", 0)) and not str(spec.get("chat_query", "")).strip():
        raise ValueError("chat/export workload requires chat_query")
    for fixture in fixtures:
        if fixture.get("kind") not in {"md", "txt", "docx", "xlsx"} or not fixture.get("content"):
            raise ValueError("fixtures require kind md/txt/docx/xlsx and non-empty content")
    return spec


def fixture_bytes(fixture: dict) -> tuple[str, bytes, str]:
    kind = fixture["kind"]
    content = fixture["content"]
    if kind in {"md", "txt"}:
        return f"capacity.{kind}", content.encode(), "text/markdown" if kind == "md" else "text/plain"
    output = io.BytesIO()
    if kind == "docx":
        from docx import Document
        document = Document()
        for line in content.splitlines():
            document.add_paragraph(line)
        document.save(output)
        return "capacity.docx", output.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    from src.result_exports.models import ResultEnvelope
    from src.result_exports.renderers import render_result
    rows = [line.split("\t") for line in content.splitlines()]
    rendered = render_result(ResultEnvelope(tables=[{"title": "接口", "columns": rows[0], "rows": rows[1:]}]),
                             "xlsx", content_shape="data")
    return "capacity.xlsx", rendered.content, rendered.mime_type


class MixedProbe:
    def __init__(self, args, spec: dict, output: Path):
        self.args, self.spec = args, spec
        self.fixtures = [fixture_bytes(fixture) for fixture in spec.get("fixtures", [])]
        self.client = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=httpx.Timeout(30, read=args.timeout),
                                   limits=httpx.Limits(max_connections=args.max_inflight * 4 + 4))
        self.lock = threading.Lock()
        self.permits = threading.BoundedSemaphore(args.max_inflight)
        self.log = (output / "samples.jsonl").open("x", encoding="utf-8")
        self.seed_session = None
        self.seed_turn = None
        fixture_dir = output / "fixtures"
        fixture_dir.mkdir()
        for index, (name, content, _) in enumerate(self.fixtures):
            (fixture_dir / f"{index}-{name}").write_bytes(content)

    def record(self, sample: dict):
        with self.lock:
            self.log.write(json.dumps(sample, ensure_ascii=False) + "\n")
            self.log.flush()

    def authenticate(self):
        response = self.client.post("/api/v1/login", json={"username": os.environ["HDB_PROBE_USER"],
                                                         "password": os.environ["HDB_PROBE_PASSWORD"]})
        response.raise_for_status()
        self.client.headers["Authorization"] = "Bearer " + response.json()["token"]

    def session(self) -> int:
        response = self.client.post("/api/v1/conversations", json={"kb_name": "__general__", "title": "Capacity " + uuid.uuid4().hex[:12]})
        response.raise_for_status()
        return int(response.json()["id"])

    def turn(self, session: int, sample: dict, started: float):
        response = self.client.post(f"/api/v1/conversations/{session}/turns", json={"query": self.spec["chat_query"], "client_request_id": uuid.uuid4().hex})
        sample["submit_status"] = response.status_code
        if response.status_code != 201:
            return
        sample["accepted"] = True
        turn_id = response.json()["turn"]["id"]
        sample["turn_id"] = turn_id
        start = self.client.post(f"/api/v1/turns/{turn_id}/start")
        start.raise_for_status()
        deadline = started + self.args.timeout
        with self.client.stream("GET", f"/api/v1/turns/{turn_id}/events") as events:
            events.raise_for_status()
            def bounded_lines():
                for line in events.iter_lines():
                    if time.monotonic() > deadline:
                        raise TimeoutError("turn deadline exceeded")
                    yield line
            for event in iter_sse_events(bounded_lines()):
                if event["event"] == "delta" and sample.get("ttft_s") is None:
                    sample["ttft_s"] = time.monotonic() - started
                if event["event"] == "turn_end":
                    break
        completed = self.client.get(f"/api/v1/turns/{turn_id}")
        completed.raise_for_status()
        turn = completed.json()
        sample["terminal_status"] = turn["status"]
        sample["success"] = turn["status"] == "completed" and bool(turn.get("answer"))
        queue_ms = (turn.get("metrics") or {}).get("queue_ms")
        if queue_ms is not None:
            sample["queue_s"] = queue_ms / 1000
        return turn_id

    def parse(self, session: int, sample: dict, fixture_index: int):
        name, content, media = self.fixtures[fixture_index % len(self.fixtures)]
        sample["fixture_sha256"] = hashlib.sha256(content).hexdigest()
        response = self.client.post(f"/api/v1/conversations/{session}/attachments", files={"file": (name, content, media)},
                                    data={"client_request_id": uuid.uuid4().hex})
        sample["submit_status"] = response.status_code
        if response.status_code != 201:
            return
        sample["accepted"] = True
        attachment_id = response.json()["attachment_id"]
        sample["attachment_id"] = attachment_id
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/v1/conversations/{session}/attachments/{attachment_id}")
            response.raise_for_status()
            status = response.json().get("parse_status")
            if status in {"ready", "degraded", "failed"}:
                sample.update(terminal_status=status, success=status == "ready")
                return
            time.sleep(0.25)
        raise TimeoutError("parse deadline exceeded")

    def export(self, sample: dict):
        response = self.client.post("/api/v1/exports", json={"source_ref": {"kind": "turn", "id": self.seed_turn},
            "formats": [self.spec.get("export_format", "docx")], "client_request_id": uuid.uuid4().hex, "organize": False})
        sample["submit_status"] = response.status_code
        if response.status_code != 202:
            return
        sample["accepted"] = True
        job = response.json()["jobs"][0]
        sample["export_job_id"] = job["export_job_id"]
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/v1/exports/{job['export_job_id']}")
            response.raise_for_status()
            job = response.json()
            if job["status"] in {"succeeded", "failed", "cancelled", "dead_letter"}:
                sample.update(terminal_status=job["status"], success=job["status"] == "succeeded")
                if sample["success"]:
                    artifact = job["artifact"]
                    content = self.client.get(artifact["download_url"])
                    content.raise_for_status()
                    sample["success"] = hashlib.sha256(content.content).hexdigest() == artifact["sha256"]
                    sample["download_bytes"] = len(content.content)
                return
            time.sleep(0.25)
        raise TimeoutError("export deadline exceeded")

    def document(self, sample: dict):
        kb = self.spec["document_kb"]
        response = self.client.post("/api/v1/document-generation/work-orders", params={"kb": kb},
                                    json={**self.spec["document_payload"], "client_request_id": uuid.uuid4().hex})
        sample["submit_status"] = response.status_code
        if response.status_code not in {200, 201}:
            return
        sample["accepted"] = True
        order_id = response.json()["work_order_id"]
        sample["work_order_id"] = order_id
        self.client.post(f"/api/v1/document-generation/work-orders/{order_id}/generate", params={"kb": kb}).raise_for_status()
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/v1/document-generation/work-orders/{order_id}/status", params={"kb": kb})
            response.raise_for_status()
            status = response.json()
            state = str(status.get("status", ""))
            if state in {"complete", "completed", "failed", "blocked", "cancelled", "waiting_human_input", "waiting_human_approval"}:
                sample.update(terminal_status=state, success=state in {"complete", "completed"})
                if sample["success"]:
                    artifacts = status.get("artifacts", [])
                    if not artifacts:
                        raise ValueError("completed document has no artifacts")
                    for artifact in artifacts:
                        download = self.client.get(f"/api/v1/document-generation/artifacts/{artifact['artifact_id']}/download", params={"kb": kb})
                        download.raise_for_status()
                        if not download.content:
                            raise ValueError("document artifact is empty")
                return
            time.sleep(0.5)
        raise TimeoutError("document deadline exceeded")

    def operation(self, sample: dict, due: float, index: int):
        started = time.monotonic()
        sample.update(scheduler_lag_s=max(0, started - due), accepted=False, success=False)
        session = None
        try:
            if sample["kind"] in {"chat", "parse"}:
                session = self.session()
                sample["session_id"] = session
            if sample["kind"] == "chat":
                self.turn(session, sample, started)
            elif sample["kind"] == "parse":
                self.parse(session, sample, index)
            elif sample["kind"] == "export":
                self.export(sample)
            else:
                self.document(sample)
        except Exception as exc:  # noqa: BLE001 - all failures become observations
            sample["error_type"] = type(exc).__name__
            sample["success"] = False
            if sample.get("turn_id"):
                try:
                    self.client.post(f"/api/v1/turns/{sample['turn_id']}/cancel").raise_for_status()
                except httpx.HTTPError:
                    pass
        finally:
            sample["e2e_s"] = time.monotonic() - started
            if sample.get("work_order_id"):
                try:
                    sample["cleanup_status"] = self.client.request("DELETE", f"/api/v1/document-generation/work-orders/{sample['work_order_id']}",
                        params={"kb": self.spec["document_kb"]}, json={"reason": "capacity acceptance finished"}).status_code
                except httpx.HTTPError:
                    sample["cleanup_status"] = 0
            if session is not None:
                try:
                    sample["cleanup_status"] = self.client.delete(f"/api/v1/conversations/{session}").status_code
                except httpx.HTTPError:
                    sample["cleanup_status"] = 0
            self.record(sample)
            self.permits.release()

    def run(self):
        self.authenticate()
        if self.spec["weights"].get("export", 0):
            self.seed_session = self.session()
            sample = {}
            self.seed_turn = self.turn(self.seed_session, sample, time.monotonic())
            if not sample.get("success"):
                raise RuntimeError("export seed turn did not complete")
        phases = [("warmup", self.args.warmup_seconds, self.args.rate),
                  ("steady", self.args.duration_seconds, self.args.rate),
                  ("overload", self.args.overload_seconds, self.args.rate * 2),
                  ("recovery", self.args.recovery_seconds, self.args.rate)]
        rng = random.Random(self.args.seed)
        kinds, weights = zip(*self.spec["weights"].items())
        phase_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=self.args.max_inflight) as executor:
            for phase, duration, rate in phases:
                index = 0
                while index / rate < duration:
                    due = phase_start + index / rate
                    time.sleep(max(0, due - time.monotonic()))
                    kind = rng.choices(kinds, weights=weights, k=1)[0]
                    sample = {"phase": phase, "kind": kind, "recorded_at": datetime.now(timezone.utc).isoformat()}
                    if self.permits.acquire(blocking=False):
                        executor.submit(self.operation, sample, due, index)
                    else:
                        self.record({**sample, "client_shed": True, "scheduler_lag_s": time.monotonic() - due})
                    index += 1
                phase_start += duration
            time.sleep(max(0, phase_start - time.monotonic()))

    def close(self):
        try:
            if self.seed_session is not None:
                self.client.delete(f"/api/v1/conversations/{self.seed_session}").raise_for_status()
        finally:
            self.client.close()
            self.log.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-reference", required=True)
    parser.add_argument("--rate", type=float, default=0.1)
    parser.add_argument("--duration-seconds", type=float, default=1800)
    parser.add_argument("--warmup-seconds", type=float, default=300)
    parser.add_argument("--overload-seconds", type=float, default=0)
    parser.add_argument("--recovery-seconds", type=float, default=0)
    parser.add_argument("--max-inflight", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--seed", type=int, default=20261008)
    args = parser.parse_args(argv)
    if not all(math.isfinite(value) for value in (args.rate, args.timeout, args.duration_seconds, args.warmup_seconds, args.overload_seconds, args.recovery_seconds)) or args.rate <= 0 or args.max_inflight < 1 or args.timeout <= 0 or args.duration_seconds <= 0 or min(args.warmup_seconds, args.overload_seconds, args.recovery_seconds) < 0:
        parser.error("rate, duration, timeout and max-inflight must be positive; phase durations cannot be negative")
    spec = validate_workload(json.loads(args.workload.read_text(encoding="utf-8")))
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {**vars(args), "workload": spec, "workload_sha256": hashlib.sha256(args.workload.read_bytes()).hexdigest(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    args.output.joinpath("manifest.json").write_text(json.dumps(manifest, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
    probe = None
    error = None
    try:
        probe = MixedProbe(args, spec, args.output)
        ready = probe.client.get("/health/ready")
        ready.raise_for_status()
        manifest["config_revision"] = ready.json().get("config_revision")
        manifest["fixture_sha256"] = [hashlib.sha256(content).hexdigest() for _, content, _ in probe.fixtures]
        args.output.joinpath("manifest.json").write_text(json.dumps(manifest, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
        probe.run()
    except Exception as exc:  # noqa: BLE001 - persist even setup/transport failures
        error = type(exc).__name__
    finally:
        try:
            if probe is not None:
                probe.close()
        except Exception as exc:  # noqa: BLE001
            error = error or type(exc).__name__
    sample_path = args.output.joinpath("samples.jsonl")
    samples = [json.loads(line) for line in sample_path.read_text(encoding="utf-8").splitlines()] if sample_path.exists() else []
    report = {"setup_error": error, "overall": summarize(samples),
              "phases": {phase: summarize([row for row in samples if row["phase"] == phase]) for phase in sorted({row["phase"] for row in samples})},
              "workloads": {kind: summarize([row for row in samples if row["kind"] == kind]) for kind in spec["weights"]}}
    args.output.joinpath("report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    # Observed reliability gate only; deployment-specific latency/capacity SLAs
    # are evaluated against the frozen report by the release owner.
    bad = report["overall"]
    return int(bool(error or not bad["accepted"] or bad["accepted_failures"] or bad["transport_or_setup_failures"] or bad["cleanup_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
