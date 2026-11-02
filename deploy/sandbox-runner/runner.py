"""Independent Sandbox Runner (POC reference implementation).

This service is the **only** component allowed to control an OCI runtime.
HDB and the MCP Gateway call it over authenticated JSON HTTP; neither imports
a container SDK nor accepts shell/command/volume arguments from callers.

Scope of this reference deployment (first phase):

- one reviewed, fixed-digest Python bundle executed with a hardened container
  profile (no network, read-only rootfs, cap-drop ALL, no-new-privileges,
  non-root, PID/memory/CPU limits, no host path mounts beyond the task's own
  read-only input and write-only output directories);
- durable task ledger in SQLite with idempotency, cancellation, timeout and
  orphan reconciliation;
- inline artifact collection with SHA-256 so the HDB Artifact Broker can
  independently stage/scan/commit outputs;
- service profiles for the MCP Gateway: start/stop a fixed read-only stdio MCP
  server container and return its loopback endpoint.

It is deliberately not a general-purpose container API: bundle references,
profiles, images and limits are operator-configured, and unknown references
are rejected fail-closed.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request, Response

HOST = os.getenv("SANDBOX_RUNNER_HOST", "127.0.0.1")
PORT = int(os.getenv("SANDBOX_RUNNER_PORT", "8780"))
TOKEN_FILE = os.getenv(
    "SANDBOX_RUNNER_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets", "token"),
)
GATEWAY_TOKEN_FILE = os.getenv(
    "SANDBOX_RUNNER_GATEWAY_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcp-gateway", "secrets", "runner-token"),
)
BUNDLE_ROOT = os.getenv(
    "SANDBOX_RUNNER_BUNDLE_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundles"),
)
STATE_DIR = os.getenv(
    "SANDBOX_RUNNER_STATE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"),
)
GENERATED_BUNDLE_ROOT = os.getenv(
    "SANDBOX_RUNNER_SKILL_BUNDLE_ROOT", os.path.join(STATE_DIR, "skill-bundles")
)
WORKSPACE_ROOT = os.getenv(
    "SANDBOX_RUNNER_WORKSPACE_ROOT", "/tmp/hdb-sandbox-runner"
)
PROFILE_FILE = os.getenv(
    "SANDBOX_RUNNER_PROFILE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime-policy.dev-l1.yaml"),
)
SERVICE_PROFILES_FILE = os.getenv(
    "SANDBOX_RUNNER_SERVICE_PROFILES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "service-profiles.json"),
)
TASK_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_RUNNER_TASK_TIMEOUT_SECONDS", "120"))
MAX_OUTPUT_BYTES = int(os.getenv("SANDBOX_RUNNER_MAX_OUTPUT_BYTES", str(4 * 1024 * 1024)))
MAX_PARAMETER_BYTES = int(os.getenv("SANDBOX_RUNNER_MAX_PARAMETER_BYTES", "65536"))
MAX_INPUT_BYTES = int(os.getenv("SANDBOX_RUNNER_MAX_INPUT_BYTES", str(32 * 1024 * 1024)))
MAX_INPUT_ARTIFACTS = int(os.getenv("SANDBOX_RUNNER_MAX_INPUT_ARTIFACTS", "16"))
MAX_EVENT_BYTES = int(os.getenv("SANDBOX_RUNNER_MAX_EVENT_BYTES", str(1024 * 1024)))
MAX_EVENTS = int(os.getenv("SANDBOX_RUNNER_MAX_EVENTS", "500"))
MAX_RESULT_BYTES = int(os.getenv("SANDBOX_RUNNER_MAX_RESULT_BYTES", str(256 * 1024)))
MAX_STDOUT_BYTES = max(0, int(os.getenv("SANDBOX_RUNNER_MAX_STDOUT_BYTES", str(64 * 1024))))
MAX_WORKERS = max(1, min(int(os.getenv("SANDBOX_RUNNER_MAX_WORKERS", "2")), 16))
MAX_QUEUE = max(MAX_WORKERS, min(int(os.getenv("SANDBOX_RUNNER_MAX_QUEUE", "32")), 256))
PAYLOAD_RETENTION_SECONDS = max(
    60, int(os.getenv("SANDBOX_RUNNER_PAYLOAD_RETENTION_SECONDS", "3600"))
)
CONTAINER_LABEL = os.getenv("SANDBOX_RUNNER_CONTAINER_LABEL", "hdb.sandbox.runner=1")
SERVICE_NETWORK = os.getenv("SANDBOX_RUNNER_SERVICE_NETWORK", "hdb-sandbox-internal")

_RUNTIME_CONTRACT_VERSION = "v1"
_RESULT_STATUSES = {
    "completed",
    "needs_user_input",
    "needs_tool",
    "validation_failed",
    "failed",
}
_EVENT_TYPES = {
    "step_started",
    "step_progress",
    "step_completed",
    "step_failed",
    "checkpoint_created",
    "validation_issue",
    "needs_user_input",
    "needs_tool",
    "heartbeat",
}
_OUTPUT_EXTENSIONS = {
    "txt",
    "json",
    "csv",
    "md",
    "svg",
    "png",
    "pdf",
    "xlsx",
    "docx",
    "kicad_sch",
    "kicad_pcb",
    "kicad_pro",
    "zip",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_TASK_ID_RE = re.compile(r"^task-[A-Za-z0-9_-]{16,64}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCK_LINE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}==[A-Za-z0-9][A-Za-z0-9_.!+-]{0,99}"
    r"(?:[ \t]+--hash=sha256:[0-9a-fA-F]{64}){1,4}$"
)
_ARTIFACT_ID_RE = re.compile(r"^art-[A-Za-z0-9_-]{16,64}$")
_MEDIA_TYPES = {
    "txt": "text/plain",
    "json": "application/json",
    "csv": "text/csv",
    "md": "text/markdown",
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "kicad_sch": "application/x-kicad-schematic",
    "kicad_pcb": "application/x-kicad-pcb",
    "kicad_pro": "application/x-kicad-project",
    "zip": "application/zip",
}

_lock = threading.Lock()
_build_lock = threading.Lock()
_processes: dict[str, subprocess.Popen[bytes]] = {}
_active_executions: set[str] = set()
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_digest(
    files: Any,
    *,
    entrypoint_digest: str,
    dependency_index_digest: str,
    profile_digest: str,
) -> str:
    """Canonical identity over every source file, dependency lock and profile."""

    normalized = sorted(
        (
            {"path": str(item.get("path") or ""), "sha256": str(item.get("sha256") or "").lower()}
            for item in (files or [])
            if isinstance(item, Mapping)
        ),
        key=lambda item: item["path"],
    )
    payload = {
        "source_manifest": normalized,
        "entrypoint_digest": entrypoint_digest,
        "dependency_index_digest": dependency_index_digest,
        "profile_digest": profile_digest,
    }
    return _sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _executor_pool() -> ThreadPoolExecutor:
    """Lazily create the bounded execution pool (never during import)."""

    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=MAX_WORKERS, thread_name_prefix="hdb-sandbox-task"
            )
        return _executor


def _shutdown_executor() -> None:
    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=False)


def _safe_relative_output_name(relative: str) -> str:
    """Flatten a reviewed relative output path into one broker-safe basename."""

    parts = [part for part in str(relative or "").replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise _safe_error("artifact_path_invalid", "output path is invalid", status=502)
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9._\u4e00-\u9fff-]{0,119}", part):
            raise _safe_error("artifact_path_invalid", "output path contains an unsafe segment", status=502)
    extension = parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
    if extension not in _OUTPUT_EXTENSIONS:
        raise _safe_error("artifact_extension_denied", "output file type is not allowed", status=502)
    stem = "__".join(parts)
    if len(stem) > 120:
        stem = f"{stem[:100]}-{_sha256(relative.encode('utf-8'))[:8]}.{extension}" if extension else stem[:120]
    return stem


def _decode_input_artifacts(body: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate inline input artifacts forwarded by HDB for this task.

    The reference deployment keeps its containers offline and lets the trusted
    Runner host stage the bytes.  Production can replace this with a one-time
    Broker pull without changing the container contract.
    """

    raw_ids = body.get("input_artifact_ids") or []
    raw_items = body.get("input_artifacts") or []
    if not isinstance(raw_ids, (list, tuple)) or len(raw_ids) > MAX_INPUT_ARTIFACTS:
        raise _safe_error("invalid_input", "input_artifact_ids must be a bounded array")
    if not isinstance(raw_items, (list, tuple)) or len(raw_items) != len(raw_ids):
        raise _safe_error("invalid_input", "every input artifact requires exactly one transfer item")
    ids = [_require_ref(item, "input_artifact_id") for item in raw_ids]
    if len(set(ids)) != len(ids):
        raise _safe_error("invalid_input", "input artifact IDs must be unique")
    total = 0
    decoded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw, expected_id in zip(raw_items, ids):
        if not isinstance(raw, Mapping):
            raise _safe_error("invalid_input", "input artifact item must be an object")
        artifact_id = str(raw.get("artifact_id") or "").strip()
        if artifact_id != expected_id or not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise _safe_error("invalid_input", "input artifact identity does not match its reference")
        if artifact_id in seen:
            raise _safe_error("invalid_input", "duplicate input artifact")
        seen.add(artifact_id)
        filename = str(raw.get("filename") or "").strip()
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9._\u4e00-\u9fff-]{0,119}", filename)
        ):
            raise _safe_error("invalid_input", "input artifact filename is invalid")
        encoded = raw.get("content_base64")
        if not isinstance(encoded, str) or len(encoded) > (MAX_INPUT_BYTES * 4 // 3) + 8:
            raise _safe_error("input_too_large", "input artifact payload is too large")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise _safe_error("invalid_input", "input artifact payload is not base64") from exc
        digest = str(raw.get("sha256") or "").strip().lower().removeprefix("sha256:")
        if not _DIGEST_RE.fullmatch(digest) or _sha256(content) != digest:
            raise _safe_error("input_digest_mismatch", "input artifact digest does not match its content")
        total += len(content)
        if total > MAX_INPUT_BYTES:
            raise _safe_error("input_too_large", "input artifact set exceeds the cap")
        decoded.append(
            {
                "artifact_id": artifact_id,
                "filename": filename,
                "sha256": digest,
                "size": len(content),
                "content": content,
            }
        )
    return decoded, [
        {"artifact_id": item["artifact_id"], "sha256": item["sha256"], "size": item["size"]}
        for item in decoded
    ]


def _resolve_result_artifacts(
    result: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve Skill-declared relative output paths into Runner Artifact IDs.

    A Skill cannot know the opaque IDs before collection.  It may therefore
    declare ``artifact_paths`` and ``checkpoint_path`` using output-relative
    paths; the Runner resolves them after collection and only the resolved
    opaque IDs leave the Runner.
    """

    resolved = dict(result)
    by_path = {
        str(item.get("relative_path") or ""): str(item.get("artifact_id"))
        for item in (manifest.get("items") or [])
        if isinstance(item, Mapping) and item.get("relative_path") and item.get("artifact_id")
    }
    raw_paths = resolved.get("artifact_paths") or []
    if raw_paths not in (None, ""):
        if not isinstance(raw_paths, (list, tuple)) or len(raw_paths) > 64:
            raise _safe_error("runtime_result_invalid", "artifact_paths must be a bounded array", status=502)
        ids: list[str] = []
        for path in raw_paths:
            key = str(path or "")
            if key not in by_path:
                raise _safe_error("runtime_result_invalid", "artifact_path is not a collected output", status=502)
            ids.append(by_path[key])
        resolved["artifact_ids"] = ids
    checkpoint_path = str(resolved.get("checkpoint_path") or "").strip()
    if checkpoint_path:
        if checkpoint_path not in by_path:
            raise _safe_error("runtime_result_invalid", "checkpoint_path is not a collected output", status=502)
        resolved["checkpoint_artifact_id"] = by_path[checkpoint_path]
    return resolved


def _read_runtime_result(workspace: str) -> dict[str, Any]:
    path = os.path.join(workspace, "out", "result.json")
    if not os.path.isfile(path) or os.path.islink(path):
        return {}
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_RESULT_BYTES + 1)
    except OSError as exc:
        raise _safe_error("runtime_result_invalid", "result.json is not readable", status=502) from exc
    if len(raw) > MAX_RESULT_BYTES:
        raise _safe_error("runtime_result_invalid", "result.json exceeds the size limit", status=502)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise _safe_error("runtime_result_invalid", "result.json is not valid JSON", status=502) from exc
    if not isinstance(value, Mapping):
        raise _safe_error("runtime_result_invalid", "result.json must be an object", status=502)
    version = str(value.get("runtime_contract_version") or "")
    if version != _RUNTIME_CONTRACT_VERSION:
        raise _safe_error("runtime_result_invalid", "unsupported runtime contract version", status=502)
    if str(value.get("result_status") or "") not in _RESULT_STATUSES:
        raise _safe_error("runtime_result_invalid", "result_status is invalid", status=502)
    return dict(value)


def _sync_runtime_events(task_id: str, workspace: str, attempt: int) -> None:
    """Best-effort live projection while the Skill process is still running."""

    path = os.path.join(workspace, "events", "events.jsonl")
    if not os.path.isfile(path) or os.path.islink(path):
        return
    try:
        if os.path.getsize(path) > MAX_EVENT_BYTES:
            return
        with open(path, "rb") as handle:
            raw = handle.read(MAX_EVENT_BYTES)
    except OSError:
        return
    events: list[dict[str, Any]] = []
    previous = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            break  # the last line may still be partially written
        if not isinstance(event, Mapping):
            break
        raw_attempt = event.get("attempt", -1)
        try:
            parsed_attempt = int(raw_attempt)
        except (TypeError, ValueError):
            break
        if (
            str(event.get("runtime_contract_version") or "") != _RUNTIME_CONTRACT_VERSION
            or str(event.get("task_id") or "") != task_id
            or parsed_attempt != attempt
            or str(event.get("event") or "") not in _EVENT_TYPES
        ):
            break
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= previous:
            break
        previous = seq
        events.append(dict(event))
        if len(events) >= MAX_EVENTS:
            break
    if events:
        _update_task(task_id, events_json=json.dumps(events, ensure_ascii=False, sort_keys=True))


def _read_runtime_events(workspace: str, *, task_id: str, attempt: int) -> list[dict[str, Any]]:
    path = os.path.join(workspace, "events", "events.jsonl")
    if not os.path.isfile(path) or os.path.islink(path):
        return []
    try:
        size = os.path.getsize(path)
        if size > MAX_EVENT_BYTES:
            raise _safe_error("runtime_event_invalid", "event stream exceeds the size limit", status=502)
        with open(path, "rb") as handle:
            raw = handle.read(MAX_EVENT_BYTES + 1)
    except OSError as exc:
        raise _safe_error("runtime_event_invalid", "event stream is not readable", status=502) from exc
    lines = [line for line in raw.decode("utf-8", errors="strict").splitlines() if line.strip()]
    if len(lines) > MAX_EVENTS:
        raise _safe_error("runtime_event_invalid", "event stream exceeds the event count limit", status=502)
    events: list[dict[str, Any]] = []
    previous = 0
    seen_ids: set[str] = set()
    for line in lines:
        if len(line.encode("utf-8")) > 4096:
            raise _safe_error("runtime_event_invalid", "one runtime event is too large", status=502)
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise _safe_error("runtime_event_invalid", "runtime event is not valid JSON", status=502) from exc
        if not isinstance(event, Mapping):
            raise _safe_error("runtime_event_invalid", "runtime event must be an object", status=502)
        if str(event.get("runtime_contract_version") or "") != _RUNTIME_CONTRACT_VERSION:
            raise _safe_error("runtime_event_invalid", "runtime event contract version is invalid", status=502)
        try:
            parsed_attempt = int(event.get("attempt", -1))
        except (TypeError, ValueError) as exc:
            raise _safe_error("runtime_event_invalid", "runtime event attempt is invalid", status=502) from exc
        if str(event.get("task_id") or "") != task_id or parsed_attempt != attempt:
            raise _safe_error("runtime_event_invalid", "runtime event is not bound to this task", status=502)
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= previous:
            raise _safe_error("runtime_event_invalid", "runtime event sequence is not strictly increasing", status=502)
        previous = seq
        event_id = str(event.get("event_id") or "").strip()
        if not event_id or event_id in seen_ids or len(event_id) > 128:
            raise _safe_error("runtime_event_invalid", "runtime event identity is invalid", status=502)
        seen_ids.add(event_id)
        if str(event.get("event") or "") not in _EVENT_TYPES:
            raise _safe_error("runtime_event_invalid", "runtime event type is invalid", status=502)
        if not str(event.get("step_id") or "").strip():
            raise _safe_error("runtime_event_invalid", "runtime event step_id is required", status=502)
        progress = event.get("progress")
        if progress is not None and (
            isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= float(progress) <= 1
        ):
            raise _safe_error("runtime_event_invalid", "runtime event progress is invalid", status=502)
        events.append(dict(event))
    return events


def _safe_error(code: str, message: str, *, retryable: bool = False, status: int = 400) -> HTTPException:
    safe = " ".join(str(message or "request failed").split())[:300]
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": safe, "retryable": bool(retryable)}},
    )


def _require_ref(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise _safe_error("invalid_reference", f"{field} is not a bounded reference")
    return text


def _require_task_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _TASK_ID_RE.fullmatch(text):
        raise _safe_error("invalid_reference", "task_id is not a safe task identifier")
    return text


def _task_container(task_id: str) -> str:
    return f"hdb-task-{_require_task_id(task_id)}"


def _profile_digest() -> str:
    try:
        with open(PROFILE_FILE, "rb") as handle:
            return _sha256(handle.read())
    except OSError as exc:
        raise _safe_error("profile_unavailable", "runtime policy is unavailable", status=503) from exc


def _reviewed_digest(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if not _IMAGE_DIGEST_RE.fullmatch(digest):
        raise _safe_error("profile_invalid", f"{field} is not a pinned image ID", status=503)
    return digest


def _runtime_security() -> None:
    """Verify that the daemon still supplies this L1 profile's kernel guards."""

    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .SecurityOptions}}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise ValueError("runtime unavailable")
        options = json.loads(result.stdout)
        if not isinstance(options, list) or not any(
            str(item).startswith("name=seccomp,profile=builtin") for item in options
        ) or "name=apparmor" not in options:
            raise ValueError("seccomp or AppArmor disabled")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise _safe_error("runtime_unverified", "runtime security profile could not be verified", status=503) from exc


def _inspect_image(image_digest: str, source_labels: Mapping[str, str]) -> None:
    """Never resolve a mutable tag or pull from a registry at task launch."""

    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_digest], capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            raise ValueError("image unavailable")
        image = json.loads(result.stdout)[0]
        if image.get("Id") != image_digest:
            raise ValueError("image ID mismatch")
        config = image.get("Config") or {}
        if config.get("Volumes"):
            raise ValueError("image declares writable volumes")
        labels = config.get("Labels") or {}
        if any(labels.get(key) != value for key, value in source_labels.items()):
            raise ValueError("source label mismatch")
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        raise _safe_error(
            "image_unverified", "reviewed image digest or source labels could not be verified", status=503
        ) from exc
    _runtime_security()


# ---------------------------------------------------------------------------
# identity


def _read_token(path: str) -> str:
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise _safe_error("identity_unavailable", "service identity is not mounted", status=503) from exc
    if stat.st_mode & 0o077:
        raise _safe_error("identity_insecure", "service token file permissions are too broad", status=503)
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise _safe_error("identity_unavailable", "service identity is unavailable", status=503) from exc
    if not token or len(token) > 8192 or any(ord(char) < 32 for char in token):
        raise _safe_error("identity_invalid", "service identity is invalid", status=503)
    return token


def _authenticate(authorization: str, audience: str, *, expected: str = "sandbox-runner") -> None:
    token = _read_token(GATEWAY_TOKEN_FILE if expected == "sandbox-gateway" else TOKEN_FILE)
    if not authorization.startswith("Bearer "):
        raise _safe_error("unauthenticated", "bearer token is required", status=401)
    if not hmac.compare_digest(authorization[len("Bearer ") :].strip(), token):
        raise _safe_error("unauthenticated", "bearer token is invalid", status=401)
    if audience != expected:
        raise _safe_error("audience_mismatch", "request audience is not accepted", status=403)


# ---------------------------------------------------------------------------
# bundle registry


def _load_bundle(bundle_ref: str) -> dict[str, Any]:
    root = os.path.abspath(GENERATED_BUNDLE_ROOT if bundle_ref.startswith("bundle/skill-") else BUNDLE_ROOT)
    # Directory convention: the last path segment of the reference names the
    # reviewed bundle directory (e.g. ``bundle/report`` -> ``bundles/report``).
    directory = _SAFE_NAME_RE.sub("_", bundle_ref.rsplit("/", 1)[-1])[:64]
    candidate = os.path.abspath(os.path.join(root, directory))
    if not candidate.startswith(root + os.sep) or not os.path.isfile(
        os.path.join(candidate, "entrypoint.py")
    ):
        raise _safe_error("bundle_unknown", "bundle reference is not registered", status=404)
    try:
        with open(os.path.join(candidate, "bundle.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        with open(os.path.join(candidate, "entrypoint.py"), "rb") as handle:
            entrypoint = handle.read()
    except (OSError, ValueError) as exc:
        raise _safe_error("bundle_invalid", "bundle manifest is not readable", status=500) from exc
    digest = _sha256(entrypoint)
    if str(manifest.get("bundle_ref") or "") != bundle_ref:
        raise _safe_error("bundle_invalid", "bundle manifest reference mismatch", status=500)
    source_digest = str(manifest.get("source_digest") or "").strip().lower()
    reviewed_digest = source_digest or digest
    if not _DIGEST_RE.fullmatch(reviewed_digest):
        raise _safe_error("bundle_invalid", "bundle source digest is invalid", status=500)
    if str(manifest.get("entrypoint_digest") or "") != digest:
        raise _safe_error("digest_mismatch", "reviewed bundle source has changed", status=503)
    if source_digest:
        files = list(manifest.get("source_manifest") or [])
        if not files or any(
            not isinstance(item, Mapping)
            or not _DIGEST_RE.fullmatch(str(item.get("sha256") or ""))
            or not str(item.get("path") or "")
            for item in files
        ):
            raise _safe_error("bundle_invalid", "bundle source manifest is invalid", status=500)
        expected = _source_digest(
            files,
            entrypoint_digest=digest,
            dependency_index_digest=str(manifest.get("dependency_index_digest") or ""),
            profile_digest=str(manifest.get("profile_digest") or ""),
        )
        if expected != source_digest:
            raise _safe_error("digest_mismatch", "reviewed bundle source manifest changed", status=503)
    if str(manifest.get("profile_digest") or "") != _profile_digest():
        raise _safe_error("profile_mismatch", "reviewed runtime policy has changed", status=503)
    entrypoint_path = str(manifest.get("entrypoint_path") or "")
    if entrypoint_path != f"/opt/bundle/{directory}/entrypoint.py":
        raise _safe_error("bundle_invalid", "bundle entrypoint path is not reviewed", status=500)
    image_digest = _reviewed_digest(manifest.get("image_digest"), "bundle image_digest")
    _inspect_image(image_digest, {f"org.hdb.bundle.{directory}.sha256": reviewed_digest})
    return {
        "bundle_ref": bundle_ref,
        "profile_ref": str(manifest.get("profile_ref") or ""),
        "digest": reviewed_digest,
        "input_schema": dict(manifest.get("input_schema") or {}),
        "entrypoint_path": entrypoint_path,
        "image_digest": image_digest,
    }


def _validate_skill_archive(raw: bytes, expected_digest: str) -> tuple[dict[str, bytes], str]:
    """Validate an admin package without executing or importing its contents."""
    if len(raw) > 8 * 1024 * 1024:
        raise _safe_error("package_too_large", "Skill package exceeds the archive size limit")
    digest = _sha256(raw)
    if digest != str(expected_digest or "").lower().removeprefix("sha256:"):
        raise _safe_error("package_digest_mismatch", "Skill package digest does not match the reviewed source")
    files: dict[str, bytes] = {}
    total = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as exc:
        raise _safe_error("package_invalid", "Skill package is not a valid ZIP archive") from exc
    try:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = str(info.filename).replace("\\", "/")
            parts = name.split("/")
            if (
                not name or name.startswith("/") or any(part in {"", ".", ".."} for part in parts)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}", name)
                or name.lower().split("/")[-1] in {"dockerfile", ".env", "setup.py", "pyproject.toml", "package.json"}
            ):
                raise _safe_error("package_path_invalid", "Skill package contains a forbidden path")
            if len(files) >= 128 or info.file_size > 4 * 1024 * 1024:
                raise _safe_error("package_limits", "Skill package file limits were exceeded")
            if name in files or ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise _safe_error("package_path_invalid", "Skill package contains a duplicate or symlink")
            suffix = os.path.splitext(name)[1].lower()
            if suffix not in {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".lock"}:
                raise _safe_error("package_file_type", "Skill package file type is not allowed")
            content = archive.read(info)
            total += len(content)
            if total > 16 * 1024 * 1024:
                raise _safe_error("package_limits", "Skill package unpacked size is too large")
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _safe_error("package_encoding", "Skill package files must be UTF-8 text") from exc
            files[name] = content
    finally:
        archive.close()
    if "SKILL.md" not in files or "entrypoint.py" not in files:
        raise _safe_error("package_contract", "Skill package must contain SKILL.md and entrypoint.py")
    return files, digest


def _skill_bundle_ref(skill_ref: str, package_digest: str, source_digest: str = "") -> str:
    safe = _SAFE_NAME_RE.sub("-", skill_ref).strip("-")[:16 if source_digest else 25] or "skill"
    identity = _sha256(skill_ref.encode("utf-8"))[:12]
    suffix = f"-{source_digest[:8]}" if source_digest else ""
    return f"bundle/skill-{safe}-{identity}-{package_digest[:12]}{suffix}"


def _skill_dependency_index(value: Any, lock: bytes) -> str:
    lines = [line.strip() for line in lock.decode("utf-8").splitlines() if line.strip()]
    if len(lock) > 128 * 1024 or len(lines) > 64 or any(not _LOCK_LINE_RE.fullmatch(line) for line in lines):
        raise _safe_error("dependency_lock_invalid", "dependencies require pinned wheel versions and SHA-256 hashes")
    raw = str(value or "").strip()
    if not lines:
        if raw:
            raise _safe_error("dependency_index_unexpected", "a dependency index requires a locked dependency list")
        return ""
    if not raw or len(raw) > 512 or any(ord(ch) < 33 or ord(ch) > 126 for ch in raw):
        raise _safe_error("dependency_index_invalid", "approved dependency index is required")
    parsed = urlsplit(raw)
    allowed = {host.strip().lower() for host in os.getenv("SANDBOX_RUNNER_PIP_ALLOWED_HOSTS", "pypi.org").split(",")}
    if (parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9._~/-]*", parsed.path)
            or ".." in parsed.path or "//" in parsed.path):
        raise _safe_error("dependency_index_invalid", "dependency index host is not approved by the Runner")
    try:
        if parsed.port not in (None, 443):
            raise ValueError("non-default port")
    except ValueError as exc:
        raise _safe_error("dependency_index_invalid", "dependency index port is invalid") from exc
    return raw.rstrip("/") + "/"


def _skill_build_base_allowed(image_digest: str) -> None:
    """Only use a locally reviewed base image; never pull arbitrary images."""
    allowed: set[str] = set()
    try:
        for name in ("report", "slow"):
            with open(os.path.join(BUNDLE_ROOT, name, "bundle.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            value = str(manifest.get("image_digest") or "")
            if _IMAGE_DIGEST_RE.fullmatch(value):
                allowed.add(value)
    except (OSError, ValueError, TypeError):
        pass
    if image_digest not in allowed:
        raise _safe_error("build_base_unapproved", "Skill build base image is not locally reviewed", status=409)


def _build_skill_bundle(body: Mapping[str, Any]) -> dict[str, Any]:
    skill_ref = _require_ref(body.get("skill_ref"), "skill_ref")
    package_digest = str(body.get("package_digest") or "").lower().removeprefix("sha256:")
    if not _DIGEST_RE.fullmatch(package_digest):
        raise _safe_error("invalid_digest", "package_digest must be a SHA-256 digest")
    base_bundle_ref = _require_ref(body.get("base_bundle_ref"), "base_bundle_ref")
    base_bundle = _load_bundle(base_bundle_ref)
    image_digest = _reviewed_digest(base_bundle.get("image_digest"), "base image_digest")
    profile_ref = _require_ref(body.get("profile_ref"), "profile_ref")
    if profile_ref != "profile/dev-l1":
        raise _safe_error("profile_invalid", "Skill build is limited to the reviewed dev-l1 profile", status=409)
    if base_bundle_ref != "bundle/report" or base_bundle["profile_ref"] != profile_ref:
        raise _safe_error("build_base_unapproved", "Skill build base must be the reviewed Python bundle", status=409)
    _skill_build_base_allowed(image_digest)
    base_tag = "hdb-sandbox-bundle:1.0"
    tagged = subprocess.run(
        ["docker", "image", "inspect", base_tag, "--format", "{{.Id}}"],
        capture_output=True, text=True, timeout=30,
    )
    if tagged.returncode or tagged.stdout.strip() != image_digest:
        raise _safe_error("build_base_unapproved", "Skill build base tag does not match the reviewed image", status=409)
    raw = base64.b64decode(str(body.get("package_base64") or ""), validate=True)
    files, _ = _validate_skill_archive(raw, package_digest)
    dependency_index = _skill_dependency_index(body.get("dependency_index_url"), files.get("requirements.lock", b""))
    dependency_index_digest = _sha256(dependency_index.encode("utf-8")) if dependency_index else ""
    entrypoint_digest = _sha256(files["entrypoint.py"])
    profile_digest = _profile_digest()
    source_manifest = [
        {"path": name, "sha256": _sha256(content)} for name, content in sorted(files.items())
    ]
    source_digest = _source_digest(
        source_manifest,
        entrypoint_digest=entrypoint_digest,
        dependency_index_digest=dependency_index_digest,
        profile_digest=profile_digest,
    )
    bundle_ref = _skill_bundle_ref(skill_ref, package_digest, dependency_index_digest)
    directory = _SAFE_NAME_RE.sub("_", bundle_ref.rsplit("/", 1)[-1])[:64]
    root = os.path.abspath(GENERATED_BUNDLE_ROOT)
    os.makedirs(root, mode=0o700, exist_ok=True)
    bundle_dir = os.path.abspath(os.path.join(root, directory))
    if not bundle_dir.startswith(root + os.sep):
        raise _safe_error("invalid_reference", "Skill bundle path is invalid")
    if os.path.exists(bundle_dir):
        bundle = _load_bundle(bundle_ref)
        if bundle["digest"] != source_digest:
            raise _safe_error("bundle_conflict", "Existing Skill bundle has different source", status=409)
        with open(os.path.join(bundle_dir, "bundle.json"), encoding="utf-8") as handle:
            existing = json.load(handle)
        if (existing.get("package_digest") != package_digest
                or existing.get("dependency_index_digest", "") != dependency_index_digest
                or existing.get("input_schema") != dict(body.get("input_schema") or {})
                or existing.get("capabilities") != list(body.get("required_capabilities") or ["sandbox.execute"])):
            raise _safe_error("bundle_conflict", "Existing Skill bundle has different package identity", status=409)
        return {"bundle_ref": bundle_ref, "bundle_digest": bundle["digest"],
                "image_digest": bundle["image_digest"], "profile_ref": profile_ref,
                "package_digest": package_digest}
    build_dir = os.path.join(WORKSPACE_ROOT, f"build-{directory}-{uuid.uuid4().hex[:8]}")
    os.makedirs(build_dir, mode=0o700, exist_ok=True)
    try:
        for name, content in files.items():
            target = os.path.abspath(os.path.join(build_dir, name))
            if not target.startswith(os.path.abspath(build_dir) + os.sep):
                raise _safe_error("package_path_invalid", "Skill package path escaped build context")
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(content)
            os.chmod(target, 0o600)
        image_root = f"/opt/bundle/{directory}"
        copy_lines = []
        for name in sorted(files):
            # BuildKit applies --chmod to newly created parent directories as
            # well, so they must remain traversable by the non-root task UID.
            copy_lines.append(f"COPY --chmod=0555 {name} {image_root}/{name}")
        install_stage = ""
        dependency_copy = ""
        if dependency_index:
            pip_args = [
                "/usr/local/bin/python3", "-m", "pip", "--isolated", "install", "--no-cache-dir",
                "--only-binary=:all:", "--require-hashes", "--no-deps",
                "--target", "/opt/skill-build/packages", "--index-url", dependency_index,
                "-r", "/opt/skill-build/requirements.lock",
            ]
            install_stage = (
                f"FROM {base_tag} AS skill_deps\n"
                "COPY --chmod=0777 requirements.lock /opt/skill-build/requirements.lock\n"
                "ENV PIP_DISABLE_PIP_VERSION_CHECK=1 TMPDIR=/opt/skill-build\n"
                f"RUN {json.dumps(pip_args, separators=(',', ':'))}\n"
            )
            dependency_copy = (
                "COPY --from=skill_deps --chmod=0555 /opt/skill-build/packages/ /opt/skill-deps/\n"
                "ENV PYTHONPATH=/opt/skill-deps\n"
            )
        dockerfile = (
            install_stage + f"FROM {base_tag}\n"
            f"LABEL org.hdb.bundle.{directory}.sha256=\"{source_digest}\"\n"
            + dependency_copy
            + "\n".join(copy_lines)
            + "\nUSER 65534:65534\nWORKDIR /work\nENTRYPOINT [\"/usr/local/bin/python3\"]\n"
        )
        with open(os.path.join(build_dir, "Dockerfile"), "w", encoding="utf-8") as handle:
            handle.write(dockerfile)
        tag = f"hdb-skill-build:{directory}-{package_digest[:12]}"
        # Build containers get the same class of CPU/memory bounds as task
        # containers; the build daemon is shared, so an oversized wheel set
        # must not be able to exhaust the host.
        built = subprocess.run(
            ["docker", "build", "--pull=false", "--network", "default" if dependency_index else "none",
             "--resource", "memory=1g", "--resource", "cpu-quota=100000",
             "--tag", tag, build_dir],
            capture_output=True, text=True, timeout=600 if dependency_index else 300,
        )
        if built.returncode:
            raise _safe_error("skill_build_failed", "fixed Skill image build failed", status=502)
        tagged = subprocess.run(
            ["docker", "image", "inspect", base_tag, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30,
        )
        if tagged.returncode or tagged.stdout.strip() != image_digest:
            raise _safe_error("build_base_unapproved", "Skill build base changed during build", status=409)
        inspected = subprocess.run(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30,
        )
        built_image = inspected.stdout.strip()
        if inspected.returncode or not _IMAGE_DIGEST_RE.fullmatch(built_image):
            raise _safe_error("skill_build_unverified", "built Skill image identity could not be verified", status=503)
        manifest = {
            "bundle_ref": bundle_ref,
            "profile_ref": profile_ref,
            "profile_digest": profile_digest,
            "image_digest": built_image,
            "entrypoint_digest": entrypoint_digest,
            "source_digest": source_digest,
            "source_manifest": source_manifest,
            "package_digest": package_digest,
            "dependency_index_digest": dependency_index_digest,
            "description": f"Administrator-built Skill bundle: {skill_ref}",
            "digest_algorithm": "sha256(source-manifest+dependency-lock+profile)",
            "input_schema": dict(body.get("input_schema") or {}),
            "outputs": ["output artifacts"],
            "capabilities": list(body.get("required_capabilities") or ["sandbox.execute"]),
            "entrypoint_path": f"{image_root}/entrypoint.py",
        }
        # Publish atomically: task admission never sees a partial manifest.
        staged = os.path.join(root, f".{directory}.staging-{uuid.uuid4().hex}")
        os.makedirs(staged, mode=0o700)
        try:
            with open(os.path.join(staged, "entrypoint.py"), "xb") as handle:
                handle.write(files["entrypoint.py"])
            with open(os.path.join(staged, "bundle.json"), "x", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
            os.rename(staged, bundle_dir)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        return {"bundle_ref": bundle_ref, "bundle_digest": source_digest,
                "image_digest": built_image, "profile_ref": profile_ref,
                "package_digest": package_digest}
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def _serialized_skill_build(body: Mapping[str, Any]) -> dict[str, Any]:
    with _build_lock:
        return _build_skill_bundle(body)


def _validate_parameters(parameters: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    if not isinstance(parameters, Mapping):
        raise _safe_error("invalid_parameters", "parameters must be an object")
    required = [str(item) for item in (schema.get("required") or [])]
    for key in required:
        if key not in parameters:
            raise _safe_error("invalid_parameters", f"missing required parameter: {key}")
    properties = dict(schema.get("properties") or {})
    if schema.get("additionalProperties") is False:
        for key in parameters:
            if key not in properties:
                raise _safe_error("invalid_parameters", f"unknown parameter: {key}")
    for key, value in parameters.items():
        rule = properties.get(key)
        if not isinstance(rule, Mapping):
            continue
        expected = str(rule.get("type") or "")
        if expected == "string":
            if not isinstance(value, str):
                raise _safe_error("invalid_parameters", f"{key} must be a string")
            max_length = int(rule.get("maxLength") or 0)
            if max_length and len(value) > max_length:
                raise _safe_error("invalid_parameters", f"{key} exceeds maxLength")
        elif expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise _safe_error("invalid_parameters", f"{key} must be an integer")
            minimum = int(rule.get("minimum") or 0)
            maximum = int(rule.get("maximum") or 0)
            if value < minimum or (maximum and value > maximum):
                raise _safe_error("invalid_parameters", f"{key} is out of range")
        elif expected == "boolean":
            if not isinstance(value, bool):
                raise _safe_error("invalid_parameters", f"{key} must be a boolean")
    payload = json.dumps(dict(parameters), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    if len(payload) > MAX_PARAMETER_BYTES:
        raise _safe_error("invalid_parameters", "parameters are too large")


# ---------------------------------------------------------------------------
# task ledger


def _connect() -> sqlite3.Connection:
    os.makedirs(STATE_DIR, exist_ok=True)
    conn = sqlite3.connect(os.path.join(STATE_DIR, "runner.db"), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runner_tasks (
                task_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL DEFAULT '',
                bundle_ref TEXT NOT NULL,
                bundle_digest TEXT NOT NULL,
                profile_ref TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'task',
                execution_id TEXT NOT NULL DEFAULT '',
                attempt INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued',
                observed_state TEXT NOT NULL DEFAULT '',
                cleanup_state TEXT NOT NULL DEFAULT 'pending',
                manifest_json TEXT NOT NULL DEFAULT '',
                parameters_json TEXT NOT NULL DEFAULT '{}',
                input_manifest_json TEXT NOT NULL DEFAULT '[]',
                result_json TEXT NOT NULL DEFAULT '',
                events_json TEXT NOT NULL DEFAULT '[]',
                error_code TEXT NOT NULL DEFAULT '',
                container_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runner_tasks)")}
        migrations = {
            "request_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "attempt": "INTEGER NOT NULL DEFAULT 1",
            "parameters_json": "TEXT NOT NULL DEFAULT '{}'",
            "input_manifest_json": "TEXT NOT NULL DEFAULT '[]'",
            "result_json": "TEXT NOT NULL DEFAULT ''",
            "events_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for column, ddl in migrations.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE runner_tasks ADD COLUMN {column} {ddl}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runner_tasks_idem_unique "
            "ON runner_tasks(idempotency_key) WHERE idempotency_key <> ''"
        )


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": str(row["task_id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "bundle_ref": str(row["bundle_ref"]),
        "bundle_digest": str(row["bundle_digest"]),
        "profile_ref": str(row["profile_ref"]),
        "request_fingerprint": str(row["request_fingerprint"]),
        "kind": str(row["kind"]),
        "execution_id": str(row["execution_id"]),
        "attempt": int(row["attempt"] or 1) if "attempt" in row.keys() else 1,
        "status": str(row["status"]),
        "observed_state": str(row["observed_state"]),
        "cleanup_state": str(row["cleanup_state"]),
        "manifest": json.loads(row["manifest_json"]) if row["manifest_json"] else {},
        "parameters": json.loads(row["parameters_json"]) if "parameters_json" in row.keys() and row["parameters_json"] else {},
        "input_manifest": json.loads(row["input_manifest_json"]) if "input_manifest_json" in row.keys() and row["input_manifest_json"] else [],
        "result": json.loads(row["result_json"]) if "result_json" in row.keys() and row["result_json"] else {},
        "events": json.loads(row["events_json"]) if "events_json" in row.keys() and row["events_json"] else [],
        "error_code": str(row["error_code"]),
        "container_name": str(row["container_name"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _get_task(task_id: str) -> dict[str, Any] | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM runner_tasks WHERE task_id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def _insert_task(**values: Any) -> None:
    now = _now()
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO runner_tasks (
                task_id, idempotency_key, bundle_ref, bundle_digest, profile_ref, request_fingerprint, kind,
                execution_id, attempt, status, observed_state, cleanup_state, container_name,
                parameters_json, input_manifest_json, result_json, events_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["task_id"],
                values.get("idempotency_key", ""),
                values["bundle_ref"],
                values["bundle_digest"],
                values["profile_ref"],
                values.get("request_fingerprint", ""),
                values.get("kind", "task"),
                values.get("execution_id", ""),
                int(values.get("attempt", 1)),
                values.get("status", "queued"),
                values.get("observed_state", ""),
                values.get("cleanup_state", "pending"),
                values.get("container_name", ""),
                json.dumps(values.get("parameters", {}), ensure_ascii=False, sort_keys=True),
                json.dumps(values.get("input_manifest", []), ensure_ascii=False, sort_keys=True),
                json.dumps(values.get("result", {}), ensure_ascii=False, sort_keys=True),
                json.dumps(values.get("events", []), ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )


def _update_task(
    task_id: str, *, expected_statuses: tuple[str, ...] = (), **values: Any
) -> bool:
    if not values:
        return False
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [_now(), task_id]
    guard = ""
    if expected_statuses:
        guard = f" AND status IN ({', '.join('?' for _ in expected_statuses)})"
        params.extend(expected_statuses)
    with closing(_connect()) as conn:
        updated = conn.execute(
            f"UPDATE runner_tasks SET {assignments}, updated_at = ? WHERE task_id = ?{guard}", params
        )
    return updated.rowcount > 0


def _finalize_cancellation(task_id: str) -> bool:
    current = _get_task(task_id)
    if current is None or current["status"] != "cancel_requested":
        return False
    name = _task_container(task_id)
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
        clean = listed.returncode == 0 and name not in listed.stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        clean = False
    workspace = os.path.join(WORKSPACE_ROOT, task_id)
    if clean:
        shutil.rmtree(workspace, ignore_errors=True)
        clean = not os.path.exists(workspace)
    _update_task(
        task_id,
        expected_statuses=("cancel_requested",),
        status="cancelled" if clean else "cleanup_pending",
        observed_state="cancelled" if clean else "cleanup_pending",
        cleanup_state="complete" if clean else "pending",
        error_code="" if clean else "cancel_cleanup_pending",
    )
    return True


def _reconcile_tasks(task_id: str = "") -> dict[str, Any]:
    """After a restart no in-memory execution owns old running ledger rows."""

    listing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label={CONTAINER_LABEL}", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30,
    )
    if listing.returncode:
        raise _safe_error("runtime_unavailable", "runtime reconciliation failed", status=503)
    listed = set(listing.stdout.split())
    with _lock:
        active = set(_active_executions)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT task_id, status, error_code FROM runner_tasks WHERE status IN "
            "('queued', 'running', 'cancel_requested', 'cleanup_pending', 'unknown')"
        ).fetchall()
    removed: list[str] = []
    for row in rows:
        current_id = str(row["task_id"])
        if task_id and current_id != task_id:
            continue
        if current_id in active:
            continue
        try:
            name = _task_container(current_id)
        except HTTPException:
            continue  # legacy invalid IDs cannot be trusted as host paths
        if name in listed:
            result = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            if result.returncode == 0:
                removed.append(name)
                listed.discard(name)
        workspace = os.path.join(WORKSPACE_ROOT, current_id)
        if name not in listed:
            shutil.rmtree(workspace, ignore_errors=True)
        clean = name not in listed and not os.path.exists(workspace)
        state = (
            "cancelled" if clean and (
                row["status"] == "cancel_requested"
                or row["status"] == "cleanup_pending" and row["error_code"] == "cancel_cleanup_pending"
            )
            else "unknown" if clean else "cleanup_pending"
        )
        _update_task(
            current_id,
            expected_statuses=(str(row["status"]),),
            status=state, observed_state=state,
            cleanup_state="complete" if clean else "pending",
            error_code="restart_reconciled" if clean and state == "unknown"
            else "cancel_cleanup_pending" if not clean and row["status"] == "cancel_requested"
            else str(row["error_code"]) if not clean else "",
        )
    for name in sorted(listed):
        if task_id or name in {_task_container(item) for item in active}:
            continue
        if not (name.startswith("hdb-task-") or name.startswith("hdb-orphan-")):
            continue  # services are never removed by the task reconciler
        result = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        if result.returncode == 0:
            removed.append(name)
    return {"reconciled": True, "removed": removed, "removed_count": len(removed)}


def _observation(task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "execution_id": task.get("execution_id", ""),
        "observed_state": task.get("observed_state", ""),
        "status": task.get("status", ""),
        "cleanup_state": task.get("cleanup_state", ""),
        "output_manifest": task.get("manifest", {}),
        "result": task.get("result", {}),
        "runtime_events": task.get("events", []),
        "error_code": task.get("error_code", ""),
    }


# ---------------------------------------------------------------------------
# runtime execution


def _container_user() -> str:
    """Run the workload as the (non-root) service identity, never as root."""

    if os.geteuid() == 0:
        raise _safe_error(
            "runner_identity_invalid",
            "runner must not run as root; use a dedicated non-root identity",
            status=503,
        )
    return f"{os.geteuid()}:{os.getegid()}"


def _run_container(
    task_id: str,
    workspace: str,
    entrypoint_path: str,
    image_digest: str,
    *,
    timeout: float,
    attempt: int = 1,
) -> tuple[int, bytes]:
    """Run the fixed bundle image; fixed argv, no shell, no caller-controlled flags."""

    container_name = _task_container(task_id)
    argv = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        container_name,
        "--label",
        CONTAINER_LABEL,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        _container_user(),
        "--pids-limit",
        "128",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        # Fixed, non-sensitive execution identity for the Runtime Contract
        # event envelope.  Callers cannot supply arbitrary environment values.
        "--env",
        f"HDB_TASK_ID={task_id}",
        "--env",
        f"HDB_ATTEMPT={int(attempt)}",
        "-v",
        f"{workspace}/in:/work/in:ro",
        "-v",
        f"{workspace}/out:/work/out:rw",
        "-v",
        f"{workspace}/events:/work/events:rw",
        image_digest,
        entrypoint_path,
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with _lock:
        _processes[task_id] = proc

    # ``communicate()`` buffers the complete stream in memory.  A Skill is
    # untrusted and its stdout is only diagnostic data, so consume it on a
    # bounded reader thread while the supervisor waits for the process.  This
    # keeps a chatty process from filling the OS pipe (and avoids turning logs
    # into an unbounded Runner memory allocation).  The fallback preserves the
    # small fake-process interface used by unit tests and older integrations.
    stream = getattr(proc, "stdout", None)
    if stream is None or not callable(getattr(proc, "wait", None)):
        try:
            stdout, _ = proc.communicate(timeout=timeout)
            return proc.returncode or 0, (stdout or b"")[:MAX_STDOUT_BYTES]
        finally:
            with _lock:
                _processes.pop(task_id, None)

    stdout_buffer = bytearray()

    def _drain_stdout() -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            if len(stdout_buffer) < MAX_STDOUT_BYTES:
                remaining = MAX_STDOUT_BYTES - len(stdout_buffer)
                stdout_buffer.extend(chunk[:remaining])

    reader = threading.Thread(
        target=_drain_stdout,
        name=f"hdb-sandbox-stdout-{task_id[-12:]}",
        daemon=True,
    )
    reader.start()

    def _wait_after_stop(wait_timeout: float) -> int:
        try:
            return int(proc.wait(timeout=wait_timeout) or 0)
        except subprocess.TimeoutExpired:
            proc.kill()
            return int(proc.wait(timeout=5) or 0)

    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                returncode = int(
                    proc.wait(timeout=max(0.01, min(0.5, deadline - time.monotonic())))
                    or 0
                )
                reader.join(timeout=5)
                return returncode, bytes(stdout_buffer)
            except subprocess.TimeoutExpired:
                _sync_runtime_events(task_id, workspace, attempt)
                current = _get_task(task_id)
                if current is not None and current["status"] == "cancel_requested":
                    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=30)
                    returncode = _wait_after_stop(5)
                    reader.join(timeout=5)
                    return returncode, bytes(stdout_buffer)
                if time.monotonic() < deadline:
                    continue
                subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=30)
                _wait_after_stop(10)
                reader.join(timeout=5)
                raise
    finally:
        with _lock:
            _processes.pop(task_id, None)


def _payload_root() -> str:
    root = os.path.join(STATE_DIR, "artifact-payloads")
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def _store_payload(task_id: str, artifact_id: str, content: bytes) -> str:
    directory = os.path.join(_payload_root(), task_id)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    target = os.path.join(directory, artifact_id)
    with open(target, "xb") as handle:
        handle.write(content)
    os.chmod(target, 0o600)
    return f"{task_id}/{artifact_id}"


def _payload_bytes(payload_ref: str) -> bytes:
    root = os.path.abspath(_payload_root())
    candidate = os.path.abspath(os.path.join(root, str(payload_ref or "")))
    if not candidate.startswith(root + os.sep) or not os.path.isfile(candidate):
        raise _safe_error("artifact_payload_missing", "artifact payload is no longer retained", status=410)
    with open(candidate, "rb") as handle:
        return handle.read()


def _cleanup_old_payloads() -> None:
    root = _payload_root()
    cutoff = time.time() - PAYLOAD_RETENTION_SECONDS
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _collect_outputs(workspace: str, execution_id: str, task_id: str) -> dict[str, Any]:
    """Recursively collect a bounded output tree without following symlinks."""

    out_dir = os.path.join(workspace, "out")
    items: list[dict[str, Any]] = []
    total = 0
    if os.path.islink(out_dir):
        raise _safe_error("artifact_path_invalid", "output directory is a symlink", status=502)
    if os.path.isdir(out_dir):
        for current, directories, files in os.walk(out_dir, topdown=True, followlinks=False):
            for name in directories:
                if os.path.islink(os.path.join(current, name)):
                    raise _safe_error("artifact_path_invalid", "output tree contains a symlink", status=502)
            directories[:] = sorted(directories)
            for name in sorted(files):
                path = os.path.join(current, name)
                if os.path.islink(path):
                    raise _safe_error("artifact_path_invalid", "output tree contains a symlink", status=502)
                if not os.path.isfile(path):
                    continue
                relative = os.path.relpath(path, out_dir).replace(os.sep, "/")
                safe_name = _safe_relative_output_name(relative)
                with open(path, "rb") as handle:
                    data = handle.read(MAX_OUTPUT_BYTES + 1)
                if len(data) > MAX_OUTPUT_BYTES:
                    raise _safe_error("artifact_too_large", f"output {safe_name} exceeds the cap")
                total += len(data)
                if total > MAX_OUTPUT_BYTES:
                    raise _safe_error("artifact_too_large", "output set exceeds the cap")
                extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
                artifact_id = f"art-{uuid.uuid4().hex[:32]}"
                payload_ref = _store_payload(task_id, artifact_id, data)
                items.append(
                    {
                        "artifact_id": artifact_id,
                        "filename": safe_name,
                        "relative_path": relative[:240],
                        "media_type": _MEDIA_TYPES.get(extension, "application/octet-stream"),
                        "size": len(data),
                        "sha256": _sha256(data),
                        "role": "output",
                        "execution_id": execution_id,
                        "payload_ref": payload_ref,
                    }
                )
                if len(items) > 256:
                    raise _safe_error("artifact_too_many", "output set exceeds the file count cap", status=502)
    return {"items": items}


def _write_input_assets(workspace: str, input_artifacts: list[dict[str, Any]]) -> None:
    assets = os.path.join(workspace, "in", "assets")
    os.makedirs(assets, mode=0o700, exist_ok=True)
    for item in input_artifacts:
        directory = os.path.join(assets, item["artifact_id"])
        os.makedirs(directory, mode=0o700, exist_ok=True)
        target = os.path.join(directory, item["filename"])
        with open(target, "xb") as handle:
            handle.write(item["content"])
        os.chmod(target, 0o400)


def _execute_task(
    task: dict[str, Any],
    bundle: Mapping[str, Any],
    parameters: Mapping[str, Any],
    input_artifacts: list[dict[str, Any]] | None = None,
) -> None:
    task_id = task["task_id"]
    execution_id = str(task.get("execution_id") or f"exec-{uuid.uuid4().hex[:24]}")
    attempt = int(task.get("attempt") or 1)
    inputs = list(input_artifacts or [])
    workspace = os.path.join(WORKSPACE_ROOT, task_id)
    with _lock:
        _active_executions.add(task_id)
    if not _update_task(
        task_id, expected_statuses=("queued",),
        status="running", observed_state="running", execution_id=execution_id,
    ):
        _finalize_cancellation(task_id)
        with _lock:
            _active_executions.discard(task_id)
        return
    manifest: dict[str, Any] = {"items": []}
    result: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    runtime_error = ""
    try:
        shutil.rmtree(workspace, ignore_errors=True)
        os.makedirs(os.path.join(workspace, "in"), mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(workspace, "out"), mode=0o777, exist_ok=True)
        os.makedirs(os.path.join(workspace, "events"), mode=0o777, exist_ok=True)
        with open(os.path.join(workspace, "in", "parameters.json"), "w", encoding="utf-8") as handle:
            json.dump(dict(parameters), handle, ensure_ascii=False, sort_keys=True)
        _write_input_assets(workspace, inputs)
        if _finalize_cancellation(task_id):
            return
        returncode = 0
        try:
            returncode, _stdout = _run_container(
                task_id, workspace, bundle["entrypoint_path"], bundle["image_digest"],
                timeout=float(TASK_TIMEOUT_SECONDS), attempt=attempt,
            )
        except subprocess.TimeoutExpired:
            if _finalize_cancellation(task_id):
                return
            if not _update_task(
                task_id,
                expected_statuses=("running",),
                status="timed_out",
                observed_state="timed_out",
                cleanup_state="complete",
                error_code="timeout",
            ):
                _finalize_cancellation(task_id)
            return
        if _finalize_cancellation(task_id):
            return
        # Preserve whatever the Skill wrote even for a non-zero exit: a
        # validation report is often the most valuable output of a failure.
        try:
            manifest = _collect_outputs(workspace, execution_id, task_id)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, Mapping) else {}
            runtime_error = str((detail.get("error") or {}).get("code") or "artifact_invalid")[:64]
        try:
            events = _read_runtime_events(workspace, task_id=task_id, attempt=attempt)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, Mapping) else {}
            runtime_error = runtime_error or str(
                (detail.get("error") or {}).get("code") or "runtime_event_invalid"
            )[:64]
        try:
            result = _read_runtime_result(workspace)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, Mapping) else {}
            runtime_error = runtime_error or str(
                (detail.get("error") or {}).get("code") or "runtime_result_invalid"
            )[:64]
        if result and not runtime_error:
            try:
                result = _resolve_result_artifacts(result, manifest)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, Mapping) else {}
                runtime_error = str((detail.get("error") or {}).get("code") or "runtime_result_invalid")[:64]
        is_skill_bundle = str(bundle.get("bundle_ref") or "").startswith("bundle/skill-")
        result_status = str(result.get("result_status") or "")
        if returncode != 0:
            status, error_code = "failed", "bundle_failed"
        elif runtime_error:
            status, error_code = "failed", runtime_error
        elif is_skill_bundle and not result:
            status, error_code = "failed", "runtime_result_missing"
        elif not manifest.get("items") and result_status not in {"needs_user_input", "needs_tool"}:
            status, error_code = "failed", "artifact_missing"
        else:
            status, error_code = "succeeded", ""
        if not _update_task(
            task_id,
            expected_statuses=("running",),
            status=status,
            observed_state=status,
            cleanup_state="complete",
            error_code=error_code,
            manifest_json=json.dumps(manifest, ensure_ascii=False),
            result_json=json.dumps(result, ensure_ascii=False, sort_keys=True) if result else "",
            events_json=json.dumps(events, ensure_ascii=False, sort_keys=True),
        ):
            _finalize_cancellation(task_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        code = str((detail.get("error") or {}).get("code") or "execution_error")
        if not _update_task(
            task_id,
            expected_statuses=("running",),
            status="failed",
            observed_state="failed",
            cleanup_state="complete",
            error_code=code[:64],
            manifest_json=json.dumps(manifest, ensure_ascii=False),
            result_json=json.dumps(result, ensure_ascii=False, sort_keys=True) if result else "",
            events_json=json.dumps(events, ensure_ascii=False, sort_keys=True),
        ):
            _finalize_cancellation(task_id)
    except Exception:  # noqa: BLE001 - bounded error code only
        if not _update_task(
            task_id,
            expected_statuses=("running",),
            status="failed",
            observed_state="failed",
            cleanup_state="complete",
            error_code="execution_error",
            manifest_json=json.dumps(manifest, ensure_ascii=False),
            result_json=json.dumps(result, ensure_ascii=False, sort_keys=True) if result else "",
            events_json=json.dumps(events, ensure_ascii=False, sort_keys=True),
        ):
            _finalize_cancellation(task_id)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        with _lock:
            _active_executions.discard(task_id)


# ---------------------------------------------------------------------------
# service profiles (MCP Gateway)


def _ensure_service_network() -> None:
    try:
        result = subprocess.run(
            ["docker", "network", "inspect", SERVICE_NETWORK],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            created = subprocess.run(
                ["docker", "network", "create", "--internal", SERVICE_NETWORK],
                capture_output=True, timeout=60,
            )
            if created.returncode:
                raise ValueError("internal network unavailable")
            result = subprocess.run(
                ["docker", "network", "inspect", SERVICE_NETWORK],
                capture_output=True, text=True, timeout=30,
            )
        network = json.loads(result.stdout)[0]
        if result.returncode or network.get("Internal") is not True or network.get("Driver") != "bridge":
            raise ValueError("network policy mismatch")
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        raise _safe_error("network_unverified", "internal service network could not be verified", status=503) from exc


def _read_service_entry(deployment_ref: str) -> dict[str, Any]:
    try:
        with open(SERVICE_PROFILES_FILE, encoding="utf-8") as handle:
            entry = json.load(handle).get(deployment_ref)
    except (OSError, ValueError, AttributeError) as exc:
        raise _safe_error("profile_unavailable", "service catalog is unavailable", status=503) from exc
    if not isinstance(entry, dict):
        raise _safe_error("deployment_unknown", "service is not reviewed by the Runner", status=404)
    return entry


def _service_profile(deployment_ref: str, profile_ref: str, image_digest: str) -> dict[str, Any]:
    entry = _read_service_entry(deployment_ref)
    pinned_image = _reviewed_digest(entry.get("image_digest"), "service image_digest")
    if entry.get("profile_ref") != profile_ref or pinned_image != image_digest:
        raise _safe_error("profile_mismatch", "service profile or image digest mismatch", status=409)
    if entry.get("profile_digest") != _profile_digest():
        raise _safe_error("profile_mismatch", "reviewed runtime policy has changed", status=503)
    labels = entry.get("source_labels")
    if not isinstance(labels, dict) or not labels or not all(
        isinstance(key, str) and isinstance(value, str) and _DIGEST_RE.fullmatch(value)
        for key, value in labels.items()
    ):
        raise _safe_error("profile_invalid", "service source labels are not reviewed", status=503)
    if entry.get("entrypoint") != ["/usr/local/bin/python3", "/opt/mcp/stdio_bridge.py"] or not (
        isinstance(entry.get("environment"), list)
        and all(isinstance(item, str) for item in entry["environment"])
        and "MCP_BRIDGE_PORT=8080" in entry["environment"]
        and "MCP_STDIO_SERVER=/opt/mcp/stdio_server.py" in entry["environment"]
    ):
        raise _safe_error("profile_invalid", "service process configuration is not reviewed", status=503)
    _inspect_image(pinned_image, labels)
    return entry


def _service_ip(container_name: str, image_digest: str, entry: Mapping[str, Any]) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["docker", "inspect", container_name], capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            raise ValueError("service unavailable")
        container = json.loads(result.stdout)[0]
        host = container.get("HostConfig") or {}
        config = container.get("Config") or {}
        networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
        ip = (networks.get(SERVICE_NETWORK) or {}).get("IPAddress") or ""
        container_id = str(container.get("Id") or "")
        if not (
            _DIGEST_RE.fullmatch(container_id)
            and container.get("Name") == f"/{container_name}"
            and container.get("Image") == image_digest
            and container.get("State", {}).get("Running") is True
            and config.get("Image") == image_digest
            and config.get("Entrypoint") == entry["entrypoint"]
            and config.get("Env") == entry["environment"]
            and not config.get("Cmd")
            and config.get("WorkingDir") == "/opt/mcp"
            and (config.get("Labels") or {}).get(CONTAINER_LABEL.partition("=")[0]) == CONTAINER_LABEL.partition("=")[2]
            and host.get("Privileged") is False
            and host.get("NetworkMode") == SERVICE_NETWORK
            and set(networks) == {SERVICE_NETWORK}
            and host.get("ReadonlyRootfs") is True
            and "ALL" in (host.get("CapDrop") or [])
            and "no-new-privileges" in (host.get("SecurityOpt") or [])
            and config.get("User") == _container_user()
            and host.get("PidsLimit") == 128
            and host.get("Memory") == 256 * 1024 * 1024
            and host.get("NanoCpus") == 1_000_000_000
            and (host.get("Tmpfs") or {}).get("/tmp") == "rw,noexec,nosuid,size=16m"
            and not container.get("Mounts")
            and ip
        ):
            raise ValueError("running service does not match reviewed profile")
        return ip, f"svc-{container_id}"
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        raise _safe_error(
            "service_unverified", "running service image or isolation could not be verified", status=503
        ) from exc


def _service_ports(deployment_ref: str, image_digest: str, entry: Mapping[str, Any]) -> tuple[str, str, str]:
    container_name = f"hdb-svc-{_SAFE_NAME_RE.sub('-', deployment_ref)[:32]}"
    _ensure_service_network()
    existing = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=^{container_name}$"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if existing:
        # Never silently reuse a container built from a previous tag or profile.
        return container_name, *_service_ip(container_name, image_digest, entry)
    argv = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--pull",
        "never",
        "--name",
        container_name,
        "--label",
        CONTAINER_LABEL,
        "--network",
        SERVICE_NETWORK,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        _container_user(),
        "--pids-limit",
        "128",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        image_digest,
    ]
    created = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if created.returncode != 0:
        raise _safe_error("service_start_failed", "service container did not start", status=502)
    try:
        return container_name, *_service_ip(container_name, image_digest, entry)
    except Exception:
        # A failed post-start attestation must not leave a running unverified
        # service. Remove by the returned container ID, never by a guessed name.
        launched_id = created.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{64}", launched_id):
            subprocess.run(["docker", "rm", "-f", launched_id], capture_output=True, timeout=30)
        raise


def _service_endpoint(deployment_ref: str, profile_ref: str, image_digest: str) -> dict[str, Any]:
    entry = _service_profile(deployment_ref, profile_ref, image_digest)
    _container, ip, execution_id = _service_ports(deployment_ref, image_digest, entry)
    return {
        "deployment_ref": deployment_ref,
        "profile_ref": profile_ref,
        "profile_digest": _profile_digest(),
        "image_digest": image_digest,
        "endpoint": f"http://{ip}:8080",
        "execution_id": execution_id,
        "cleanup_state": "running",
    }


def _stop_service(deployment_ref: str) -> bool:
    container_name = f"hdb-svc-{_SAFE_NAME_RE.sub('-', deployment_ref)[:32]}"
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", container_name], capture_output=True, text=True, timeout=60
        )
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
        if listed.returncode or container_name in listed.stdout.split():
            raise ValueError("service still exists")
        return result.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise _safe_error("service_stop_unverified", "service shutdown could not be confirmed", status=503) from exc


# ---------------------------------------------------------------------------
# API


app = FastAPI(title="HDB Sandbox Runner (reference)", docs_url=None, redoc_url=None)


def _envelope(request: Request) -> dict[str, str]:
    return {
        "request_id": str(request.headers.get("X-Request-ID") or ""),
        "actor_id": str(request.headers.get("X-Actor-ID") or ""),
        "department_id": str(request.headers.get("X-Department-ID") or ""),
        "audience": str(request.headers.get("X-Audience") or "sandbox-runner"),
    }


@app.on_event("startup")
def _startup() -> None:
    _init_db()
    _reconcile_tasks()
    _cleanup_old_payloads()


@app.on_event("shutdown")
def _shutdown() -> None:
    _shutdown_executor()


@app.get("/v1/health")
def health(authorization: str = Header(default=""), x_audience: str = Header(default="")) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    return {"status": "up", "service": "sandbox-runner", "time": _now()}


@app.post("/v1/tasks", status_code=202)
async def submit_task(
    request: Request,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
    idempotency_key: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    body = await request.json()
    if not isinstance(body, Mapping):
        raise _safe_error("invalid_request", "request body must be an object")
    bundle_ref = _require_ref(body.get("bundle_ref"), "bundle_ref")
    profile_ref = _require_ref(body.get("profile_ref"), "profile_ref")
    bundle_digest = str(body.get("bundle_digest") or "").strip().lower().removeprefix("sha256:")
    if not _DIGEST_RE.fullmatch(bundle_digest):
        raise _safe_error("invalid_digest", "bundle_digest must be a SHA-256 digest")
    kind = str(body.get("kind") or "task")
    if kind != "task":
        raise _safe_error("unsupported_kind", "only task execution is accepted here")
    network_policy = str(body.get("network_policy_ref") or "")
    if network_policy not in {"", "none"}:
        raise _safe_error("network_denied", "this deployment only allows network_policy_ref=none")
    runtime_contract_version = str(body.get("runtime_contract_version") or _RUNTIME_CONTRACT_VERSION)
    if runtime_contract_version != _RUNTIME_CONTRACT_VERSION:
        raise _safe_error("runtime_contract_unsupported", "unsupported runtime contract version")
    parent_task_id = str(body.get("parent_task_id") or "").strip()
    resume_from = str(body.get("resume_from") or "").strip()
    for field_name, value in (("parent_task_id", parent_task_id), ("resume_from", resume_from)):
        if value and not _REF_RE.fullmatch(value):
            raise _safe_error("invalid_reference", f"{field_name} is not a bounded reference")
    attempt = body.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 1000:
        raise _safe_error("invalid_attempt", "attempt must be a bounded positive integer")

    bundle = _load_bundle(bundle_ref)
    if bundle["digest"] != bundle_digest:
        raise _safe_error("digest_mismatch", "bundle digest does not match the reviewed bundle")
    if bundle["profile_ref"] != profile_ref:
        raise _safe_error("profile_mismatch", "profile does not match the reviewed bundle")
    if not isinstance(body.get("parameters") or {}, Mapping):
        raise _safe_error("invalid_parameters", "parameters must be an object")
    parameters = dict(body.get("parameters") or {})
    _validate_parameters(parameters, bundle["input_schema"])
    input_artifacts, input_manifest = _decode_input_artifacts(body)
    if idempotency_key:
        idempotency_key = _require_ref(idempotency_key, "idempotency_key")
    fingerprint = _sha256(json.dumps({
        "bundle_ref": bundle_ref,
        "bundle_digest": bundle_digest,
        "profile_ref": profile_ref,
        "kind": kind,
        "network_policy_ref": network_policy or "none",
        "parameters": parameters,
        "input_artifacts": input_manifest,
        "runtime_contract_version": runtime_contract_version,
        "parent_task_id": parent_task_id,
        "resume_from": resume_from,
        "actor_id": request.headers.get("X-Actor-ID") or "",
        "department_id": request.headers.get("X-Department-ID") or "",
        "policy_snapshot_hash": request.headers.get("X-Policy-Snapshot-Hash") or "",
        "capability_snapshot_hash": body.get("capability_snapshot_hash") or "",
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    requested_id = str(body.get("task_id") or "").strip()
    auto_id = requested_id in {"", "pending"}
    if not auto_id:
        requested_id = _require_task_id(requested_id)
    execution_id = f"exec-{uuid.uuid4().hex[:24]}"

    with _lock:
        if idempotency_key:
            with closing(_connect()) as conn:
                existing = conn.execute(
                    "SELECT task_id FROM runner_tasks WHERE idempotency_key = ? ORDER BY created_at LIMIT 1",
                    (idempotency_key,),
                ).fetchone()
            if existing:
                task = _get_task(str(existing["task_id"]))
                if task is not None:
                    if (not auto_id and str(task["task_id"]) != requested_id) or task["request_fingerprint"] != fingerprint:
                        raise _safe_error(
                            "idempotency_conflict",
                            "idempotency key is already bound to another request",
                            status=409,
                        )
                    return {"task_id": task["task_id"], **_observation(task)}
        if auto_id:
            requested_id = f"task-{uuid.uuid4().hex[:24]}"
        existing_task = _get_task(requested_id)
        if existing_task is not None:
            if existing_task["request_fingerprint"] != fingerprint or existing_task["idempotency_key"] != idempotency_key:
                raise _safe_error("idempotency_conflict", "task_id is bound to another request", status=409)
            return {"task_id": existing_task["task_id"], **_observation(existing_task)}
        with closing(_connect()) as conn:
            queued = int(
                conn.execute(
                    "SELECT COUNT(*) FROM runner_tasks WHERE status IN ('queued', 'running')"
                ).fetchone()[0]
            )
        if queued >= MAX_QUEUE:
            raise _safe_error("queue_full", "Runner execution queue is full", retryable=True, status=429)
        _insert_task(
            task_id=requested_id,
            idempotency_key=idempotency_key,
            bundle_ref=bundle_ref,
            bundle_digest=bundle_digest,
            profile_ref=profile_ref,
            request_fingerprint=fingerprint,
            kind=kind,
            execution_id=execution_id,
            attempt=attempt,
            parameters=parameters,
            input_manifest=input_manifest,
        )
        task = _get_task(requested_id)
        assert task is not None
        # Admission is durable before any OCI work starts.  The bounded worker
        # pool owns execution, so this endpoint never blocks cancel/observe.
        _executor_pool().submit(_execute_task, task, bundle, parameters, input_artifacts)
    return {"task_id": requested_id, **_observation(task)}


@app.post("/v1/skill-build")
async def build_skill(
    request: Request,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    """Build an admin-uploaded Skill using only the Runner's fixed template."""
    _authenticate(authorization, x_audience)
    chunks: list[bytes] = []
    length = 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > 12 * 1024 * 1024:
            raise _safe_error("package_too_large", "Skill build request exceeds the size limit", status=413)
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError) as exc:
        raise _safe_error("invalid_request", "request body must be JSON") from exc
    if not isinstance(body, Mapping):
        raise _safe_error("invalid_request", "request body must be an object")
    try:
        return await asyncio.to_thread(_serialized_skill_build, body)
    except (binascii.Error, ValueError) as exc:
        raise _safe_error("package_invalid", "Skill build request is invalid") from exc


@app.post("/v1/generated-bundles/{bundle_ref:path}/purge")
def purge_generated_bundle(
    bundle_ref: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    """Remove one generated Skill bundle directory and its fixed image.

    Only bundles produced by the Skill build template (``bundle/skill-*``) can
    be purged, and never while an unfinished task still references them.
    """

    _authenticate(authorization, x_audience)
    ref = _require_ref(bundle_ref, "bundle_ref")
    if not ref.startswith("bundle/skill-"):
        raise _safe_error(
            "invalid_reference", "only generated Skill bundles can be purged", status=400
        )
    root = os.path.abspath(GENERATED_BUNDLE_ROOT)
    directory = _SAFE_NAME_RE.sub("_", ref.rsplit("/", 1)[-1])[:64]
    candidate = os.path.abspath(os.path.join(root, directory))
    if not candidate.startswith(root + os.sep) or not os.path.isdir(candidate):
        raise _safe_error("bundle_unknown", "generated bundle is not registered", status=404)
    with closing(_connect()) as conn:
        active = int(
            conn.execute(
                "SELECT COUNT(*) FROM runner_tasks WHERE bundle_ref = ? AND status NOT IN "
                "('succeeded','failed','cancelled','timed_out')",
                (ref,),
            ).fetchone()[0]
        )
    if active:
        raise _safe_error("bundle_in_use", "bundle is referenced by unfinished tasks", status=409)
    try:
        with open(os.path.join(candidate, "bundle.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        image_digest = _reviewed_digest(manifest.get("image_digest"), "image_digest")
    except (OSError, ValueError) as exc:
        raise _safe_error("bundle_invalid", "bundle manifest is not readable", status=500) from exc
    image_removed = False
    if image_digest:
        result = subprocess.run(
            ["docker", "rmi", image_digest], capture_output=True, timeout=60
        )
        image_removed = result.returncode == 0
    try:
        shutil.rmtree(candidate)
    except OSError as exc:
        raise _safe_error(
            "cleanup_pending", "bundle directory could not be removed", status=503
        ) from exc
    return {
        "purged": True,
        "bundle_ref": ref,
        "image_removed": image_removed,
        "cleanup_pending": False,
    }


@app.get("/v1/tasks/{task_id}")
def observe_task(
    task_id: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    task = _get_task(_require_task_id(task_id))
    if task is None:
        raise _safe_error("task_unknown", "task is not known to the runner", status=404)
    return {"observation": _observation(task)}


@app.post("/v1/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    task = _get_task(_require_task_id(task_id))
    if task is None:
        raise _safe_error("task_unknown", "task is not known to the runner", status=404)
    if task["status"] not in {"queued", "running", "cancel_requested"}:
        return {"task_id": task["task_id"], **_observation(task)}
    _update_task(
        task["task_id"], expected_statuses=("queued", "running"),
        status="cancel_requested", observed_state="cancel_requested", cleanup_state="pending",
    )
    container_name = _task_container(task["task_id"])
    subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=30)
    updated = _get_task(task["task_id"])
    assert updated is not None
    return {"task_id": task["task_id"], **_observation(updated)}


def _inline_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve retained Runner payload files into an inline collection result."""

    result: dict[str, Any] = {"items": []}
    for raw in manifest.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        payload_ref = str(item.pop("payload_ref", "") or "")
        if payload_ref:
            item["content_base64"] = base64.b64encode(_payload_bytes(payload_ref)).decode("ascii")
        result["items"].append(item)
    return result


@app.post("/v1/tasks/{task_id}/collect")
def collect_task(
    task_id: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    task = _get_task(_require_task_id(task_id))
    if task is None:
        raise _safe_error("task_unknown", "task is not known to the runner", status=404)
    if task["status"] not in {"succeeded", "failed"}:
        raise _safe_error("collect_unavailable", "task has no collectable output", status=409)
    manifest = task["manifest"] or {}
    if not manifest.get("items") and str((task.get("result") or {}).get("result_status") or "") not in {
        "needs_user_input",
        "needs_tool",
    }:
        raise _safe_error("collect_unavailable", "task has no collectable output", status=409)
    observation = _observation(task)
    observation["output_manifest"] = _inline_manifest(manifest)
    return observation


@app.post("/v1/reconcile")
@app.post("/v1/reconcile/{task_id}")
def reconcile(
    task_id: str = "",
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience)
    return _reconcile_tasks(_require_task_id(task_id) if task_id else "")


@app.post("/v1/services")
async def start_service(
    request: Request,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience, expected="sandbox-gateway")
    body = await request.json()
    if not isinstance(body, Mapping):
        raise _safe_error("invalid_request", "request body must be an object")
    deployment_ref = _require_ref(body.get("deployment_ref"), "deployment_ref")
    profile_ref = _require_ref(body.get("profile_ref"), "profile_ref")
    image_digest = _reviewed_digest(body.get("image_digest"), "image_digest")
    return _service_endpoint(deployment_ref, profile_ref, image_digest)


@app.get("/v1/services/{deployment_ref:path}")
def observe_service(
    deployment_ref: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> dict[str, Any]:
    _authenticate(authorization, x_audience, expected="sandbox-gateway")
    ref = _require_ref(deployment_ref, "deployment_ref")
    entry = _read_service_entry(ref)
    image_digest = _reviewed_digest(entry.get("image_digest"), "image_digest")
    _service_profile(ref, _require_ref(entry.get("profile_ref"), "profile_ref"), image_digest)
    _ensure_service_network()
    name = f"hdb-svc-{_SAFE_NAME_RE.sub('-', ref)[:32]}"
    ip, execution_id = _service_ip(name, image_digest, entry)
    return {
        "deployment_ref": ref,
        "profile_ref": entry["profile_ref"],
        "profile_digest": _profile_digest(),
        "image_digest": image_digest,
        "endpoint": f"http://{ip}:8080",
        "execution_id": execution_id,
    }


@app.delete("/v1/services/{deployment_ref:path}")
def stop_service(
    deployment_ref: str,
    authorization: str = Header(default=""),
    x_audience: str = Header(default=""),
) -> Response:
    _authenticate(authorization, x_audience, expected="sandbox-gateway")
    ref = _require_ref(deployment_ref, "deployment_ref")
    _read_service_entry(ref)
    _stop_service(ref)
    return Response(status_code=204)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
