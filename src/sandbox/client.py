"""Authenticated HTTP clients for the external Runner and MCP Gateway.

This module is deliberately an API client only.  It has no Docker/OCI imports,
does not accept runtime arguments, and never falls back to a local subprocess
when the remote service is unavailable.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

import src.settings as settings

from .models import SandboxTaskSpec
from .policy import SandboxPolicyError, require_execution_enabled, validate_task_spec
from .protocol import (
    RequestEnvelope,
    RunnerObservation,
    RunnerSubmitRequest,
    SandboxProtocolError,
    SandboxRemoteError,
    new_request_id,
)


class SandboxClientConfigurationError(SandboxProtocolError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="client_configuration", status_code=503, retryable=False)


_PATH_RE = re.compile(r"[^A-Za-z0-9._:/-]")


def validate_service_url(value: str, *, allow_insecure: bool = False) -> str:
    """Validate a fixed operator-supplied endpoint, not a user-supplied URL."""

    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise SandboxClientConfigurationError("sandbox service URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SandboxClientConfigurationError("sandbox service URL contains unsupported URL fields")
    if parsed.scheme == "http" and not allow_insecure and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SandboxClientConfigurationError("sandbox service URL must use HTTPS")
    return text


def _path_part(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not text
        or _PATH_RE.search(text)
        or ".." in text
        or "//" in text
        or text.startswith("/")
        or text.endswith("/")
    ):
        raise SandboxProtocolError("invalid remote resource reference", code="invalid_reference", status_code=400)
    return text


class SandboxServiceClient:
    """Shared transport for Runner API and Gateway API calls."""

    def __init__(
        self,
        *,
        runner_url: str | None = None,
        gateway_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        auth_provider: Callable[[str], str] | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        allow_insecure: bool = False,
        require_authentication: bool = True,
        runner_prefix: str = "/v1",
        gateway_prefix: str = "/v1",
    ) -> None:
        configured_runner = runner_url if runner_url is not None else getattr(settings, "SANDBOX_RUNNER_URL", "")
        configured_gateway = gateway_url if gateway_url is not None else getattr(settings, "SANDBOX_GATEWAY_URL", "")
        self.runner_url = validate_service_url(configured_runner, allow_insecure=allow_insecure) if configured_runner else ""
        self.gateway_url = validate_service_url(configured_gateway, allow_insecure=allow_insecure) if configured_gateway else ""
        self.auth_provider = auth_provider
        self.require_authentication = bool(require_authentication)
        self.timeout = timeout or httpx.Timeout(30.0, read=60.0)
        self.runner_prefix = "/" + str(runner_prefix or "v1").strip("/")
        self.gateway_prefix = "/" + str(gateway_prefix or "v1").strip("/")
        # MockTransport is useful for contract tests and does not expose a
        # network fallback.  Async transports are rejected because HDB's agent
        # control path is synchronous and must not accidentally share a loop.
        if isinstance(transport, httpx.AsyncBaseTransport) and not isinstance(transport, httpx.BaseTransport):
            raise SandboxClientConfigurationError("async transports are not supported by SandboxServiceClient")
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": False,
            "trust_env": False,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._http = httpx.Client(**kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SandboxServiceClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _base(self, service: str) -> str:
        base = self.runner_url if service == "runner" else self.gateway_url
        if not base:
            raise SandboxClientConfigurationError(f"{service} endpoint is not configured")
        return base

    def _headers(self, envelope: RequestEnvelope | None, *, service: str) -> dict[str, str]:
        if envelope is None:
            envelope = RequestEnvelope(
                request_id=new_request_id(),
                actor_id="service",
                department_id="system",
                policy_snapshot_hash="system",
                audience=f"sandbox-{service}",
            )
        headers = envelope.headers()
        token = ""
        if self.auth_provider is not None:
            try:
                token = str(self.auth_provider(service) or "").strip()
            except Exception as exc:  # noqa: BLE001 - do not leak provider details
                raise SandboxClientConfigurationError("service identity provider failed") from exc
        if self.require_authentication and not token:
            raise SandboxClientConfigurationError(
                "sandbox service identity is not available; refusing an unauthenticated request"
            )
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        envelope: RequestEnvelope | None = None,
        payload: Mapping[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        base = self._base(service)
        prefix = self.runner_prefix if service == "runner" else self.gateway_prefix
        clean_path = "/" + "/".join(part for part in str(path).split("/") if part)
        url = f"{base}{prefix}{clean_path}"
        try:
            response = self._http.request(
                method.upper(),
                url,
                headers=self._headers(envelope, service=service),
                json=dict(payload or {}),
                timeout=timeout or self.timeout,
            )
        except httpx.TimeoutException as exc:
            request_id = envelope.request_id if envelope else ""
            raise SandboxRemoteError(
                "sandbox service timed out",
                code="timeout",
                status_code=504,
                retryable=True,
                request_id=request_id,
            ) from exc
        except httpx.HTTPError as exc:
            request_id = envelope.request_id if envelope else ""
            raise SandboxRemoteError(
                "sandbox service unavailable",
                code="unavailable",
                status_code=503,
                retryable=True,
                request_id=request_id,
            ) from exc
        request_id = response.headers.get("X-Request-ID", envelope.request_id if envelope else "")
        if response.status_code >= 400:
            code = "remote_error"
            message = "sandbox service rejected the request"
            retryable = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = {}
            if isinstance(body, Mapping):
                detail = body.get("error", body.get("detail", body))
                if isinstance(detail, Mapping):
                    code = str(detail.get("code") or code)
                    # Only use a bounded, explicitly structured message; never
                    # echo arbitrary response text or a traceback.
                    candidate = detail.get("message")
                    if isinstance(candidate, str) and len(candidate) <= 300:
                        message = candidate
                    retryable = bool(detail.get("retryable", retryable))
                elif isinstance(detail, str) and len(detail) <= 160:
                    message = detail
            raise SandboxRemoteError(
                message,
                code=code,
                status_code=response.status_code,
                retryable=retryable,
                request_id=request_id,
            )
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SandboxRemoteError(
                "sandbox service returned an invalid response",
                code="invalid_response",
                status_code=502,
                request_id=request_id,
            ) from exc
        if not isinstance(body, Mapping):
            raise SandboxRemoteError(
                "sandbox service returned an invalid response",
                code="invalid_response",
                status_code=502,
                request_id=request_id,
            )
        return dict(body)

    def submit(
        self,
        request: RunnerSubmitRequest | None = None,
        *,
        spec: SandboxTaskSpec | None = None,
        envelope: RequestEnvelope | None = None,
        task_id: str = "",
    ) -> dict[str, Any]:
        require_execution_enabled()
        if request is None:
            if spec is None:
                raise ValueError("request or spec is required")
            normalized = validate_task_spec(spec)
            request = RunnerSubmitRequest(
                envelope=envelope
                or RequestEnvelope.create(
                    actor_id=normalized.owner_id,
                    department_id=normalized.department_id,
                    policy_snapshot_hash=normalized.policy_snapshot_hash,
                    capability_snapshot=normalized.required_capabilities,
                    capability_snapshot_hash=normalized.capability_snapshot_hash,
                    idempotency_key=normalized.idempotency_key,
                    deadline=normalized.deadline_at,
                ),
                task_id=task_id or "pending",
                bundle_ref=normalized.bundle_ref,
                bundle_digest=normalized.bundle_digest,
                profile_ref=normalized.profile_ref,
                kind=normalized.kind,
                network_policy_ref=normalized.network_policy_ref,
                input_artifact_ids=normalized.input_artifact_ids,
                parameters=normalized.parameters,
                capability_snapshot_hash=normalized.capability_snapshot_hash,
            )
        return self._request(
            "runner",
            "POST",
            "/tasks",
            envelope=request.envelope,
            payload=request.to_payload(),
        )

    def observe(self, task_id: str, *, envelope: RequestEnvelope | None = None) -> RunnerObservation:
        result = self._request("runner", "GET", f"/tasks/{_path_part(task_id)}", envelope=envelope)
        value = result.get("observation", result)
        if not isinstance(value, Mapping):
            raise SandboxRemoteError("invalid observation", code="invalid_response", status_code=502)
        return RunnerObservation.from_dict(value)

    def cancel(self, task_id: str, *, envelope: RequestEnvelope | None = None) -> dict[str, Any]:
        return self._request("runner", "POST", f"/tasks/{_path_part(task_id)}/cancel", envelope=envelope)

    def collect(self, task_id: str, *, envelope: RequestEnvelope | None = None) -> dict[str, Any]:
        return self._request("runner", "POST", f"/tasks/{_path_part(task_id)}/collect", envelope=envelope)

    def skill_build(
        self,
        *,
        skill_ref: str,
        package_digest: str,
        package_bytes: bytes,
        base_bundle_ref: str,
        profile_ref: str,
        runtime_policy_ref: str = "",
        runtime_policy_digest: str = "",
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        required_capabilities: Any = ("sandbox.execute", "sandbox.write_artifact"),
        dependency_index_url: str = "",
        envelope: RequestEnvelope,
    ) -> dict[str, Any]:
        """Build an uploaded Skill through the Runner's fixed build template."""
        if len(package_bytes) > 8 * 1024 * 1024:
            raise SandboxPolicyError("Skill package is too large")
        return self._request(
            "runner", "POST", "/skill-build", envelope=envelope, timeout=httpx.Timeout(660.0),
            payload={
                "skill_ref": _path_part(skill_ref),
                "package_digest": str(package_digest).lower().removeprefix("sha256:"),
                "package_base64": base64.b64encode(package_bytes).decode("ascii"),
                "base_bundle_ref": _path_part(base_bundle_ref),
                "profile_ref": _path_part(profile_ref),
                "runtime_policy_ref": str(runtime_policy_ref or ""),
                "runtime_policy_digest": str(runtime_policy_digest or ""),
                "input_schema": dict(input_schema or {}),
                "output_schema": dict(output_schema or {}),
                "required_capabilities": list(required_capabilities or ()),
                "dependency_index_url": str(dependency_index_url or ""),
            },
        )

    def reconcile(self, task_id: str = "", *, envelope: RequestEnvelope | None = None) -> dict[str, Any]:
        suffix = f"/{_path_part(task_id)}" if task_id else ""
        return self._request("runner", "POST", f"/reconcile{suffix}", envelope=envelope)

    def gateway_health(self, *, envelope: RequestEnvelope | None = None) -> dict[str, Any]:
        return self._request("gateway", "GET", "/gateway/health", envelope=envelope)

    def gateway_attach(
        self,
        *,
        deployment_ref: str,
        profile_ref: str,
        envelope: RequestEnvelope,
    ) -> dict[str, Any]:
        return self._request(
            "gateway",
            "POST",
            "/gateway/attach",
            envelope=envelope,
            payload={"deployment_ref": _path_part(deployment_ref), "profile_ref": _path_part(profile_ref)},
        )

    def gateway_disable(self, deployment_ref: str, *, envelope: RequestEnvelope) -> dict[str, Any]:
        return self._request(
            "gateway", "POST", f"/gateway/{_path_part(deployment_ref)}/disable", envelope=envelope
        )

    def gateway_revoke(self, deployment_ref: str, *, envelope: RequestEnvelope) -> dict[str, Any]:
        return self._request(
            "gateway", "POST", f"/gateway/{_path_part(deployment_ref)}/revoke", envelope=envelope
        )

    def gateway_discover(self, deployment_ref: str, *, envelope: RequestEnvelope) -> dict[str, Any]:
        return self._request(
            "gateway", "GET", f"/gateway/{_path_part(deployment_ref)}/discover", envelope=envelope
        )

    def gateway_invoke(
        self,
        deployment_ref: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        envelope: RequestEnvelope,
    ) -> dict[str, Any]:
        # The caller is expected to have applied the MCP schema/policy.  Keep
        # this client generic but reject obvious runtime-control fields here as
        # a second boundary check.
        forbidden = {"command", "shell", "docker_args", "volume", "volumes", "env", "install"}
        if any(str(key).lower().replace("-", "_") in forbidden for key in arguments):
            raise SandboxPolicyError("gateway invocation contains a forbidden runtime field")
        return self._request(
            "gateway",
            "POST",
            f"/gateway/{_path_part(deployment_ref)}/invoke",
            envelope=envelope,
            payload={"tool_name": _path_part(tool_name), "arguments": dict(arguments)},
        )


SandboxExecutionClient = SandboxServiceClient
RunnerClient = SandboxServiceClient


__all__ = [
    "RunnerClient",
    "SandboxClientConfigurationError",
    "SandboxExecutionClient",
    "SandboxServiceClient",
    "validate_service_url",
]
