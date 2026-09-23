"""Fail-closed admission policy for the HDB sandbox control plane."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import src.settings as settings

from .models import SandboxTaskSpec


class SandboxPolicyError(ValueError):
    """A sandbox request cannot be admitted under the active policy."""


class SandboxDisabledError(SandboxPolicyError):
    """The sandbox kill switch is off."""


_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$", re.IGNORECASE)
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DANGEROUS_KEYS = {
    "command",
    "cmd",
    "shell",
    "script",
    "docker_args",
    "dockerargs",
    "volume",
    "volumes",
    "host_path",
    "hostpath",
    "cwd",
    "env",
    "environment",
    "install",
    "pip",
    "npm",
    "secret",
    "token",
    "password",
    "api_key",
}
_ALLOWED_CAPABILITIES = frozenset(
    {
        "sandbox.execute",
        "sandbox.network",
        "sandbox.internal_data",
        "sandbox.write_artifact",
    }
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def require_execution_enabled(*, operation: str = "sandbox execution") -> None:
    """Reject execution unless an independently deployed Runner is configured."""

    if not bool(getattr(settings, "SANDBOX_ENABLED", False)):
        raise SandboxDisabledError(
            f"{operation} disabled: SANDBOX_ENABLED=false"
        )
    backend = str(getattr(settings, "SANDBOX_BACKEND", "disabled") or "disabled").lower()
    if backend != "runner_api":
        raise SandboxPolicyError(
            "sandbox backend is not validated; SANDBOX_BACKEND must be runner_api"
        )
    if not str(getattr(settings, "SANDBOX_RUNNER_URL", "") or "").strip():
        raise SandboxPolicyError("SANDBOX_RUNNER_URL is required when sandbox is enabled")
    if not str(getattr(settings, "SANDBOX_SERVICE_AUTH_REF", "") or "").strip():
        raise SandboxPolicyError(
            "SANDBOX_SERVICE_AUTH_REF is required when sandbox is enabled"
        )
    if bool(getattr(settings, "SANDBOX_REQUIRE_RUNTIME_ATTESTATION", True)):
        attestation = str(getattr(settings, "SANDBOX_RUNTIME_ATTESTATION_REF", "") or "").strip()
        profile_digest = str(getattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "") or "").strip().lower()
        profile_digest = profile_digest.removeprefix("sha256:")
        if not attestation or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", attestation):
            raise SandboxPolicyError(
                "sandbox runtime attestation is not configured; refusing execution"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", profile_digest):
            raise SandboxPolicyError(
                "sandbox runtime profile digest is not configured; refusing execution"
            )
    if _truthy(getattr(settings, "SANDBOX_ALLOW_DYNAMIC_INSTALL", False)):
        raise SandboxPolicyError("dynamic dependency installation is not supported")


def _check_ref(value: str, field: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or not _REF_RE.fullmatch(text)
        or ".." in text
        or "//" in text
        or any(ord(char) < 32 for char in text)
    ):
        raise SandboxPolicyError(f"{field} must be a bounded reference")
    return text


def _check_parameters(value: Any, *, path: str = "parameters") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _DANGEROUS_KEYS:
                raise SandboxPolicyError(f"{path}.{key} is not an allowed structured parameter")
            _check_parameters(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_parameters(child, path=f"{path}[{index}]")
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    raise SandboxPolicyError(f"{path} contains a non-JSON value")


def _check_deadline(deadline_at: str) -> str:
    text = str(deadline_at or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SandboxPolicyError("deadline_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SandboxPolicyError("deadline_at must include a timezone")
    now = datetime.now(timezone.utc)
    max_deadline = now + timedelta(
        seconds=max(1, int(getattr(settings, "SANDBOX_TASK_TIMEOUT_SECONDS", 300)))
    )
    if parsed <= now:
        raise SandboxPolicyError("deadline_at is already expired")
    if parsed > max_deadline:
        raise SandboxPolicyError("deadline_at exceeds the configured task timeout")
    return parsed.astimezone(timezone.utc).isoformat()


def validate_task_spec(spec: SandboxTaskSpec) -> SandboxTaskSpec:
    """Validate and return a normalized, execution-safe task specification."""

    require_execution_enabled()
    if str(spec.kind or "task") not in {"task", "session"}:
        raise SandboxPolicyError("kind must be task or session")
    idempotency_key = _check_ref(spec.idempotency_key, "idempotency_key")
    skill_ref = _check_ref(spec.skill_ref, "skill_ref") if spec.skill_ref else ""
    skill_digest = str(spec.skill_content_digest or "").lower().removeprefix("sha256:")
    if bool(skill_ref) != bool(skill_digest) or (skill_digest and not re.fullmatch(r"[0-9a-f]{64}", skill_digest)):
        raise SandboxPolicyError("Skill task must bind a reviewed Skill reference and digest")
    owner_id = _check_ref(spec.owner_id, "owner_id")
    department_id = _check_ref(spec.department_id, "department_id")
    bundle_ref = _check_ref(spec.bundle_ref, "bundle_ref")
    profile_ref = _check_ref(spec.profile_ref, "profile_ref")
    if not _DIGEST_RE.fullmatch(str(spec.bundle_digest or "").strip().lower().removeprefix("sha256:")):
        raise SandboxPolicyError("bundle_digest must be a SHA-256 digest")
    policy_hash = _check_ref(spec.policy_snapshot_hash, "policy_snapshot_hash")
    capability_hash = str(getattr(spec, "capability_snapshot_hash", "") or "").strip()
    if capability_hash and not _REF_RE.fullmatch(capability_hash):
        raise SandboxPolicyError("capability_snapshot_hash must be a bounded reference")
    network_ref = str(spec.network_policy_ref or "").strip()
    capabilities = tuple(sorted({str(item).strip() for item in spec.required_capabilities if str(item).strip()}))
    unknown_capabilities = set(capabilities) - _ALLOWED_CAPABILITIES
    if unknown_capabilities:
        raise SandboxPolicyError(
            "unsupported sandbox capabilities: " + ", ".join(sorted(unknown_capabilities))
        )
    if "sandbox.execute" not in capabilities:
        raise SandboxPolicyError("sandbox.execute capability is required")
    if network_ref:
        _check_ref(network_ref, "network_policy_ref")
        if "sandbox.network" not in capabilities:
            raise SandboxPolicyError(
                "a non-empty network policy requires sandbox.network capability"
            )
    if str(getattr(settings, "SANDBOX_DEFAULT_NETWORK", "none")).lower() != "none" and not network_ref:
        raise SandboxPolicyError("non-none default network requires an explicit network policy")

    artifact_ids = tuple(str(item).strip() for item in spec.input_artifact_ids)
    if len(set(artifact_ids)) != len(artifact_ids):
        raise SandboxPolicyError("input_artifact_ids must be unique")
    for artifact_id in artifact_ids:
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise SandboxPolicyError("input_artifact_ids must be opaque IDs")

    _check_parameters(spec.parameters)
    try:
        encoded_parameters = json.dumps(
            spec.parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SandboxPolicyError("parameters must be JSON serializable") from exc
    max_parameters = max(1, int(getattr(settings, "MCP_MAX_ARGUMENT_CHARS", 12000)))
    if len(encoded_parameters) > max_parameters:
        raise SandboxPolicyError("structured parameters exceed the configured limit")

    return SandboxTaskSpec(
        idempotency_key=idempotency_key,
        skill_ref=skill_ref,
        skill_content_digest=skill_digest,
        owner_id=owner_id,
        department_id=department_id,
        bundle_ref=bundle_ref,
        bundle_digest=str(spec.bundle_digest).strip().lower().removeprefix("sha256:"),
        profile_ref=profile_ref,
        policy_snapshot_hash=policy_hash,
        capability_snapshot_hash=capability_hash,
        kind=str(spec.kind or "task"),
        network_policy_ref=network_ref,
        required_capabilities=capabilities,
        input_artifact_ids=artifact_ids,
        parameters=dict(spec.parameters),
        deadline_at=_check_deadline(spec.deadline_at),
    )


__all__ = [
    "SandboxDisabledError",
    "SandboxPolicyError",
    "require_execution_enabled",
    "validate_task_spec",
]
