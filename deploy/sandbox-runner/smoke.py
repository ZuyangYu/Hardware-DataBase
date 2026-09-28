#!/usr/bin/env python3
"""Contract + real-runtime validation for the reference Sandbox Runner.

Checks (no HDB dependency, standard library only):

1. bearer authentication and audience rejection;
2. fixed bundle digest/profile enforcement and unknown-reference rejection;
3. a real hardened container run producing a verified artifact manifest;
4. observe/collect idempotent reads;
5. timeout handling for a slow bundle;
6. orphan-container reconciliation.

Run with the Runner up and ``SANDBOX_RUNNER_TOKEN_FILE`` pointing at the
operator-mounted token (default: ``deploy/sandbox-runner/secrets/token``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.getenv("SANDBOX_RUNNER_URL", "http://127.0.0.1:8780").rstrip("/")
TOKEN_FILE = os.getenv("SANDBOX_RUNNER_TOKEN_FILE", os.path.join(HERE, "secrets", "token"))
BUNDLE_ROOT = os.path.join(HERE, "bundles")
GATEWAY_TOKEN_FILE = os.path.join(HERE, "..", "mcp-gateway", "secrets", "runner-token")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(f"{name}{' - ' + detail if detail else ''}")
    print(f"[{'ok' if condition else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def token() -> str:
    with open(TOKEN_FILE, encoding="utf-8") as handle:
        return handle.read().strip()


def envelope(*, audience: str = "sandbox-runner", bearer: str | None = None) -> dict[str, str]:
    return {
        "X-Request-ID": f"req-{secrets.token_hex(8)}",
        "X-Actor-ID": "hdb-live-e2e",
        "X-Department-ID": "47",
        "X-Policy-Snapshot-Hash": "policy-live-1",
        "X-Capability-Snapshot": "sandbox.execute,sandbox.write_artifact",
        "X-Audience": audience,
        "Authorization": f"Bearer {bearer if bearer is not None else token()}",
        "Content-Type": "application/json",
    }


def request(method: str, path: str, payload: dict | None = None, headers: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    for key, value in (headers or envelope()).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:200].decode("utf-8", "replace")}


def bundle_digest(name: str) -> str:
    with open(os.path.join(BUNDLE_ROOT, name, "entrypoint.py"), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def submit(bundle: str, parameters: dict, *, digest: str | None = None, profile: str = "profile/dev-l1"):
    return request(
        "POST",
        "/v1/tasks",
        {
            "task_id": "pending",
            "bundle_ref": bundle,
            "bundle_digest": digest or bundle_digest(bundle.rsplit("/", 1)[-1]),
            "profile_ref": profile,
            "kind": "task",
            "network_policy_ref": "none",
            "input_artifact_ids": [],
            "parameters": parameters,
            "capability_snapshot_hash": "",
        },
        headers={**envelope(), "Idempotency-Key": f"idem-{secrets.token_hex(8)}"},
    )


def main() -> int:
    status, body = request("GET", "/v1/health")
    check("health", status == 200 and body.get("status") == "up", str(body)[:80])

    status, body = request("GET", "/v1/health", headers=envelope(bearer="wrong-token"))
    check("bad token rejected", status == 401, f"status={status}")

    status, body = request("GET", "/v1/health", headers=envelope(audience="sandbox-unknown"))
    check("audience rejected", status == 403, f"status={status}")

    status, body = submit("bundle/report", {"title": "live", "rows": 3})
    ok = status == 200 and body.get("status") == "succeeded"
    check("fixed bundle runs on the real runtime", ok, f"status={status} body={str(body)[:120]}")
    task_id = body.get("task_id", "")
    manifest = body.get("output_manifest") or {}
    items = manifest.get("items") or []
    check("manifest has two outputs", len(items) == 2, f"items={len(items)}")
    if items:
        sample = items[0]
        raw = base64.b64decode(sample["content_base64"])
        check(
            "artifact sha256 matches content",
            hashlib.sha256(raw).hexdigest() == sample["sha256"],
            sample["filename"],
        )
        check("artifact is bound to the execution", bool(sample.get("execution_id")))

    if task_id:
        status, body = request("GET", f"/v1/tasks/{task_id}")
        observation = body.get("observation") or {}
        check("observe returns succeeded", status == 200 and observation.get("status") == "succeeded")
        status, body = request("POST", f"/v1/tasks/{task_id}/collect")
        check("collect returns the manifest", status == 200 and len((body.get("output_manifest") or {}).get("items", [])) == 2)

    status, body = submit("bundle/report", {"title": "x"}, digest="0" * 64)
    check("digest mismatch rejected", status == 400 and (body.get("detail") or {}).get("error", {}).get("code") == "digest_mismatch", f"status={status}")

    status, body = submit("bundle/unknown", {"title": "x"}, digest="0" * 64)
    check("unknown bundle rejected", status == 404, f"status={status}")

    with open(GATEWAY_TOKEN_FILE, encoding="utf-8") as handle:
        gateway_token = handle.read().strip()
    with open(os.path.join(HERE, "service-profiles.json"), encoding="utf-8") as handle:
        profile = json.load(handle)["deploy/sandbox-info"]
    service = {
        "deployment_ref": "deploy/sandbox-info",
        "profile_ref": profile["profile_ref"],
        "image_digest": profile["image_digest"],
    }
    status, _ = request("POST", "/v1/services", service, headers=envelope(audience="sandbox-gateway"))
    check("HDB token cannot start services", status == 401, f"status={status}")
    gateway_headers = envelope(audience="sandbox-gateway", bearer=gateway_token)
    status, _ = request("POST", "/v1/services", {**service, "profile_ref": "profile/unreviewed"}, headers=gateway_headers)
    check("service profile mismatch rejected", status == 409, f"status={status}")
    status, _ = request("POST", "/v1/services", {**service, "image_digest": "sha256:" + "0" * 64}, headers=gateway_headers)
    check("service image digest mismatch rejected", status == 409, f"status={status}")
    status, _ = request("POST", "/v1/services", {**service, "deployment_ref": "deploy/unknown"}, headers=gateway_headers)
    check("unreviewed service rejected", status == 404, f"status={status}")

    status, body = submit("bundle/slow", {"seconds": 30})
    check("slow bundle times out and cleans up", status == 200 and body.get("status") == "timed_out", f"status={status} body={str(body)[:100]}")

    orphan = f"hdb-orphan-{secrets.token_hex(4)}"
    subprocess.run(
        [
            "docker", "run", "-d", "--name", orphan, "--label", "hdb.sandbox.runner=1",
            "--entrypoint", "/usr/local/bin/python3", "hdb-sandbox-bundle:1.0",
            "-c", "import time; time.sleep(60)",
        ],
        capture_output=True,
        timeout=60,
    )
    status, body = request("POST", "/v1/reconcile")
    check("orphan container reconciled", status == 200 and orphan in (body.get("removed") or []), f"status={status}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
