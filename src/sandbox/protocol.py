"""Wire-level contracts for the external Sandbox Runner/Gateway APIs.

The HDB process is a control-plane client.  These classes deliberately model
only references, policy snapshots and structured values; they do not expose a
shell command, a container argument list, a host path or a runtime socket.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


class SandboxProtocolError(RuntimeError):
    """A bounded, safe-to-return error from a Gateway/Runner call."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "protocol_error",
        status_code: int = 502,
        retryable: bool = False,
        request_id: str = "",
    ) -> None:
        self.code = _safe_code(code)
        self.status_code = int(status_code)
        self.retryable = bool(retryable)
        self.request_id = str(request_id or "")[:128]
        # Never retain an arbitrary downstream response body in the exception.
        self.message = _safe_message(message)
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": self.request_id,
        }


class SandboxRemoteError(SandboxProtocolError):
    """A remote service returned a structured non-success response."""


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_ARTIFACT_ID_RE = re.compile(r"^art-[A-Za-z0-9_-]{16,64}$")
_FORBIDDEN_PARAMETER_KEYS = frozenset(
    {
        "command",
        "cmd",
        "shell",
        "script",
        "docker_args",
        "volume",
        "volumes",
        "host_path",
        "env",
        "environment",
        "install",
        "pip",
        "npm",
        "secret",
        "token",
        "password",
    }
)


def _safe_code(value: Any) -> str:
    text = str(value or "protocol_error").strip().lower()
    return text if _CODE_RE.fullmatch(text) else "protocol_error"


def _safe_message(value: Any) -> str:
    text = str(value or "request failed").replace("\x00", " ").strip()
    # Newlines and very long upstream messages are not useful in API errors or
    # audit records and can accidentally contain credentials or paths.
    text = " ".join(text.split())
    text = re.sub(
        r"(?i)(token|secret|password|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    # Internal absolute paths and URLs are not actionable to an HDB caller and
    # can disclose host topology or mounted secrets.
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s,;]+)", "<internal-ref>", text)
    return text[:300] or "request failed"


def _required_ref(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a bounded reference")
    return text


def _validate_parameters(value: Any, *, path: str = "parameters") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_PARAMETER_KEYS:
                raise ValueError(f"{path}.{key} is not an allowed Runner parameter")
            _validate_parameters(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_parameters(child, path=f"{path}[{index}]")
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    raise ValueError(f"{path} must be JSON-compatible")


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RequestEnvelope:
    """Identity and authorization snapshot attached to every cross-service call."""

    request_id: str
    actor_id: str
    department_id: str
    policy_snapshot_hash: str
    capability_snapshot: tuple[str, ...] = ()
    capability_snapshot_hash: str = ""
    idempotency_key: str = ""
    deadline: str = ""
    audience: str = "sandbox-runner"

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required_ref(self.request_id, "request_id"))
        object.__setattr__(self, "actor_id", _required_ref(self.actor_id, "actor_id"))
        object.__setattr__(self, "department_id", _required_ref(self.department_id, "department_id"))
        object.__setattr__(
            self,
            "policy_snapshot_hash",
            _required_ref(self.policy_snapshot_hash, "policy_snapshot_hash"),
        )
        object.__setattr__(self, "audience", _required_ref(self.audience, "audience"))
        if self.capability_snapshot_hash:
            digest = str(self.capability_snapshot_hash).strip().lower().removeprefix("sha256:")
            if not _DIGEST_RE.fullmatch(digest):
                raise ValueError("capability_snapshot_hash must be a SHA-256 digest")
            object.__setattr__(
                self,
                "capability_snapshot_hash",
                digest,
            )
        if self.idempotency_key:
            object.__setattr__(
                self, "idempotency_key", _required_ref(self.idempotency_key, "idempotency_key")
            )
        capabilities = tuple(
            sorted({str(item).strip() for item in self.capability_snapshot if str(item).strip()})
        )
        if len(capabilities) > 64 or any(
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9._:-]{0,127}", item) for item in capabilities
        ):
            raise ValueError("capability_snapshot contains an invalid capability")
        object.__setattr__(self, "capability_snapshot", capabilities)

    @classmethod
    def create(
        cls,
        *,
        actor_id: str | int,
        department_id: str | int,
        policy_snapshot_hash: str,
        capability_snapshot: Any = (),
        idempotency_key: str = "",
        deadline: str = "",
        audience: str = "sandbox-runner",
        capability_snapshot_hash: str = "",
        request_id: str = "",
    ) -> "RequestEnvelope":
        return cls(
            request_id=request_id or new_request_id(),
            actor_id=str(actor_id),
            department_id=str(department_id),
            policy_snapshot_hash=str(policy_snapshot_hash),
            capability_snapshot=tuple(capability_snapshot or ()),
            idempotency_key=str(idempotency_key or ""),
            deadline=str(deadline or ""),
            audience=str(audience),
            capability_snapshot_hash=str(capability_snapshot_hash or ""),
        )

    def headers(self) -> dict[str, str]:
        """Return non-secret request headers for an HTTP transport."""

        headers = {
            "X-Request-ID": self.request_id,
            "X-Actor-ID": self.actor_id,
            "X-Department-ID": self.department_id,
            "X-Policy-Snapshot-Hash": self.policy_snapshot_hash,
            "X-Capability-Snapshot": ",".join(self.capability_snapshot),
            "X-Audience": self.audience,
        }
        if self.idempotency_key:
            headers["Idempotency-Key"] = self.idempotency_key
        if self.deadline:
            headers["X-Deadline"] = self.deadline
        if self.capability_snapshot_hash:
            headers["X-Capability-Snapshot-Hash"] = self.capability_snapshot_hash
        return headers

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "actor_id": self.actor_id,
            "department_id": self.department_id,
            "policy_snapshot_hash": self.policy_snapshot_hash,
            "capability_snapshot": list(self.capability_snapshot),
            "idempotency_key": self.idempotency_key,
            "deadline": self.deadline,
            "audience": self.audience,
            "capability_snapshot_hash": self.capability_snapshot_hash,
        }


@dataclass(frozen=True)
class RunnerSubmitRequest:
    envelope: RequestEnvelope
    task_id: str
    bundle_ref: str
    bundle_digest: str
    profile_ref: str
    kind: str = "task"
    network_policy_ref: str = ""
    input_artifact_ids: tuple[str, ...] = ()
    input_artifacts: tuple[Mapping[str, Any], ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    capability_snapshot_hash: str = ""
    runtime_contract_version: str = "v1"
    parent_task_id: str = ""
    resume_from: str = ""
    attempt: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _required_ref(self.task_id, "task_id"))
        object.__setattr__(self, "bundle_ref", _required_ref(self.bundle_ref, "bundle_ref"))
        object.__setattr__(self, "profile_ref", _required_ref(self.profile_ref, "profile_ref"))
        digest = str(self.bundle_digest or "").strip().lower().removeprefix("sha256:")
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError("bundle_digest must be a SHA-256 digest")
        object.__setattr__(self, "bundle_digest", digest)
        request_hash = str(self.capability_snapshot_hash or "").strip().lower().removeprefix("sha256:")
        if request_hash and not _DIGEST_RE.fullmatch(request_hash):
            raise ValueError("capability_snapshot_hash must be a SHA-256 digest")
        envelope_hash = str(self.envelope.capability_snapshot_hash or "").strip().lower()
        if request_hash and envelope_hash and request_hash != envelope_hash:
            raise ValueError("request capability snapshot does not match its envelope")
        if not request_hash and envelope_hash:
            object.__setattr__(self, "capability_snapshot_hash", envelope_hash)
        if str(self.kind or "task") not in {"task", "session", "service"}:
            raise ValueError("kind must be task, session or service")
        artifact_ids = tuple(str(item).strip() for item in self.input_artifact_ids)
        if any(not _ARTIFACT_ID_RE.fullmatch(item) for item in artifact_ids):
            raise ValueError("input_artifact_ids must be opaque artifact IDs")
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("input_artifact_ids must be unique")
        object.__setattr__(self, "input_artifact_ids", artifact_ids)
        items = tuple(self.input_artifacts or ())
        if items:
            if len(items) != len(artifact_ids):
                raise ValueError("every input artifact requires one transfer item")
            normalized_items: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError("input artifact transfer item must be an object")
                payload = dict(item)
                artifact_id = str(payload.get("artifact_id") or "")
                if artifact_id not in artifact_ids:
                    raise ValueError("input artifact transfer item is not referenced by the task")
                content = payload.get("content_base64")
                if not isinstance(content, str) or len(content) > 48 * 1024 * 1024:
                    raise ValueError("input artifact payload is invalid")
                digest = str(payload.get("sha256") or "").strip().lower().removeprefix("sha256:")
                if not _DIGEST_RE.fullmatch(digest):
                    raise ValueError("input artifact digest is invalid")
                normalized_items.append(payload)
            object.__setattr__(self, "input_artifacts", tuple(normalized_items))
        else:
            object.__setattr__(self, "input_artifacts", ())
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or not 1 <= self.attempt <= 1000:
            raise ValueError("attempt must be a bounded positive integer")
        _validate_parameters(self.parameters)
        contract = str(self.runtime_contract_version or "v1").strip()
        if contract != "v1":
            raise ValueError("unsupported runtime_contract_version")
        object.__setattr__(self, "runtime_contract_version", contract)
        for field_name in ("parent_task_id", "resume_from"):
            value = str(getattr(self, field_name) or "").strip()
            if value and not _ID_RE.fullmatch(value):
                raise ValueError(f"{field_name} must be a bounded reference")
            object.__setattr__(self, field_name, value)

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": _required_ref(self.task_id, "task_id"),
            "bundle_ref": _required_ref(self.bundle_ref, "bundle_ref"),
            "bundle_digest": self.bundle_digest,
            "profile_ref": _required_ref(self.profile_ref, "profile_ref"),
            "kind": str(self.kind or "task"),
            "network_policy_ref": str(self.network_policy_ref or ""),
            "input_artifact_ids": list(self.input_artifact_ids),
            "input_artifacts": [dict(item) for item in self.input_artifacts],
            "parameters": dict(self.parameters),
            "capability_snapshot_hash": self.capability_snapshot_hash,
            "runtime_contract_version": self.runtime_contract_version,
            "parent_task_id": self.parent_task_id,
            "resume_from": self.resume_from,
            "attempt": self.attempt,
            "request": self.envelope.to_dict(),
        }


@dataclass(frozen=True)
class RunnerObservation:
    task_id: str
    execution_id: str = ""
    observed_state: str = ""
    status: str = ""
    cleanup_state: str = ""
    output_manifest: Mapping[str, Any] = field(default_factory=dict)
    resource_summary: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    result: Mapping[str, Any] = field(default_factory=dict)
    runtime_events: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunnerObservation":
        return cls(
            task_id=str(value.get("task_id") or ""),
            execution_id=str(value.get("execution_id") or ""),
            observed_state=str(value.get("observed_state") or value.get("state") or ""),
            status=str(value.get("status") or ""),
            cleanup_state=str(value.get("cleanup_state") or ""),
            output_manifest=dict(value.get("output_manifest") or {}),
            resource_summary=dict(value.get("resource_summary") or {}),
            error_code=_safe_code(value.get("error_code") or "") if value.get("error_code") else "",
            result=dict(value.get("result") or value.get("skill_result") or {}),
            runtime_events=tuple(
                item for item in (value.get("runtime_events") or value.get("events") or ())
                if isinstance(item, Mapping)
            ),
        )


def parameter_fingerprint(parameters: Mapping[str, Any]) -> str:
    """Stable digest useful for idempotency/audit without logging values."""

    import json

    payload = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "RequestEnvelope",
    "RunnerObservation",
    "RunnerSubmitRequest",
    "SandboxProtocolError",
    "SandboxRemoteError",
    "new_request_id",
    "parameter_fingerprint",
    "utc_now_iso",
]
