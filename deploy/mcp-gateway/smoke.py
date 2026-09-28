#!/usr/bin/env python3
"""Contract validation for the reference MCP Gateway + stdio MCP service.

Checks (standard library only):

1. bearer authentication and audience rejection;
2. attach starts the reviewed service profile through the Runner;
3. discover returns only allowlisted, read-only tools;
4. invoke forwards a real stdio MCP ``tools/call`` and returns its content;
5. unknown tools and runtime-control arguments are rejected;
6. disable stops the service and detaches the deployment.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.getenv("SANDBOX_GATEWAY_URL", "http://127.0.0.1:8781").rstrip("/")
TOKEN_FILE = os.getenv(
    "SANDBOX_GATEWAY_TOKEN_FILE",
    os.path.join(HERE, "..", "sandbox-runner", "secrets", "token"),
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'ok' if condition else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def token() -> str:
    with open(os.path.abspath(TOKEN_FILE), encoding="utf-8") as handle:
        return handle.read().strip()


def request(method: str, path: str, payload: dict | None = None, *, audience: str = "sandbox-gateway", bearer: str | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {bearer if bearer is not None else token()}")
    req.add_header("X-Audience", audience)
    req.add_header("X-Request-ID", f"req-{secrets.token_hex(6)}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {}


def error_code(body: dict) -> str:
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str((detail.get("error") or {}).get("code") or "")
    return ""


def main() -> int:
    deployment = "deploy/sandbox-info"
    profile = "profile/dev-l1"

    status, body = request("GET", "/v1/gateway/health")
    check("gateway health", status == 200 and body.get("status") in {"healthy", "up"}, f"status={status}")

    status, body = request("GET", "/v1/gateway/health", bearer="wrong")
    check("bad token rejected", status == 401, f"status={status}")

    status, body = request("GET", "/v1/gateway/health", audience="sandbox-unknown")
    check("audience rejected", status == 403, f"status={status}")

    status, body = request(
        "POST", "/v1/gateway/attach", {"deployment_ref": deployment, "profile_ref": profile}
    )
    with open(os.path.join(HERE, "deployments.json"), encoding="utf-8") as handle:
        reviewed = json.load(handle)["deployments"][deployment]
    attached = (
        status == 200 and body.get("attached") is True
        and body.get("image_digest") == reviewed["image_digest"]
        and body.get("profile_digest") == reviewed["profile_digest"]
    )
    check("attach starts the service via the runner", attached, f"status={status} body={str(body)[:120]}")

    status, body = request("GET", f"/v1/gateway/{deployment}/discover")
    tools = body.get("tools") or []
    names = [str(item.get("name")) for item in tools]
    check(
        "discover returns the allowlisted read-only tool",
        status == 200 and names == ["sandbox_runtime_info"] and tools[0].get("readOnly") is True,
        f"status={status} names={names}",
    )

    status, body = request(
        "POST",
        f"/v1/gateway/{deployment}/invoke",
        {"tool_name": "sandbox_runtime_info", "arguments": {"label": "live"}},
    )
    content = ((body.get("result") or {}).get("content") or [{}])[0].get("text", "")
    check(
        "invoke executes the real stdio MCP tool",
        status == 200 and '"label": "live"' in content and '"network": "none"' in content,
        f"status={status} text={content[:80]}",
    )

    status, body = request(
        "POST",
        f"/v1/gateway/{deployment}/invoke",
        {"tool_name": "not_allowed", "arguments": {}},
    )
    check("unknown tool rejected", status == 403 and error_code(body) == "tool_not_allowed", f"status={status}")

    status, body = request(
        "POST",
        f"/v1/gateway/{deployment}/invoke",
        {"tool_name": "sandbox_runtime_info", "arguments": {"label": "x", "env": "A=B"}},
    )
    check("runtime-control argument rejected", status == 400 and error_code(body) == "forbidden_argument", f"status={status}")

    status, body = request("POST", f"/v1/gateway/{deployment}/disable")
    check("disable stops the service", status == 200 and body.get("disabled") is True, f"status={status}")

    status, body = request("GET", f"/v1/gateway/{deployment}/discover")
    check("detached deployment cannot discover", status == 409 and error_code(body) == "not_attached", f"status={status}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
