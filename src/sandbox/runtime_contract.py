"""Versioned, domain-neutral contracts emitted by executable Skills."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


RUNTIME_CONTRACT_VERSION = "v1"
RESULT_STATUSES = frozenset(
    {"completed", "needs_user_input", "needs_tool", "validation_failed", "failed"}
)
EVENT_TYPES = frozenset(
    {
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
)
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ARTIFACT_RE = re.compile(r"^art-[A-Za-z0-9_-]{16,64}$")


class RuntimeContractError(ValueError):
    """A Skill result or event violates Runtime Contract v1."""


def _ref(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise RuntimeContractError(f"{field} must be a bounded reference")
    return text


def _optional_ref(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    return _ref(value, field)


def _bounded_text(value: Any, field: str, limit: int = 512) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    if len(text) > limit:
        raise RuntimeContractError(f"{field} exceeds the maximum length")
    return text


def _refs(values: Any, field: str, *, artifact: bool = False) -> tuple[str, ...]:
    if values in (None, ""):
        return ()
    if not isinstance(values, (list, tuple)):
        raise RuntimeContractError(f"{field} must be an array")
    result: list[str] = []
    for value in values:
        text = _ref(value, field)
        if artifact and not _ARTIFACT_RE.fullmatch(text):
            raise RuntimeContractError(f"{field} contains an invalid artifact reference")
        result.append(text)
    if len(set(result)) != len(result):
        raise RuntimeContractError(f"{field} must not contain duplicates")
    return tuple(result)


def _issues(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise RuntimeContractError("issues must be an array")
    result: list[Mapping[str, Any]] = []
    for issue in value:
        if not isinstance(issue, Mapping):
            raise RuntimeContractError("issues must contain objects")
        safe: dict[str, Any] = {}
        for key, item in list(issue.items())[:16]:
            name = _bounded_text(key, "issue field", 80)
            if isinstance(item, (str, int, float, bool)) or item is None:
                safe[name] = _bounded_text(item, name, 1000) if isinstance(item, str) else item
            else:
                raise RuntimeContractError("issue fields must be scalar")
        result.append(safe)
    return tuple(result)


@dataclass(frozen=True)
class SkillResult:
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    result_status: str = "completed"
    error_code: str = ""
    issues: tuple[Mapping[str, Any], ...] = ()
    required_inputs: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    checkpoint_artifact_id: str = ""
    artifact_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_contract_version": self.runtime_contract_version,
            "result_status": self.result_status,
            "error_code": self.error_code,
            "issues": [dict(issue) for issue in self.issues],
            "required_inputs": list(self.required_inputs),
            "required_tools": list(self.required_tools),
            "checkpoint_artifact_id": self.checkpoint_artifact_id,
            "artifact_ids": list(self.artifact_ids),
        }


def parse_skill_result(value: Mapping[str, Any]) -> SkillResult:
    if not isinstance(value, Mapping):
        raise RuntimeContractError("result must be an object")
    version = str(value.get("runtime_contract_version") or RUNTIME_CONTRACT_VERSION)
    if version != RUNTIME_CONTRACT_VERSION:
        raise RuntimeContractError("unsupported runtime_contract_version")
    status = str(value.get("result_status") or "").strip()
    if status not in RESULT_STATUSES:
        raise RuntimeContractError("result_status is invalid")
    error_code = _bounded_text(value.get("error_code"), "error_code", 80)
    checkpoint = _optional_ref(value.get("checkpoint_artifact_id"), "checkpoint_artifact_id")
    if checkpoint and not _ARTIFACT_RE.fullmatch(checkpoint):
        raise RuntimeContractError("checkpoint_artifact_id is invalid")
    return SkillResult(
        runtime_contract_version=version,
        result_status=status,
        error_code=error_code,
        issues=_issues(value.get("issues")),
        required_inputs=_refs(value.get("required_inputs"), "required_inputs"),
        required_tools=_refs(value.get("required_tools"), "required_tools"),
        checkpoint_artifact_id=checkpoint,
        artifact_ids=_refs(value.get("artifact_ids"), "artifact_ids", artifact=True),
    )


@dataclass(frozen=True)
class SkillEvent:
    runtime_contract_version: str
    event_id: str
    seq: int
    task_id: str
    attempt: int
    step_id: str
    event: str
    emitted_at: str
    status: str = ""
    progress: float | None = None
    message: str = ""
    artifact_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    heartbeat: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = {
            "runtime_contract_version": self.runtime_contract_version,
            "event_id": self.event_id,
            "seq": self.seq,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "step_id": self.step_id,
            "event": self.event,
            "emitted_at": self.emitted_at,
            "status": self.status,
            "message": self.message,
            "artifact_ids": list(self.artifact_ids),
            "metadata": dict(self.metadata),
            "heartbeat": self.heartbeat,
        }
        if self.progress is not None:
            result["progress"] = self.progress
        return result


def validate_skill_event(
    value: Mapping[str, Any],
    *,
    task_id: str = "",
    attempt: int | None = None,
    previous_seq: int | None = None,
) -> SkillEvent:
    if not isinstance(value, Mapping):
        raise RuntimeContractError("event must be an object")
    version = str(value.get("runtime_contract_version") or RUNTIME_CONTRACT_VERSION)
    if version != RUNTIME_CONTRACT_VERSION:
        raise RuntimeContractError("unsupported runtime_contract_version")
    event_id = _optional_ref(value.get("event_id"), "event_id") or f"evt-{uuid.uuid4().hex}"
    seq = value.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise RuntimeContractError("seq must be a positive integer")
    if previous_seq is not None and seq <= previous_seq:
        raise RuntimeContractError("event sequence must be strictly increasing")
    actual_task = _ref(value.get("task_id"), "task_id")
    if task_id and actual_task != task_id:
        raise RuntimeContractError("event task_id does not match the task")
    actual_attempt = value.get("attempt", 0)
    if isinstance(actual_attempt, bool) or not isinstance(actual_attempt, int) or actual_attempt < 0:
        raise RuntimeContractError("attempt must be a non-negative integer")
    if attempt is not None and actual_attempt != attempt:
        raise RuntimeContractError("event attempt does not match the task")
    event_type = _bounded_text(value.get("event"), "event", 80)
    if event_type not in EVENT_TYPES:
        raise RuntimeContractError("event type is invalid")
    step_id = _ref(value.get("step_id") or "runtime", "step_id")
    progress = value.get("progress")
    if progress is not None:
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= float(progress) <= 1:
            raise RuntimeContractError("progress must be between 0 and 1")
        progress = float(progress)
    metadata = value.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise RuntimeContractError("metadata must be an object")
    safe_metadata: dict[str, Any] = {}
    for key, item in list(metadata.items())[:32]:
        name = _bounded_text(key, "metadata field", 80)
        if isinstance(item, (str, int, float, bool)) or item is None:
            safe_metadata[name] = _bounded_text(item, name, 1000) if isinstance(item, str) else item
        else:
            raise RuntimeContractError("metadata fields must be scalar")
    return SkillEvent(
        runtime_contract_version=version,
        event_id=event_id,
        seq=seq,
        task_id=actual_task,
        attempt=actual_attempt,
        step_id=step_id,
        event=event_type,
        emitted_at=_bounded_text(value.get("emitted_at") or datetime.now(timezone.utc).isoformat(), "emitted_at", 80),
        status=_bounded_text(value.get("status"), "status", 80),
        progress=progress,
        message=_bounded_text(value.get("message"), "message", 1000),
        artifact_ids=_refs(value.get("artifact_ids"), "artifact_ids", artifact=True),
        metadata=safe_metadata,
        heartbeat=bool(value.get("heartbeat", event_type == "heartbeat")),
    )


__all__ = [
    "EVENT_TYPES",
    "RESULT_STATUSES",
    "RUNTIME_CONTRACT_VERSION",
    "RuntimeContractError",
    "SkillEvent",
    "SkillResult",
    "parse_skill_result",
    "validate_skill_event",
]
