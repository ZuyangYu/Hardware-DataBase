"""Independent MCP Gateway (POC reference implementation).

The Gateway owns MCP transport/session/routing and service governance, but it
does **not** control the OCI runtime: it asks the Sandbox Runner to start/stop
a reviewed service profile and then talks HTTP to that service's loopback
bridge.  HDB only calls the authenticated endpoints below.

Endpoints (prefix ``/v1``, bearer token + request envelope required):

- ``GET  /gateway/health``
- ``POST /gateway/attach``                         {deployment_ref, profile_ref}
- ``GET  /gateway/{deployment_ref}/discover``
- ``POST /gateway/{deployment_ref}/invoke``        {tool_name, arguments}
- ``POST /gateway/{deployment_ref}/disable``
- ``POST /gateway/{deployment_ref}/revoke``
"""

from __future__ import annotations

import asyncio
import json
import hmac
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from fastapi import FastAPI, Header, HTTPException, Request

HOST = os.getenv("SANDBOX_GATEWAY_HOST", "127.0.0.1")
PORT = int(os.getenv("SANDBOX_GATEWAY_PORT", "8781"))
TOKEN_FILE = os.getenv(
    "SANDBOX_GATEWAY_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sandbox-runner", "secrets", "token"),
)
RUNNER_TOKEN_FILE = os.getenv(
    "SANDBOX_GATEWAY_RUNNER_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets", "runner-token"),
)
DEPLOYMENTS_FILE = os.getenv(
    "SANDBOX_GATEWAY_DEPLOYMENTS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "deployments.json"),
)
RUNNER_URL = os.getenv("SANDBOX_RUNNER_URL", "http://127.0.0.1:8780").rstrip("/")

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROFILE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_ARGUMENTS = {
    "command", "cmd", "shell", "script", "docker_args", "volume", "volumes",
    "host_path", "env", "environment", "install", "pip", "npm", "secret",
    "token", "password",
}

_attached: dict[str, dict[str, Any]] = {}


def _token(path: str = TOKEN_FILE) -> str:
    path = os.path.abspath(path)
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise HTTPException(status_code=503, detail={"error": {"code": "identity_unavailable", "message": "service identity is not mounted", "retryable": False}}) from exc
    if stat.st_mode & 0o077:
        raise HTTPException(status_code=503, detail={"error": {"code": "identity_insecure", "message": "service token file permissions are too broad", "retryable": False}})
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def _safe_error(code: str, message: str, *, status: int = 400, retryable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": " ".join(str(message).split())[:300], "retryable": retryable}},
    )


def _authenticate(authorization: str, audience: str) -> None:
    if not authorization.startswith("Bearer ") or not hmac.compare_digest(
        authorization[len("Bearer ") :].strip(), _token()
    ):
        raise _safe_error("unauthenticated", "bearer token is required", status=401)
    if audience != "sandbox-gateway":
        raise _safe_error("audience_mismatch", "request audience is not accepted", status=403)


def _require_ref(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise _safe_error("invalid_reference", f"{field} is not a bounded reference")
    return text


def _load_deployments() -> dict[str, Any]:
    try:
        with open(DEPLOYMENTS_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise _safe_error("deployments_invalid", "deployment registry is not readable", status=500) from exc
    return dict(data.get("deployments") or {})


def _deployment(deployment_ref: str) -> dict[str, Any]:
    deployments = _load_deployments()
    item = deployments.get(deployment_ref)
    if not isinstance(item, Mapping):
        raise _safe_error("deployment_unknown", "deployment is not registered", status=404)
    deployment = dict(item)
    if not _DIGEST_RE.fullmatch(str(deployment.get("image_digest") or "")) or not _PROFILE_DIGEST_RE.fullmatch(
        str(deployment.get("profile_digest") or "")
    ):
        raise _safe_error("deployment_invalid", "reviewed image or profile digest is missing", status=503)
    return deployment


def _verified_endpoint(deployment_ref: str, deployment: Mapping[str, Any]) -> str:
    attached = _attached.get(deployment_ref)
    if attached is None:
        raise _safe_error("not_attached", "deployment is not attached", status=409)
    observed = _runner_request("GET", f"/v1/services/{deployment_ref}")
    expected = {
        "deployment_ref": deployment_ref,
        "profile_ref": deployment["profile_ref"],
        "profile_digest": deployment["profile_digest"],
        "image_digest": deployment["image_digest"],
        "endpoint": attached["endpoint"],
        "execution_id": attached["execution_id"],
    }
    if any(observed.get(key) != value for key, value in expected.items()):
        _attached.pop(deployment_ref, None)
        raise _safe_error("service_mismatch", "reviewed service instance changed", status=503)
    return str(attached["endpoint"])


def _runner_request(method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(dict(payload)).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{RUNNER_URL}{path}", data=data, method=method)
    request.add_header("Authorization", f"Bearer {_token(RUNNER_TOKEN_FILE)}")
    request.add_header("X-Audience", "sandbox-gateway")
    request.add_header("X-Request-ID", f"gw-{int(time.time() * 1000)}")
    request.add_header("X-Actor-ID", "mcp-gateway")
    request.add_header("X-Department-ID", "system")
    request.add_header("X-Policy-Snapshot-Hash", "gateway")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise _safe_error("runner_rejected", f"runner returned {exc.code}", status=502) from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise _safe_error("runner_unavailable", "runner is unavailable", status=503, retryable=True) from exc


def _bridge_get(endpoint: str, path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{endpoint}{path}", timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except (urllib.error.URLError, ValueError) as exc:
        raise _safe_error("bridge_unavailable", "service bridge is unavailable", status=502) from exc


def _bridge_invoke(endpoint: str, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    data = json.dumps({"name": tool_name, "arguments": dict(arguments)}).encode("utf-8")
    request = urllib.request.Request(f"{endpoint}/invoke", data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise _safe_error("tool_error", f"bridge rejected the call ({exc.code})", status=502) from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise _safe_error("bridge_unavailable", "service bridge is unavailable", status=502) from exc


def _wait_bridge(endpoint: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _bridge_get(endpoint, "/health").get("status") == "up":
                return
        except HTTPException:
            pass
        time.sleep(0.3)
    raise _safe_error("service_unhealthy", "service did not become healthy", status=502)


app = FastAPI(title="HDB MCP Gateway (reference)", docs_url=None, redoc_url=None)


@app.get("/v1/gateway/health")
def gateway_health(
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    # HDB accepts healthy/ok/ready for a passed gateway health test.
    return {"status": "healthy", "service": "mcp-gateway", "attached": sorted(_attached)}


@app.post("/v1/gateway/attach")
async def attach(
    request: Request,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    body = await request.json()
    deployment_ref = _require_ref(body.get("deployment_ref"), "deployment_ref")
    profile_ref = _require_ref(body.get("profile_ref"), "profile_ref")
    deployment = _deployment(deployment_ref)
    if str(deployment.get("profile_ref") or "") != profile_ref:
        raise _safe_error("profile_mismatch", "profile does not match the reviewed deployment")
    started = await asyncio.to_thread(_runner_request,
        "POST", "/v1/services", {
            "deployment_ref": deployment_ref,
            "profile_ref": profile_ref,
            "image_digest": deployment["image_digest"],
        }
    )
    if any(started.get(key) != value for key, value in (
        ("deployment_ref", deployment_ref),
        ("profile_ref", profile_ref),
        ("profile_digest", deployment["profile_digest"]),
        ("image_digest", deployment["image_digest"]),
    )):
        raise _safe_error("service_mismatch", "Runner did not attest the reviewed service", status=503)
    if not re.fullmatch(r"svc-[0-9a-f]{64}", str(started.get("execution_id") or "")):
        raise _safe_error("service_mismatch", "Runner did not attest a stable service identity", status=503)
    endpoint = str(started.get("endpoint") or "")
    if not endpoint:
        raise _safe_error("service_unavailable", "runner did not return an endpoint", status=502)
    await asyncio.to_thread(_wait_bridge, endpoint)
    _attached[deployment_ref] = {"endpoint": endpoint, "execution_id": started.get("execution_id", "")}
    return {
        "deployment_ref": deployment_ref,
        "profile_ref": profile_ref,
        "profile_digest": deployment["profile_digest"],
        "image_digest": deployment["image_digest"],
        "attached": True,
        "endpoint": endpoint,
        "execution_id": started.get("execution_id", ""),
    }


@app.get("/v1/gateway/{deployment_ref:path}/discover")
def discover(
    deployment_ref: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    deployment = _deployment(deployment_ref)
    endpoint = _verified_endpoint(deployment_ref, deployment)
    tools = _bridge_get(endpoint, "/tools").get("tools") or []
    allowlist = dict(deployment.get("tools") or {})
    filtered = []
    for raw in tools:
        name = str(raw.get("name") or "")
        if name not in allowlist:
            continue
        reviewed = dict(allowlist[name])
        filtered.append(
            {
                "name": name,
                "description": str(raw.get("description") or reviewed.get("description") or "")[:2000],
                "inputSchema": dict(reviewed.get("inputSchema") or raw.get("inputSchema") or {}),
                "readOnly": not bool(reviewed.get("write")),
            }
        )
    return {"deployment_ref": deployment_ref, "tools": filtered, "tool_count": len(filtered)}


@app.post("/v1/gateway/{deployment_ref:path}/invoke")
async def invoke(
    deployment_ref: str,
    request: Request,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    deployment = _deployment(deployment_ref)
    endpoint = await asyncio.to_thread(_verified_endpoint, deployment_ref, deployment)
    attached = _attached.get(deployment_ref)
    if attached is None:
        raise _safe_error("not_attached", "deployment is not attached", status=409)
    body = await request.json()
    tool_name = _require_ref(body.get("tool_name"), "tool_name")
    allowlist = dict(deployment.get("tools") or {})
    if tool_name not in allowlist:
        raise _safe_error("tool_not_allowed", "tool is not in the reviewed allowlist", status=403)
    arguments = body.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        raise _safe_error("invalid_arguments", "arguments must be an object")
    for key in arguments:
        if str(key).lower().replace("-", "_") in _FORBIDDEN_ARGUMENTS:
            raise _safe_error("forbidden_argument", "arguments contain a forbidden runtime field")
    result = await asyncio.to_thread(_bridge_invoke, endpoint, tool_name, arguments)
    if _attached.get(deployment_ref) is not attached:
        raise _safe_error("service_detached", "service was disabled during invocation", status=409)
    return {
        "deployment_ref": deployment_ref,
        "tool_name": tool_name,
        "read_only": not bool(dict(allowlist[tool_name]).get("write")),
        "result": result.get("result") or {},
    }


@app.post("/v1/gateway/{deployment_ref:path}/disable")
def disable(
    deployment_ref: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    _deployment(deployment_ref)
    _attached.pop(deployment_ref, None)
    _runner_request("DELETE", f"/v1/services/{deployment_ref}")
    return {"deployment_ref": deployment_ref, "disabled": True}


@app.post("/v1/gateway/{deployment_ref:path}/revoke")
def revoke(
    deployment_ref: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    _deployment(deployment_ref)
    _attached.pop(deployment_ref, None)
    _runner_request("DELETE", f"/v1/services/{deployment_ref}")
    return {"deployment_ref": deployment_ref, "revoked": True}


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
