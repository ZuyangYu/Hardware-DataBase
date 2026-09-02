"""Bounded external-agent executor selection and field harness.

This module is deliberately an adapter boundary.  The document-authoring
runtime owns leases, receipts, events and the business ``HarnessRun``.  The
agent harness selects an executor, derives stable field thread ids, exposes a
small typed tool surface and delegates unfinished work to the governed graph
executor when the agent path is unavailable.

The selector is intentionally fail-closed:

* schema, work-order and requested-executor values must agree;
* an external agent needs an approved, version-matched policy;
* the feature flag and agent infrastructure are independent gates; and
* only those last two availability gates may degrade an external request to
  ``authoring_graph``.

No deepagents default tools are imported here.  In particular, the module
does not make filesystem, shell, command or general-purpose subagent tools
visible merely by constructing an agent harness.  Task 6 can inject a real
agent runner behind the same context/protocol without changing this boundary.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from src.document_authoring.harness.idempotency import (
    agent_thread_id as _stable_agent_thread_id,
    canonical_json,
    execution_event_key,
    receipt_action_key,
)
from src.document_authoring.harness.evidence_registry import (
    EvidenceAccessError,
    validate_evidence_access,
)
from src.document_authoring.models import (
    AuthoringExecutionEvent,
    DocumentUnitDraft,
    DraftAssertion,
    EvidenceRegistryEntry,
    TypedFieldValue,
    content_hash,
)
from src.observability import observe
from src.observability.metrics import record_authoring_agent, record_authoring_tool
from pydantic import BaseModel, ConfigDict, Field


ExecutionMode: TypeAlias = Literal[
    "internal_harness", "deterministic_only", "external_agent",
]
EffectiveExecutor: TypeAlias = Literal[
    "deterministic_rule", "authoring_graph", "agent_field_harness",
]

EXECUTION_MODES = frozenset({
    "internal_harness", "deterministic_only", "external_agent",
})

# These are the only application-owned tools that the future agent may see.
# Todo/list planning is supplied by the agent framework itself and is not an
# application capability, so it is intentionally not part of this allowlist.
AGENT_TOOL_ALLOWLIST = frozenset({
    "read_field_brief",
    "retrieve_evidence",
    "propose_field_value",
    "mark_missing",
})

FORBIDDEN_AGENT_TOOLS = frozenset({
    "filesystem", "file", "files", "shell", "command", "exec", "subprocess",
    "task", "general-purpose", "general_purpose", "general-purpose-subagent",
})
# The persisted HarnessPolicy currently uses these canonical names.  The
# additional aliases above are rejected if explicitly requested, but an old
# policy need not redundantly list every spelling in its excluded profile.
REQUIRED_EXCLUDED_AGENT_CAPABILITIES = frozenset({
    "filesystem", "shell", "command", "task", "general-purpose",
})

REASON_AGENT_MODE_DISABLED = "agent_mode_disabled"
REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE = "agent_infrastructure_unavailable"
REASON_AGENT_TOOLS_NOT_IMPLEMENTED = "agent_tools_not_implemented"
REASON_AGENT_TOOL_BUDGET_EXHAUSTED = "agent_tool_budget_exhausted"
REASON_AGENT_PROPOSAL_BUDGET_EXHAUSTED = "agent_proposal_budget_exhausted"
REASON_AGENT_LEASE_LOST = "agent_lease_lost"

ERROR_FIELD_NOT_REGISTERED = "field_not_registered"
ERROR_VALUE_TYPE_MISMATCH = "value_type_mismatch"
ERROR_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
ERROR_MISSING_POLICY = "missing_policy_unavailable"
ERROR_PROPOSAL_REQUIRES_HUMAN = "proposal_requires_human"
ERROR_AGENT_TOOL_BUDGET = "agent_tool_budget_exhausted"
ERROR_PROPOSAL_BUDGET = "proposal_retry_budget_exhausted"

ERROR_EXECUTOR_MISMATCH = "executor_mismatch"
ERROR_POLICY_REQUIRED = "approved_policy_required"
ERROR_POLICY_NOT_APPROVED = "policy_not_approved"
ERROR_POLICY_EXPIRED = "policy_expired"
ERROR_POLICY_REVOKED = "policy_revoked"
ERROR_POLICY_HASH_MISMATCH = "policy_hash_mismatch"
ERROR_POLICY_BINDING_MISMATCH = "policy_binding_mismatch"
ERROR_AGENT_TOOL_NOT_ALLOWED = "agent_tool_not_allowed"
ERROR_AGENT_PROFILE_INVALID = "agent_profile_invalid"
ERROR_FALLBACK_UNAVAILABLE = "fallback_executor_unavailable"


class _FieldArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(min_length=1, max_length=200)


class _RetrieveEvidenceArgs(_FieldArgs):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)
    retriever_kind: str = Field(default="default", max_length=80)


class _ProposalArgs(_FieldArgs):
    value: Any
    value_type: str = Field(min_length=1, max_length=80)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    note: str = Field(default="", max_length=2000)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class _MissingArgs(_FieldArgs):
    reason: str = Field(min_length=1, max_length=2000)


class HarnessExecutorSelectionError(ValueError):
    """A request cannot be safely assigned to an authoring executor."""

    error_code = ERROR_EXECUTOR_MISMATCH

    def __init__(self, message: str, *, error_code: str = ERROR_EXECUTOR_MISMATCH):
        self.error_code = error_code
        super().__init__(f"{error_code}: {message}")


class HarnessPolicyError(HarnessExecutorSelectionError):
    """A missing, stale, unapproved or unsafe frozen policy is fatal."""

    def __init__(self, message: str, *, error_code: str = ERROR_POLICY_NOT_APPROVED):
        super().__init__(message, error_code=error_code)


class AgentToolNotAllowed(PermissionError):
    """Raised when a tool is outside the frozen agent allowlist."""

    error_code = ERROR_AGENT_TOOL_NOT_ALLOWED

    def __init__(self, tool_name: str):
        self.tool_name = str(tool_name)
        super().__init__(f"{self.error_code}: agent tool is not allowlisted: {tool_name}")


class AgentInfrastructureUnavailable(RuntimeError):
    """An injected agent backend cannot be constructed or resumed."""

    error_code = REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE


class AgentToolsNotImplemented(RuntimeError):
    """The requested agent tool is outside the frozen allowlist."""

    error_code = REASON_AGENT_TOOLS_NOT_IMPLEMENTED


class FallbackExecutorUnavailable(RuntimeError):
    """A selected degraded path has no graph executor to call."""

    error_code = ERROR_FALLBACK_UNAVAILABLE

    def __init__(self, message: str = "authoring_graph fallback executor is unavailable"):
        super().__init__(f"{self.error_code}: {message}")


@dataclass(frozen=True)
class HarnessExecutionContext:
    """Request-scoped capabilities passed across executor boundaries.

    The context contains references owned by the runtime; it is not graph
    state and must not be serialized into a checkpoint.  ``field_ids`` is a
    bounded selection of semantic fields for a field-level fallback.
    """

    work_order: Any
    harness_run: Any
    schema: Any
    policy: Any | None = None
    run_manifest: Any | None = None
    snapshot: Any | None = None
    legacy_claims: tuple[Any, ...] = ()
    writer: Any | None = None
    retrieve: Callable[..., Any] | None = None
    checkpointer: Any | None = None
    field_ids: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def run(self) -> Any:
        """Compatibility alias used by small executor adapters and tests."""

        return self.harness_run

    @property
    def manifest(self) -> Any | None:
        return self.run_manifest

    def with_fields(self, field_ids: Sequence[str]) -> "HarnessExecutionContext":
        return replace(self, field_ids=tuple(str(value) for value in field_ids))

    def as_kwargs(self, *, include_field_ids: bool = True) -> dict[str, Any]:
        """Return graph/runtime keyword arguments without losing aliases."""

        values: dict[str, Any] = {
            "work_order": self.work_order,
            "harness_run": self.harness_run,
            "run": self.harness_run,
            "run_manifest": self.run_manifest,
            "manifest": self.run_manifest,
            "policy": self.policy,
            "schema": self.schema,
            "snapshot": self.snapshot,
            "legacy_claims": list(self.legacy_claims),
            "writer": self.writer,
            "retrieve": self.retrieve,
            "checkpointer": self.checkpointer,
        }
        if include_field_ids:
            values["field_ids"] = list(self.field_ids)
        values.update(dict(self.extra))
        return values


@runtime_checkable
class HarnessExecutor(Protocol):
    """Common runtime boundary for graph and agent executors.

    Implementations accept either the typed context or keyword arguments for
    compatibility with ``AuthoringGraph.run`` and the existing runtime call
    shape.  The protocol intentionally exposes no store or model API.
    """

    effective_executor: EffectiveExecutor

    def execute(
        self,
        context: HarnessExecutionContext | None = None,
        **kwargs: Any,
    ) -> Any:
        ...


def agent_thread_id(harness_run_id: str, field_id: str) -> str:
    """Return the stable per-field SHA-256 agent thread id."""

    return _stable_agent_thread_id(str(harness_run_id), str(field_id))


def build_agent_thread_id(harness_run_id: str, field_id: str) -> str:
    """Named alias for callers that prefer a builder verb."""

    return agent_thread_id(harness_run_id, field_id)


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


_MISSING = object()


def _explicit_attribute(source: Any, key: str) -> Any:
    """Read an attribute without triggering dynamic ``__getattr__`` hooks."""

    if isinstance(source, Mapping):
        return source.get(key, _MISSING)
    namespace = getattr(source, "__dict__", {})
    if key in namespace:
        return namespace[key]
    for cls in type(source).__mro__:
        if key in getattr(cls, "__dict__", {}):
            return getattr(source, key)
    return _MISSING


def _required_value(source: Any, key: str) -> Any:
    value = _value(source, key, _MISSING)
    if value is _MISSING or value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def _normalize_mode(value: Any, *, label: str) -> ExecutionMode:
    if not isinstance(value, str):
        raise HarnessExecutorSelectionError(
            f"{label} must be one of {sorted(EXECUTION_MODES)}, got {value!r}",
        )
    normalized = value.strip().casefold()
    if normalized not in EXECUTION_MODES:
        raise HarnessExecutorSelectionError(
            f"{label} must be one of {sorted(EXECUTION_MODES)}, got {value!r}",
        )
    return normalized  # type: ignore[return-value]


def validate_execution_contract(
    *,
    schema: Any,
    work_order: Any,
    requested_executor: str | None = None,
    harness_run: Any | None = None,
    run_manifest: Any | None = None,
) -> ExecutionMode:
    """Validate schema/order/requested-executor compatibility.

    ``execution_mode`` remains the compatibility alias.  A missing persisted
    ``requested_executor`` is accepted only as the legacy alias of the
    work-order mode; a supplied conflicting value is always a request error.
    """

    schema_raw = _required_value(schema, "execution_mode")
    order_raw = _required_value(work_order, "execution_mode")
    order_requested = _value(work_order, "requested_executor", None)

    # A future/persisted shape may carry only requested_executor.  The current
    # DocumentWorkOrder always has execution_mode, but accepting this alias
    # keeps the adapter usable during the migration window.
    if order_raw is None and order_requested is not None:
        order_raw = order_requested
    if schema_raw is None:
        raise HarnessExecutorSelectionError(
            "schema execution_mode is required",
        )
    if order_raw is None:
        raise HarnessExecutorSelectionError(
            "work order execution_mode is required",
        )

    schema_mode = _normalize_mode(schema_raw, label="schema.execution_mode")
    order_mode = _normalize_mode(order_raw, label="work_order.execution_mode")
    if schema_mode != order_mode:
        raise HarnessExecutorSelectionError(
            f"schema execution_mode {schema_mode!r} does not match "
            f"work order execution_mode {order_mode!r}",
        )

    values: list[tuple[str, Any]] = [
        ("schema.requested_executor", _value(schema, "requested_executor", None)),
        ("work_order.requested_executor", order_requested),
        ("requested_executor", requested_executor),
        ("harness_run.requested_executor", _value(harness_run, "requested_executor", None)),
        ("run_manifest.requested_executor", _value(run_manifest, "requested_executor", None)),
    ]
    manifest_mode = _value(run_manifest, "execution_mode", None)
    if manifest_mode is not None:
        manifest_mode = _normalize_mode(manifest_mode, label="run_manifest.execution_mode")
        if manifest_mode != schema_mode:
            raise HarnessExecutorSelectionError(
                f"run manifest execution_mode {manifest_mode!r} does not match "
                f"schema/order mode {schema_mode!r}",
            )

    for label, value in values:
        if value is None:
            continue
        normalized = _normalize_mode(value, label=label)
        if normalized != schema_mode:
            raise HarnessExecutorSelectionError(
                f"{label} {normalized!r} does not match schema/order mode {schema_mode!r}",
            )
    return schema_mode


# Compatibility spelling used by a few callers during the execution_mode ->
# requested_executor migration.
validate_executor_contract = validate_execution_contract


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HarnessPolicyError(
                "policy expiry is not a valid datetime",
                error_code=ERROR_POLICY_EXPIRED,
            ) from exc
    else:
        raise HarnessPolicyError(
            "policy expiry is not a valid datetime",
            error_code=ERROR_POLICY_EXPIRED,
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sequence_of(value: Any, *, label: str, default: Sequence[str] = ()) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, (str, bytes)):
        raise HarnessPolicyError(f"{label} must be a list", error_code=ERROR_AGENT_PROFILE_INVALID)
    try:
        result = [str(item).strip() for item in value]
    except TypeError as exc:
        raise HarnessPolicyError(f"{label} must be a list", error_code=ERROR_AGENT_PROFILE_INVALID) from exc
    if any(not item for item in result):
        raise HarnessPolicyError(f"{label} contains an empty name", error_code=ERROR_AGENT_PROFILE_INVALID)
    return result


def _policy_hash(policy: Any) -> str:
    try:
        return content_hash(policy)
    except Exception:
        # Adapter tests and migration readers may expose an expiry datetime in
        # a plain mapping rather than in a Pydantic model.  Keep the same
        # sorted JSON/hash contract for that shape instead of silently
        # disabling the frozen-policy check.
        def normalize(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.replace(
                    tzinfo=timezone.utc
                ).isoformat()
            if isinstance(value, Mapping):
                return {str(key): normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            if hasattr(value, "model_dump"):
                return normalize(value.model_dump(mode="json"))
            if isinstance(value, (str, int, bool)) or value is None:
                return value
            if hasattr(value, "__dict__"):
                return normalize(vars(value))
            raise TypeError(f"unsupported policy value: {type(value).__name__}")

        try:
            return hashlib.sha256(canonical_json(normalize(policy)).encode("utf-8")).hexdigest()
        except Exception as exc:
            raise HarnessPolicyError(
                "approved policy is not canonically serializable",
                error_code=ERROR_POLICY_HASH_MISMATCH,
            ) from exc


def _validate_agent_tool_profile(policy: Any) -> list[str]:
    agent_tools = _sequence_of(_value(policy, "agent_tools", None), label="policy.agent_tools")
    normalized_tools = [item.casefold() for item in agent_tools]
    if len(normalized_tools) != len(set(normalized_tools)):
        raise HarnessPolicyError(
            "policy.agent_tools must be unique",
            error_code=ERROR_AGENT_PROFILE_INVALID,
        )
    for tool_name in normalized_tools:
        if tool_name in FORBIDDEN_AGENT_TOOLS or tool_name not in AGENT_TOOL_ALLOWLIST:
            raise HarnessPolicyError(
                f"policy.agent_tools contains an unauthorized tool: {tool_name}",
                error_code=ERROR_AGENT_TOOL_NOT_ALLOWED,
            )

    excluded = _sequence_of(
        _value(policy, "excluded_tools", None),
        label="policy.excluded_tools",
        default=tuple(FORBIDDEN_AGENT_TOOLS),
    )
    excluded_normalized = {item.casefold() for item in excluded}
    if not REQUIRED_EXCLUDED_AGENT_CAPABILITIES.issubset(excluded_normalized):
        missing = sorted(REQUIRED_EXCLUDED_AGENT_CAPABILITIES - excluded_normalized)
        raise HarnessPolicyError(
            f"restricted agent profile does not exclude: {missing}",
            error_code=ERROR_AGENT_PROFILE_INVALID,
        )

    backend_profile = str(_value(policy, "backend_profile", "restricted")).strip().casefold()
    if backend_profile != "restricted":
        raise HarnessPolicyError(
            f"unsupported agent backend profile: {backend_profile!r}",
            error_code=ERROR_AGENT_PROFILE_INVALID,
        )

    # ``allowed_tools`` belongs to the internal graph policy.  It is read only
    # for shape validation and is deliberately not intersected with agent_tools.
    _sequence_of(_value(policy, "allowed_tools", None), label="policy.allowed_tools")
    return agent_tools


def validate_approved_harness_policy(
    policy: Any,
    *,
    work_order: Any | None = None,
    run_manifest: Any | None = None,
    expected_policy_hash: str | None = None,
    require_binding: bool = True,
    now: datetime | None = None,
) -> Any:
    """Validate a frozen policy without allowing availability degradation.

    The function accepts the real ``HarnessPolicy`` as well as a mapping-like
    adapter, which keeps the selector usable while persisted policy loading is
    being migrated.  An approved status is never inferred from a missing or
    malformed field.
    """

    if policy is None:
        raise HarnessPolicyError(
            "external/internal harness execution requires an approved HarnessPolicy",
            error_code=ERROR_POLICY_REQUIRED,
        )
    # ``HarnessToolPolicy`` is the graph-side wrapper; accept it without
    # coupling the agent module to the graph implementation.
    policy = _value(policy, "policy", policy)
    status = _value(policy, "status", _MISSING)
    if status is _MISSING or str(status).strip().casefold() != "approved":
        raise HarnessPolicyError(
            f"HarnessPolicy status must be approved, got {status!r}",
            error_code=ERROR_POLICY_NOT_APPROVED,
        )

    policy_id = _required_value(policy, "harness_policy_id")
    policy_version = _required_value(policy, "version")
    if policy_id is None or policy_version is None:
        raise HarnessPolicyError(
            "approved policy must have a non-empty id and version",
            error_code=ERROR_POLICY_REQUIRED,
        )

    if require_binding and work_order is not None:
        order_id = _required_value(work_order, "harness_policy_id")
        order_version = _required_value(work_order, "harness_policy_version")
        if order_id is None or order_version is None:
            raise HarnessPolicyError(
                "work order has no frozen HarnessPolicy id/version",
                error_code=ERROR_POLICY_REQUIRED,
            )
        if str(order_id) != str(policy_id) or str(order_version) != str(policy_version):
            raise HarnessPolicyError(
                "work-order HarnessPolicy binding does not match the loaded policy",
                error_code=ERROR_POLICY_BINDING_MISMATCH,
            )

    if run_manifest is not None:
        manifest_id = _value(run_manifest, "harness_policy_id", None)
        manifest_version = _value(run_manifest, "harness_policy_version", None)
        if manifest_id is not None and str(manifest_id) != str(policy_id):
            raise HarnessPolicyError(
                "run manifest HarnessPolicy id does not match the loaded policy",
                error_code=ERROR_POLICY_BINDING_MISMATCH,
            )
        if manifest_version is not None and str(manifest_version) != str(policy_version):
            raise HarnessPolicyError(
                "run manifest HarnessPolicy version does not match the loaded policy",
                error_code=ERROR_POLICY_BINDING_MISMATCH,
            )

    revoked = _value(policy, "revoked", False)
    revoked = (
        revoked.strip().casefold() in {"1", "true", "yes", "on"}
        if isinstance(revoked, str)
        else bool(revoked)
    )
    if revoked or _value(policy, "revoked_at", None) is not None:
        raise HarnessPolicyError("HarnessPolicy has been revoked", error_code=ERROR_POLICY_REVOKED)

    frozen = _value(policy, "is_frozen", _value(policy, "frozen", True))
    if frozen is None or frozen is False:
        raise HarnessPolicyError(
            "HarnessPolicy is not frozen",
            error_code=ERROR_POLICY_NOT_APPROVED,
        )

    expires_at = _parse_datetime(_value(policy, "expires_at", None))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= current.astimezone(timezone.utc):
        raise HarnessPolicyError("HarnessPolicy has expired", error_code=ERROR_POLICY_EXPIRED)

    for name, default in (("max_agent_tool_calls", 60), ("max_proposal_retries_per_field", 2)):
        value = _value(policy, name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise HarnessPolicyError(
                f"{name} must be a positive integer",
                error_code=ERROR_AGENT_PROFILE_INVALID,
            )
    confidence = _value(policy, "min_agent_confidence", _value(policy, "agent_confidence_threshold", 0.7))
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise HarnessPolicyError(
            "agent confidence threshold must be between 0 and 1",
            error_code=ERROR_AGENT_PROFILE_INVALID,
        )

    _validate_agent_tool_profile(policy)

    actual_hash = _policy_hash(policy)
    declared_hash = _value(policy, "policy_hash", None)
    if declared_hash is not None and str(declared_hash) != actual_hash:
        raise HarnessPolicyError(
            "policy self-reported hash does not match its contents",
            error_code=ERROR_POLICY_HASH_MISMATCH,
        )
    manifest_hash = _value(run_manifest, "tool_policy_hash", None) if run_manifest is not None else None
    expected = expected_policy_hash or manifest_hash or _value(work_order, "harness_policy_hash", None)
    if expected and str(expected) != actual_hash:
        raise HarnessPolicyError(
            "loaded HarnessPolicy hash does not match the frozen hash",
            error_code=ERROR_POLICY_HASH_MISMATCH,
        )
    return policy


# Short compatibility aliases for callers that use the word ``agent``.
validate_agent_policy = validate_approved_harness_policy
validate_harness_policy = validate_approved_harness_policy


def _read_agent_mode_flag(value: Any, settings: Any | None = None) -> bool:
    def as_bool(candidate: Any) -> bool:
        if isinstance(candidate, str):
            return candidate.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(candidate)

    if value is not None:
        return as_bool(value)
    source = settings
    if source is None:
        try:
            source = import_module("src.settings")
        except Exception:
            return False
    if isinstance(source, Mapping):
        return as_bool(source.get("DOCUMENT_AUTHORING_AGENT_MODE_ENABLED", False))
    return as_bool(getattr(source, "DOCUMENT_AUTHORING_AGENT_MODE_ENABLED", False))


def _probe_infrastructure(value: Any) -> bool:
    if value is None:
        # The skeleton is deliberately selectable even before a real agent
        # runner is injected; it will record agent_tools_not_implemented at
        # execution time and use the graph fallback.
        return True
    candidate = value
    if hasattr(candidate, "is_available"):
        candidate = getattr(candidate, "is_available")
    try:
        result = candidate() if callable(candidate) else candidate
        if isinstance(result, str):
            return result.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(result)
    except Exception:
        return False


def _unique_reasons(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ExecutorSelection:
    """Selector result with a protocol executor and auditable gate outcome."""

    executor: HarnessExecutor
    requested_executor: ExecutionMode
    effective_executor: EffectiveExecutor
    degraded_reasons: list[str] = field(default_factory=list)
    gates: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep the direct-executor and selection-returning APIs equivalent:
        # callers can inspect gate/degradation metadata either way.
        try:
            setattr(self.executor, "selection", self)
            setattr(self.executor, "gates", dict(self.gates))
            if not isinstance(self.executor, AgentFieldHarness):
                setattr(self.executor, "degraded_reasons", list(self.degraded_reasons))
        except Exception:
            pass

    @property
    def selected_executor(self) -> HarnessExecutor:
        return self.executor

    @property
    def gate_results(self) -> dict[str, bool]:
        return dict(self.gates)

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded_reasons)

    def apply_to_run(self, harness_run: Any, *, agent_thread: str | None = None) -> Any:
        """Annotate an existing run; this never creates or replaces a run."""

        _record_run_execution(
            harness_run,
            requested_executor=self.requested_executor,
            effective_executor=self.effective_executor,
            degraded_reasons=self.degraded_reasons,
            agent_thread_id_value=agent_thread,
        )
        return harness_run

    def __getattr__(self, name: str) -> Any:
        # Makes a selection usable by legacy code that expected selector() to
        # return the executor directly, while retaining gate metadata.
        return getattr(self.executor, name)


HarnessExecutorSelection = ExecutorSelection


def _missing_executor(effective: EffectiveExecutor) -> HarnessExecutor:
    return InternalGraphExecutor(None, effective_executor=effective)


def _coerce_executor(executor: Any | None, *, effective_executor: EffectiveExecutor) -> HarnessExecutor:
    if isinstance(executor, InternalGraphExecutor):
        return executor
    if executor is None:
        return _missing_executor(effective_executor)
    if getattr(executor, "effective_executor", None) == effective_executor and hasattr(executor, "execute"):
        return executor
    return InternalGraphExecutor(executor, effective_executor=effective_executor)


def _executor_policy(executor: Any | None) -> Any | None:
    """Read a graph's policy when the fallback exposes one, without guessing."""

    candidate = executor
    for _ in range(2):
        if candidate is None:
            return None
        policy = _explicit_attribute(candidate, "policy")
        if policy is not _MISSING and policy is not None:
            return policy
        candidate = _explicit_attribute(candidate, "delegate")
        if candidate is _MISSING:
            return None
    return None


def _has_explicit_callable(source: Any, name: str) -> bool:
    """Avoid treating ``unittest.mock.Mock`` dynamic attributes as methods."""

    value = _explicit_attribute(source, name)
    return value is not _MISSING and callable(value)


def _call_candidates(
    target: Callable[..., Any],
    candidates: Sequence[tuple[tuple[Any, ...], dict[str, Any]]],
) -> Any:
    """Call a target using signature binding without swallowing body errors."""

    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        args, kwargs = candidates[0]
        return target(*args, **kwargs)

    for args, raw_kwargs in candidates:
        kwargs = dict(raw_kwargs)
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if not accepts_kwargs:
            kwargs = {
                key: value for key, value in kwargs.items()
                if key in parameters and parameters[key].kind is not inspect.Parameter.POSITIONAL_ONLY
            }
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return target(*args, **kwargs)
    raise TypeError("executor callable does not match HarnessExecutionContext or runtime keyword contract")


def _invoke_target(target: Callable[..., Any], context: HarnessExecutionContext, *, field_id: str | None = None) -> Any:
    kwargs = context.as_kwargs(include_field_ids=False)
    if field_id is not None:
        kwargs_with_field = {**kwargs, "field_id": field_id}
        candidates = [
            ((field_id,), kwargs),
            ((), {"field_id": field_id, "context": context}),
            ((field_id, context), {}),
            ((context,), {}),
            ((), kwargs_with_field),
        ]
    else:
        candidates = [
            ((context,), {}),
            ((), kwargs),
            ((), {"context": context}),
            ((context.harness_run, context.schema), {}),
            ((context.harness_run,), {}),
        ]
    return _call_candidates(target, candidates)


class InternalGraphExecutor:
    """Adapt existing ``AuthoringGraph``/callable shapes to the protocol."""

    def __init__(
        self,
        delegate: Any | None,
        *,
        effective_executor: EffectiveExecutor = "authoring_graph",
    ):
        self.delegate = delegate
        self.effective_executor = effective_executor
        self.supports_field_fallback = _has_explicit_callable(delegate, "run_field")

    def execute(self, context: HarnessExecutionContext | None = None, **kwargs: Any) -> Any:
        resolved = _context_from_call(context, kwargs)
        if self.delegate is None:
            raise FallbackExecutorUnavailable()
        target = _explicit_attribute(self.delegate, "execute")
        if target is not _MISSING and callable(target):
            return _invoke_target(target, resolved)
        target = _explicit_attribute(self.delegate, "run")
        if target is not _MISSING and callable(target):
            return _invoke_target(target, resolved)
        if callable(self.delegate):
            return _invoke_target(self.delegate, resolved)
        raise FallbackExecutorUnavailable("delegate has no execute/run/callable interface")

    def run(self, context: HarnessExecutionContext | None = None, **kwargs: Any) -> Any:
        return self.execute(context, **kwargs)

    def run_field(
        self,
        field_id: str,
        context: HarnessExecutionContext | None = None,
        **kwargs: Any,
    ) -> Any:
        resolved = _context_from_call(context, kwargs).with_fields((str(field_id),))
        if self.delegate is None:
            raise FallbackExecutorUnavailable()
        target = _explicit_attribute(self.delegate, "run_field")
        if target is not _MISSING and callable(target):
            return _invoke_target(target, resolved, field_id=str(field_id))
        return self.execute(resolved)


class DeterministicRuleExecutor(InternalGraphExecutor):
    """Protocol adapter for the existing deterministic-only path."""

    def __init__(self, delegate: Any | None):
        super().__init__(delegate, effective_executor="deterministic_rule")


def _context_from_call(
    context: HarnessExecutionContext | None,
    kwargs: Mapping[str, Any],
) -> HarnessExecutionContext:
    if context is not None:
        if not isinstance(context, HarnessExecutionContext):
            raise TypeError("executor context must be HarnessExecutionContext")
        if kwargs:
            merged = dict(context.extra)
            merged.update(dict(kwargs))
            return replace(context, extra=merged)
        return context
    values = dict(kwargs)
    work_order = values.pop("work_order", None)
    harness_run = values.pop("harness_run", values.pop("run", None))
    schema = values.pop("schema", None)
    if work_order is None or harness_run is None or schema is None:
        raise TypeError("executor call requires work_order, harness_run/run and schema")
    run_manifest = values.pop("run_manifest", values.pop("manifest", None))
    field_ids = values.pop("field_ids", ()) or ()
    legacy_claims = values.pop("legacy_claims", ()) or ()
    return HarnessExecutionContext(
        work_order=work_order,
        harness_run=harness_run,
        schema=schema,
        policy=values.pop("policy", None),
        run_manifest=run_manifest,
        snapshot=values.pop("snapshot", None),
        legacy_claims=tuple(legacy_claims),
        writer=values.pop("writer", None),
        retrieve=values.pop("retrieve", None),
        checkpointer=values.pop("checkpointer", None),
        field_ids=tuple(str(value) for value in field_ids),
        extra=values,
    )


def _record_run_execution(
    harness_run: Any,
    *,
    requested_executor: str | None,
    effective_executor: EffectiveExecutor,
    degraded_reasons: Sequence[str],
    agent_thread_id_value: str | None = None,
) -> None:
    """Record selector facts on the existing run object when fields exist."""

    if harness_run is None:
        return
    updates: dict[str, Any] = {
        "effective_executor": effective_executor,
        "degraded_reasons": _unique_reasons(
            [*(_value(harness_run, "degraded_reasons", []) or []), *degraded_reasons]
        ),
    }
    if requested_executor is not None and _value(harness_run, "requested_executor", None) is None:
        updates["requested_executor"] = requested_executor
    if agent_thread_id_value is not None:
        updates["agent_thread_id"] = agent_thread_id_value

    for key, value in updates.items():
        try:
            setattr(harness_run, key, value)
        except Exception:
            # A compatibility DTO may be immutable or omit migration fields;
            # the selector result still carries the facts for the runtime to
            # persist through its own update path.
            try:
                object.__setattr__(harness_run, key, value)
            except Exception:
                continue


def _field_id(value: Any) -> str | None:
    raw = _value(value, "field_id", None)
    if raw is None:
        return None
    text = str(raw).strip()
    if text.startswith("field:"):
        text = text.removeprefix("field:")
    return text or None


def _schema_field_ids(schema: Any) -> list[str]:
    fields = _value(schema, "fields", ()) or ()
    result: list[str] = []
    for item in fields:
        field_id = _field_id(item)
        if field_id is None or field_id in result:
            continue
        # Deterministic and human-only fields are not agent semantic units.
        policy = str(_value(item, "authoring_policy", "managed_writer")).strip().casefold()
        if policy in {"deterministic", "human_only"}:
            continue
        result.append(field_id)
    return result


def _expected_agent_typed_kind(value_type: str) -> str | None:
    normalized = str(value_type or "").strip().casefold()
    if normalized in {"text", "string", "scalar", "number", "integer", "float", "date", "datetime", "boolean", "bool"}:
        return "scalar"
    if normalized in {"enum", "enumeration", "list", "set", "array", "multi_enum"}:
        return "enumeration"
    return None


_COMMITTED_FIELD_STATUSES = frozenset({
    "committed", "ready_to_render", "draft_persisted", "completed", "succeeded",
})


def _pending_field_ids(
    schema: Any,
    harness_run: Any,
    requested: Sequence[str] | None,
    extra: Mapping[str, Any],
) -> list[str]:
    available = _schema_field_ids(schema)
    if requested is not None:
        requested_ids = []
        for value in requested:
            normalized = str(value).strip().removeprefix("field:")
            if normalized and normalized not in requested_ids:
                requested_ids.append(normalized)
        unknown = [value for value in requested_ids if value not in available]
        if unknown:
            raise HarnessExecutorSelectionError(
                f"requested agent fields are not in schema: {unknown}",
            )
        available = requested_ids

    statuses = _value(harness_run, "unit_statuses", {}) or {}
    committed = {
        str(key).strip().removeprefix("field:"): str(value).strip().casefold()
        for key, value in (statuses.items() if isinstance(statuses, Mapping) else ())
    }
    committed_ids = {
        str(value).strip().removeprefix("field:")
        for value in (_value(harness_run, "committed_field_ids", ()) or ())
    }
    committed_ids.update(
        str(value).strip().removeprefix("field:")
        for value in (extra.get("committed_field_ids", ()) or ())
    )
    is_committed = extra.get("is_field_committed")
    pending: list[str] = []
    for field_id in available:
        done = field_id in committed_ids or committed.get(field_id) in _COMMITTED_FIELD_STATUSES
        if not done and callable(is_committed):
            try:
                done = bool(is_committed(field_id))
            except Exception:
                # A failed diagnostic callback must not turn an unfinished
                # field into a falsely committed one.
                done = False
        if not done:
            pending.append(field_id)
    return pending


@dataclass
class AgentHarnessExecutionMetadata:
    harness_run_id: str
    effective_executor: EffectiveExecutor
    degraded_reasons: list[str] = field(default_factory=list)
    agent_thread_ids: dict[str, str] = field(default_factory=dict)
    fallback_field_ids: list[str] = field(default_factory=list)
    agent_token_usage: dict[str, Any] = field(default_factory=dict)


class AgentFieldHarness:
    """Restricted per-field agent boundary with a safe graph fallback.

    Tool calls are validated against the frozen field contract, evidence
    registry and proposal budgets.  The real provider-backed agent loop is
    opt-in; when unavailable, ``run`` records a stable degradation reason and
    invokes the supplied fallback executor for only pending fields.
    """

    def __init__(
        self,
        fallback_executor: Any | None = None,
        *,
        fallback: Any | None = None,
        policy: Any | None = None,
        agent_tools: Sequence[str] | None = None,
        tools: Mapping[str, Any] | None = None,
        validator: Any | None = None,
        agent_tools_implemented: bool = False,
        agent_runner: Callable[..., Any] | None = None,
        backend_profile: str = "restricted",
        permissions: Sequence[str] | None = None,
        excluded_tools: Sequence[str] | None = None,
        initial_degraded_reasons: Sequence[str] = (),
        on_run_update: Callable[[Any], None] | None = None,
        on_degraded: Callable[..., Any] | None = None,
    ):
        if fallback_executor is not None and fallback is not None and fallback_executor is not fallback:
            raise ValueError("fallback_executor and fallback refer to different executors")
        self.policy = policy
        if policy is not None:
            policy = _value(policy, "policy", policy)
            configured_tools = _sequence_of(
                _value(policy, "agent_tools", None), label="policy.agent_tools"
            )
            # Constructor-level agent_tools cannot broaden a frozen policy.
            if agent_tools is not None and {str(item).casefold() for item in agent_tools} != {
                str(item).casefold() for item in configured_tools
            }:
                raise HarnessPolicyError(
                    "constructor agent_tools do not match the frozen policy",
                    error_code=ERROR_AGENT_TOOL_NOT_ALLOWED,
                )
            agent_tools = configured_tools
        else:
            agent_tools = _sequence_of(agent_tools, label="agent_tools")

        self.backend_profile = str(
            _value(policy, "backend_profile", backend_profile) if policy is not None else backend_profile
        ).strip().casefold()
        self.permissions = frozenset(str(value).strip().casefold() for value in (permissions or ()))
        self.excluded_tools = frozenset(
            str(value).strip().casefold()
            for value in (
                excluded_tools
                if excluded_tools is not None
                else (_value(policy, "excluded_tools", None) if policy is not None else FORBIDDEN_AGENT_TOOLS)
            )
        )
        if (
            self.backend_profile != "restricted"
            or not REQUIRED_EXCLUDED_AGENT_CAPABILITIES.issubset(self.excluded_tools)
        ):
            raise HarnessPolicyError(
                "AgentFieldHarness requires the restricted excluded-tools profile",
                error_code=ERROR_AGENT_PROFILE_INVALID,
            )
        if self.permissions.intersection(FORBIDDEN_AGENT_TOOLS):
            raise HarnessPolicyError(
                "AgentFieldHarness permissions contain a forbidden capability",
                error_code=ERROR_AGENT_PROFILE_INVALID,
            )

        normalized_tools = [str(value).strip().casefold() for value in agent_tools]
        if len(normalized_tools) != len(set(normalized_tools)):
            raise HarnessPolicyError(
                "agent_tools must be unique",
                error_code=ERROR_AGENT_PROFILE_INVALID,
            )
        for tool_name in normalized_tools:
            if tool_name not in AGENT_TOOL_ALLOWLIST or tool_name in FORBIDDEN_AGENT_TOOLS:
                raise AgentToolNotAllowed(tool_name)
        self.agent_tools = tuple(normalized_tools)
        self.visible_tools = tuple(normalized_tools)
        self.tools = {
            name: implementation
            for name, implementation in (tools or {}).items()
            if str(name).strip().casefold() in self.agent_tools
        }
        for name in (tools or {}):
            normalized_name = str(name).strip().casefold()
            if normalized_name not in AGENT_TOOL_ALLOWLIST or normalized_name not in self.agent_tools:
                raise AgentToolNotAllowed(normalized_name)
        self.agent_tools_implemented = bool(agent_tools_implemented)
        if validator is None:
            from src.document_authoring.validator import DocumentValidator

            validator = DocumentValidator()
        self.validator = validator
        self.agent_runner = agent_runner or (
            self._run_deep_agent if self.agent_tools_implemented else None
        )
        self.fallback_executor = (
            _coerce_executor(fallback_executor if fallback_executor is not None else fallback,
                             effective_executor="authoring_graph")
            if fallback_executor is not None or fallback is not None
            else None
        )
        self.degraded_reasons = _unique_reasons(initial_degraded_reasons)
        self._base_degraded_reasons = list(self.degraded_reasons)
        self.on_run_update = on_run_update
        self.on_degraded = on_degraded
        self.agent_thread_ids: dict[str, str] = {}
        self.last_execution: AgentHarnessExecutionMetadata | None = None
        self.last_result: Any | None = None
        self.last_fallback_executor: Any | None = None
        self._active_context: HarnessExecutionContext | None = None
        self._effective_executor: EffectiveExecutor = "agent_field_harness"
        self._tool_call_count = 0
        self._proposal_attempts: dict[str, int] = {}
        self._retrieval_attempts: dict[str, int] = {}
        self._accepted_drafts: dict[str, DocumentUnitDraft] = {}
        self._field_outcomes: dict[str, Any] = {}
        self._retrieval_ledger: list[dict[str, Any]] = []
        self._missing_statuses: dict[str, str] = {}
        self._waiting_fields: set[str] = set()
        self._agent_token_usage: dict[str, Any] = {
            "usage_returned": False,
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "call_count": 0,
        }

    @property
    def effective_executor(self) -> EffectiveExecutor:  # type: ignore[override]
        return self._effective_executor

    @effective_executor.setter
    def effective_executor(self, value: EffectiveExecutor) -> None:
        self._effective_executor = value

    def require_agent_tool(self, tool_name: str) -> None:
        normalized = str(tool_name).strip().casefold()
        if normalized not in self.visible_tools:
            raise AgentToolNotAllowed(normalized)

    # A shorter graph-policy-like spelling is useful to callers and keeps the
    # two allowlist boundaries visibly separate.
    require_tool = require_agent_tool

    def agent_thread_id(self, harness_run_id: str, field_id: str | None = None) -> str:
        if field_id is None:
            if self._active_context is None:
                raise ValueError("field agent thread id requires a run id and field id")
            field_id = harness_run_id
            harness_run_id = str(_value(self._active_context.harness_run, "harness_run_id", ""))
        return agent_thread_id(str(harness_run_id), str(field_id))

    def thread_id_for_field(self, field_id: str, harness_run_id: str | None = None) -> str:
        if harness_run_id is None:
            if self._active_context is None:
                raise ValueError("field agent thread id requires an active run")
            harness_run_id = str(_value(self._active_context.harness_run, "harness_run_id", ""))
        return self.agent_thread_id(str(harness_run_id), str(field_id))

    def run(
        self,
        context: HarnessExecutionContext | None = None,
        *,
        work_order: Any | None = None,
        harness_run: Any | None = None,
        run: Any | None = None,
        run_manifest: Any | None = None,
        manifest: Any | None = None,
        policy: Any | None = None,
        schema: Any | None = None,
        snapshot: Any | None = None,
        legacy_claims: Sequence[Any] | None = None,
        writer: Any | None = None,
        retrieve: Callable[..., Any] | None = None,
        checkpointer: Any | None = None,
        field_ids: Sequence[str] | None = None,
        fallback_executor: Any | None = None,
        fallback: Any | None = None,
        **extra: Any,
    ) -> Any:
        resolved = _context_from_call(
            context,
            {
                **({"work_order": work_order} if work_order is not None else {}),
                **({"harness_run": harness_run} if harness_run is not None else {}),
                **({"run": run} if run is not None else {}),
                **({"run_manifest": run_manifest} if run_manifest is not None else {}),
                **({"manifest": manifest} if manifest is not None else {}),
                **({"policy": policy} if policy is not None else {}),
                **({"schema": schema} if schema is not None else {}),
                **({"snapshot": snapshot} if snapshot is not None else {}),
                **({"legacy_claims": legacy_claims or ()}),
                **({"writer": writer} if writer is not None else {}),
                **({"retrieve": retrieve} if retrieve is not None else {}),
                **({"checkpointer": checkpointer} if checkpointer is not None else {}),
                **({"field_ids": field_ids} if field_ids is not None else {}),
                **extra,
            },
        )
        mode = validate_execution_contract(
            schema=resolved.schema,
            work_order=resolved.work_order,
            harness_run=resolved.harness_run,
            run_manifest=resolved.run_manifest,
        )
        if mode != "external_agent":
            raise HarnessExecutorSelectionError(
                f"AgentFieldHarness only accepts external_agent, got {mode!r}",
            )
        active_policy = resolved.policy if resolved.policy is not None else self.policy
        active_policy = validate_approved_harness_policy(
            active_policy,
            work_order=resolved.work_order,
            run_manifest=resolved.run_manifest,
            require_binding=True,
        )
        self.policy = active_policy
        self._reset_agent_state()
        configured_policy = _value(self.policy, "policy", self.policy) if self.policy is not None else None
        if configured_policy is not None and _policy_hash(configured_policy) != _policy_hash(active_policy):
            raise HarnessPolicyError(
                "run policy does not match the harness frozen policy",
                error_code=ERROR_POLICY_BINDING_MISMATCH,
            )
        fallback_policy = _executor_policy(self.fallback_executor)
        if fallback_policy is not None:
            fallback_policy = validate_approved_harness_policy(
                fallback_policy,
                work_order=resolved.work_order,
                run_manifest=resolved.run_manifest,
                require_binding=True,
            )
            if _policy_hash(fallback_policy) != _policy_hash(active_policy):
                raise HarnessPolicyError(
                    "fallback executor policy is not the same approved policy version",
                    error_code=ERROR_POLICY_BINDING_MISMATCH,
                )

        run_id = _required_value(resolved.harness_run, "harness_run_id")
        if run_id is None:
            raise HarnessExecutorSelectionError("HarnessRun harness_run_id is required")
        self._effective_executor = "agent_field_harness"
        self.degraded_reasons = list(self._base_degraded_reasons)
        pending = _pending_field_ids(
            resolved.schema,
            resolved.harness_run,
            resolved.field_ids or None,
            resolved.extra,
        )
        self.agent_thread_ids = {
            field_id: self.agent_thread_id(str(run_id), field_id)
            for field_id in pending
        }
        self._active_context = resolved
        try:
            if not pending:
                self._effective_executor = "agent_field_harness"
                selected_thread = next(iter(self.agent_thread_ids.values()), None)
                _record_run_execution(
                    resolved.harness_run,
                    requested_executor="external_agent",
                    effective_executor=self._effective_executor,
                    degraded_reasons=self.degraded_reasons,
                    agent_thread_id_value=selected_thread,
                )
                self._notify_run_update(resolved.harness_run)
                result = _empty_execution_result(resolved.harness_run, resolved.schema)
                return self._finish(result, pending)

            if not self.agent_tools_implemented:
                self._degrade(REASON_AGENT_TOOLS_NOT_IMPLEMENTED, resolved, pending)
                result = self._run_fallback(
                    resolved,
                    pending,
                    fallback_executor=fallback_executor if fallback_executor is not None else fallback,
                )
                return self._finish(result, pending)

            if self.agent_runner is None:
                self._degrade(REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE, resolved, pending)
                result = self._run_fallback(
                    resolved,
                    pending,
                    fallback_executor=fallback_executor if fallback_executor is not None else fallback,
                )
                return self._finish(result, pending)

            try:
                result = _invoke_target(self.agent_runner, resolved.with_fields(pending))
            except (AgentInfrastructureUnavailable, AgentToolsNotImplemented, ImportError, ModuleNotFoundError):
                self._degrade(REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE, resolved, pending)
                result = self._run_fallback(
                    resolved,
                    pending,
                    fallback_executor=fallback_executor if fallback_executor is not None else fallback,
                )
                return self._finish(result, pending)
            own_result = self._build_agent_result()
            try:
                from src.document_authoring.harness.graph import HarnessExecutionResult
            except ImportError:  # pragma: no cover - graph is a production dependency
                HarnessExecutionResult = ()
            if result is None:
                result = own_result
            elif self._accepted_drafts or self._pending_proposals or self._missing_statuses:
                if isinstance(result, HarnessExecutionResult):
                    result = _merge_execution_results(
                        [result, own_result], resolved.harness_run, resolved.schema,
                    )
                else:
                    result = own_result
            unresolved = [
                field_id for field_id in pending
                if field_id not in self._accepted_drafts
                and field_id not in self._missing_statuses
                and field_id not in self._waiting_fields
            ]
            if unresolved:
                self._degrade(REASON_AGENT_PROPOSAL_BUDGET_EXHAUSTED, resolved, unresolved)
                fallback_result = self._run_fallback(
                    resolved,
                    unresolved,
                    fallback_executor=fallback_executor if fallback_executor is not None else fallback,
                )
                result = _merge_execution_results(
                    [result, fallback_result], resolved.harness_run, resolved.schema,
                )
            self._effective_executor = "agent_field_harness"
            selection = getattr(self, "selection", None)
            if isinstance(selection, ExecutorSelection):
                selection.effective_executor = "agent_field_harness"
                selection.degraded_reasons = list(self.degraded_reasons)
            _record_run_execution(
                resolved.harness_run,
                requested_executor="external_agent",
                effective_executor=self._effective_executor,
                degraded_reasons=self.degraded_reasons,
                agent_thread_id_value=next(iter(self.agent_thread_ids.values()), None),
            )
            self._notify_run_update(resolved.harness_run)
            return self._finish(result, pending)
        finally:
            self._active_context = None

    def execute(self, context: HarnessExecutionContext | None = None, **kwargs: Any) -> Any:
        return self.run(context, **kwargs)

    def _notify_run_update(self, harness_run: Any) -> None:
        if self.on_run_update is not None:
            self.on_run_update(harness_run)

    def _degrade(self, reason: str, context: HarnessExecutionContext, pending: Sequence[str]) -> None:
        if reason not in self.degraded_reasons:
            self.degraded_reasons.append(reason)
            if self.on_degraded is not None:
                try:
                    self.on_degraded(reason, context.harness_run, tuple(pending))
                except TypeError:
                    self.on_degraded(reason)
        self._effective_executor = "authoring_graph"
        selection = getattr(self, "selection", None)
        if isinstance(selection, ExecutorSelection):
            selection.effective_executor = "authoring_graph"
            selection.degraded_reasons = _unique_reasons(
                [*selection.degraded_reasons, *self.degraded_reasons]
            )
        _record_run_execution(
            context.harness_run,
            requested_executor="external_agent",
            effective_executor="authoring_graph",
            degraded_reasons=self.degraded_reasons,
            agent_thread_id_value=next(iter(self.agent_thread_ids.values()), None),
        )
        self._notify_run_update(context.harness_run)

    def _run_fallback(
        self,
        context: HarnessExecutionContext,
        pending: Sequence[str],
        *,
        fallback_executor: Any | None = None,
    ) -> Any:
        executor = (
            _coerce_executor(fallback_executor, effective_executor="authoring_graph")
            if fallback_executor is not None
            else self.fallback_executor
        )
        if executor is None:
            raise FallbackExecutorUnavailable()
        self.last_fallback_executor = executor
        results: list[Any] = []
        field_runner = getattr(executor, "run_field", None)
        if callable(field_runner) and getattr(executor, "supports_field_fallback", True):
            for field_id in pending:
                results.append(
                    _invoke_target(
                        field_runner,
                        context.with_fields((field_id,)),
                        field_id=field_id,
                    )
                )
            return _merge_execution_results(results, context.harness_run, context.schema)
        return executor.execute(context.with_fields(pending))

    def _run_deep_agent(self, context: HarnessExecutionContext) -> Any:
        """Run one restricted deepagents loop per pending semantic field.

        The framework's built-in filesystem and subagent tools are removed via
        an explicit HarnessProfile.  The only user-visible tools are the four
        coordinator adapters below; they return JSON only at this outer
        LangChain boundary while the domain methods remain typed Pydantic.
        """

        if not self.agent_tools:
            raise AgentToolsNotImplemented("approved policy exposes no agent tools")
        try:
            from deepagents import (
                GeneralPurposeSubagentProfile,
                HarnessProfile,
                create_deep_agent,
                register_harness_profile,
            )
            from deepagents.backends import StateBackend
            from langchain_core.tools import StructuredTool
            from src.core.model_factory import create_chat_model
        except (ImportError, ModuleNotFoundError) as exc:
            raise AgentInfrastructureUnavailable("deepagents dependencies are unavailable") from exc

        try:
            model = create_chat_model()
        except Exception as exc:
            raise AgentInfrastructureUnavailable("agent model could not be constructed") from exc
        excluded = frozenset({
            "ls", "read_file", "write_file", "edit_file", "glob", "grep",
            "delete", "execute", "task",
        })
        provider = str(
            getattr(model, "model_provider", None)
            or getattr(model, "provider", None)
            or getattr(model, "model_name", None)
            or type(model).__module__.split(".", 1)[0]
        ).split(":", 1)[0].casefold()
        if provider:
            try:
                register_harness_profile(
                    provider,
                    HarnessProfile(
                        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
                        excluded_tools=excluded,
                    ),
                )
            except Exception:
                # A provider-specific profile can be unavailable for a custom
                # adapter.  The explicit tool list and empty permissions still
                # apply; construction below is allowed to report the stable
                # infrastructure error rather than widening capabilities.
                pass

        def outer_tool(name: str, args_schema: type[BaseModel], method: Callable[..., Any]) -> Any:
            def call(**kwargs: Any) -> str:
                result = method(**kwargs)
                if not isinstance(result, BaseModel):
                    raise TypeError("agent tool returned a non-typed result")
                return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)

            call.__name__ = name
            call.__doc__ = f"Bounded document-authoring tool: {name}."
            return StructuredTool.from_function(
                call, name=name, description=call.__doc__, args_schema=args_schema,
            )

        tool_builders: dict[str, tuple[type[BaseModel], Callable[..., Any]]] = {
            "read_field_brief": (_FieldArgs, self.read_field_brief),
            "retrieve_evidence": (_RetrieveEvidenceArgs, self.retrieve_evidence),
            "propose_field_value": (_ProposalArgs, self.propose_field_value),
            "mark_missing": (_MissingArgs, self.mark_missing),
        }
        tools = [
            outer_tool(name, tool_builders[name][0], tool_builders[name][1])
            for name in self.agent_tools
            if name in tool_builders
        ]
        if not tools:
            raise AgentToolsNotImplemented("approved policy tools do not map to coordinator tools")

        policy = _value(self.policy, "policy", self.policy)
        recursion_limit = int(_value(policy, "max_steps", 40) or 40)
        all_fields = tuple(str(value).removeprefix("field:") for value in context.field_ids)
        for field_id in all_fields:
            action_key = self._next_action_key("agent_run", field_id)
            started = time.monotonic()
            self._append_event(
                "agent_started", action_key=action_key, status="started", field_id=field_id,
                payload={"thread_id": self.thread_id_for_field(field_id)},
            )
            status = "succeeded"
            try:
                with observe.agent(
                    "hdb.authoring.agent.field",
                    field_id=field_id,
                    thread_id=self.thread_id_for_field(field_id),
                ) as observation:
                    try:
                        agent = create_deep_agent(
                            model=model,
                            tools=tools,
                            system_prompt=(
                                "你是受限的文档字段提案 Agent。只能使用已显示的四个工具；"
                                "不能访问文件、命令、数据库或任意子 Agent。每个提案必须引用"
                                "retrieve_evidence 返回的 evidence_id；证据不足时调用 mark_missing。"
                                f"当前字段：{field_id}。先读取字段契约，再检索并提交提案。"
                            ),
                            backend=StateBackend(),
                            permissions=[],
                            # Human-in-the-loop is scoped to proposals only;
                            # retrieval/brief/missing remain coordinator-owned.
                            interrupt_on={"propose_field_value": True},
                            checkpointer=context.checkpointer,
                            name="document-authoring-field-agent",
                        )
                        llm_action_key = self._next_action_key("llm_call", field_id)
                        self._append_event(
                            "llm_called",
                            action_key=llm_action_key,
                            status="started",
                            field_id=field_id,
                            payload={"usage_returned": False},
                        )
                        try:
                            response = agent.invoke(
                                {
                                    "messages": [{
                                        "role": "user",
                                        "content": f"处理字段 field:{field_id}，完成一个可验证的字段提案。",
                                    }]
                                },
                                config={
                                    "recursion_limit": max(1, recursion_limit),
                                    "configurable": {
                                        "thread_id": self.thread_id_for_field(field_id),
                                        "fencing_token": _value(context.harness_run, "fencing_token", None),
                                    },
                                },
                            )
                        except Exception as exc:
                            self._append_event(
                                "llm_failed",
                                action_key=llm_action_key,
                                status="failed",
                                field_id=field_id,
                                error_code=type(exc).__name__,
                            )
                            raise
                        messages = response.get("messages", []) if isinstance(response, Mapping) else []
                        for message in messages:
                            self._observe_usage(message, observation)
                        self._append_event(
                            "llm_succeeded",
                            action_key=llm_action_key,
                            status="succeeded",
                            field_id=field_id,
                            payload={
                                "usage_returned": self._agent_token_usage["usage_returned"],
                                "prompt_tokens": self._agent_token_usage["prompt_tokens"],
                                "completion_tokens": self._agent_token_usage["completion_tokens"],
                                "total_tokens": self._agent_token_usage["total_tokens"],
                            },
                        )
                    except Exception as exc:
                        from src.document_authoring.harness.policy import HarnessLeaseLost

                        if isinstance(exc, HarnessLeaseLost):
                            raise
                        raise AgentInfrastructureUnavailable("agent execution failed") from exc
            except Exception:
                status = "failed"
                raise
            finally:
                duration = time.monotonic() - started
                record_authoring_agent(status=status, mode="external_agent", duration_s=duration)
                self._append_event(
                    "agent_succeeded" if status == "succeeded" else "agent_failed",
                    action_key=action_key,
                    status=status,
                    field_id=field_id,
                    duration_seconds=duration,
                    payload={"tool_call_count": self._tool_call_count, "usage_returned": self._agent_token_usage["usage_returned"]},
                )
        return self._build_agent_result()

    def _observe_usage(self, message: Any, observation: Any | None = None) -> None:
        metadata = _value(message, "usage_metadata", None)
        if not isinstance(metadata, Mapping):
            return
        prompt = _optional_int(metadata.get("input_tokens", metadata.get("prompt_tokens")))
        completion = _optional_int(metadata.get("output_tokens", metadata.get("completion_tokens")))
        total = _optional_int(metadata.get("total_tokens"))
        usage = self._agent_token_usage
        usage["usage_returned"] = True
        usage["call_count"] = int(usage.get("call_count") or 0) + 1
        for key, value in (("prompt_tokens", prompt), ("completion_tokens", completion), ("total_tokens", total)):
            if value is not None:
                usage[key] = int(usage.get(key) or 0) + value
        if total is None and prompt is not None and completion is not None:
            usage["total_tokens"] = int(usage.get("total_tokens") or 0) + prompt + completion
        if observation is not None:
            observation.tokens(
                input_tokens=prompt, output_tokens=completion,
                total_tokens=total,
            )

    def _finish(self, result: Any, pending: Sequence[str]) -> Any:
        self.last_result = result
        token_usage = dict(self._agent_token_usage)
        if result is not None:
            try:
                setattr(result, "agent_token_usage", token_usage)
            except Exception:
                pass
        if self._active_context is not None:
            self._persist_run(agent_token_usage=token_usage)
        self.last_execution = AgentHarnessExecutionMetadata(
            harness_run_id=str(_value(self._active_context.harness_run, "harness_run_id", ""))
            if self._active_context is not None else "",
            effective_executor=self._effective_executor,
            degraded_reasons=list(self.degraded_reasons),
            agent_thread_ids=dict(self.agent_thread_ids),
            fallback_field_ids=list(pending) if self._effective_executor == "authoring_graph" else [],
            agent_token_usage=token_usage,
        )
        _attach_result_metadata(result, self.last_execution)
        return result

    # ── Task 6 bounded tool boundary ───────────────────────────────────────

    def _reset_agent_state(self) -> None:
        """Reset only per-execution state; the frozen policy remains intact."""

        self._tool_call_count = 0
        self._proposal_attempts = {}
        self._retrieval_attempts = {}
        self._accepted_drafts = {}
        self._pending_proposals = {}
        self._field_outcomes = {}
        self._retrieval_ledger = []
        self._missing_statuses = {}
        self._waiting_fields = set()
        self._evidence_registry = {}
        self._evidence_payloads = {}
        self._tool_call_sequence = 0
        self._execution_events = []
        self._agent_token_usage = {
            "usage_returned": False,
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "call_count": 0,
        }

    def _current_context(self) -> HarnessExecutionContext:
        if self._active_context is None:
            raise RuntimeError("agent tool requires an active HarnessExecutionContext")
        return self._active_context

    def _field_schema(self, field_id: str) -> tuple[str, Any]:
        normalized, field_error = self._tool_field_status(field_id)
        if field_error is not None:
            raise KeyError(normalized)
        context = self._current_context()
        for item in _value(context.schema, "fields", ()) or ():
            if str(_value(item, "field_id", "")).strip() == normalized:
                return normalized, item
        raise KeyError(normalized)

    @staticmethod
    def _issue(code: str, message: str, *, field_id: str | None = None, retryable: bool = False) -> Any:
        from src.document_authoring.harness.agent_contracts import ToolIssue

        return ToolIssue(code=code, message=message[:500], field_id=field_id, retryable=retryable)

    @staticmethod
    def _issue_codes(issues: Sequence[Any]) -> list[str]:
        return [str(_value(issue, "code", "unknown")) for issue in issues]

    def _next_action_key(
        self,
        operation: str,
        field_id: str | None = None,
        *,
        attempt: int = 1,
        input_hash: str | None = None,
    ) -> str:
        context = self._current_context()
        self._tool_call_sequence += 1
        run_id = str(_value(context.harness_run, "harness_run_id", ""))
        work_order = context.work_order
        fingerprint = input_hash or str(_value(work_order, "input_fingerprint", ""))
        return receipt_action_key(
            harness_run_id=run_id,
            node_name="agent_field_harness",
            unit_id=field_id or "run",
            attempt=max(1, int(attempt)),
            input_fingerprint=fingerprint,
            action={
                "version": "v1",
                "operation": operation,
                "call_index": self._tool_call_sequence,
            },
        )

    @staticmethod
    def _safe_event_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """Keep event payloads bounded and free of source/model contents."""

        forbidden = {"prompt", "content", "evidence_content", "credential", "password", "api_key"}

        def clean(value: Any, key: str | None = None) -> Any:
            if key is not None and key.casefold() in forbidden:
                return None
            if value is None or isinstance(value, (str, int, bool, float)):
                return str(value)[:500] if isinstance(value, str) else value
            if isinstance(value, Mapping):
                return {
                    str(item_key): clean(item_value, str(item_key))
                    for item_key, item_value in list(value.items())[:32]
                    if str(item_key).casefold() not in forbidden
                }
            if isinstance(value, (list, tuple, set)):
                return [clean(item) for item in list(value)[:32]]
            return str(value)[:200]

        return clean(dict(payload or {}))

    def _append_event(
        self,
        event_type: str,
        *,
        action_key: str,
        status: str = "succeeded",
        field_id: str | None = None,
        unit_id: str | None = None,
        tool_name: str | None = None,
        attempt: int = 1,
        error_code: str | None = None,
        retryable: bool = False,
        duration_seconds: float | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        context = self._current_context()
        run = context.harness_run
        event = AuthoringExecutionEvent(
            event_id=f"authoring-event-{uuid.uuid4().hex}",
            event_type=event_type,
            tenant_id=str(_value(run, "tenant_id", _value(context.work_order, "tenant_id", "default"))),
            work_order_id=str(_value(context.work_order, "work_order_id", "")),
            harness_run_id=str(_value(run, "harness_run_id", "")),
            idempotency_key=execution_event_key(action_key, event_type),
            attempt=max(1, int(attempt)),
            executor="agent_field_harness",
            node_name="agent_field_harness",
            tool_name=tool_name,
            field_id=field_id,
            unit_id=unit_id or (f"field:{field_id}" if field_id else None),
            status=status,
            error_code=error_code,
            retryable=retryable,
            trace_id=_value(run, "trace_id", None),
            duration_seconds=duration_seconds,
            sanitized_payload=self._safe_event_payload(payload),
        )
        callback = _value(context.extra, "append_execution_event", None)
        stored = event
        if callable(callback):
            stored = callback(event) or event
        else:
            store = _value(context.extra, "store", None)
            append = getattr(store, "append_execution_event", None)
            if callable(append):
                stored = append(event)
        self._execution_events.append(stored)
        return stored

    def _persist_run(self, **updates: Any) -> None:
        context = self._current_context()
        run = context.harness_run
        for key, value in updates.items():
            try:
                setattr(run, key, value)
            except Exception:
                try:
                    object.__setattr__(run, key, value)
                except Exception:
                    pass
        callback = _value(context.extra, "persist_run", None)
        if callable(callback):
            try:
                callback(run, **updates)
            except TypeError:
                callback(run, updates)
        self._notify_run_update(run)

    def _check_lease(self) -> None:
        context = self._current_context()
        callback = _value(context.extra, "check_lease", None)
        if not callable(callback):
            return
        try:
            valid = callback()
        except Exception as exc:
            from src.document_authoring.harness.policy import HarnessLeaseLost

            raise HarnessLeaseLost("agent lease/fencing check failed") from exc
        if valid is False:
            from src.document_authoring.harness.policy import HarnessLeaseLost

            raise HarnessLeaseLost("agent lease/fencing token is stale")

    def _record_proposal_rejection(
        self,
        field_id: str,
        *,
        attempt: int,
        error_code: str,
        issue_codes: Sequence[str] = (),
        evidence_ids: Sequence[str] = (),
        proposal_hash: str | None = None,
    ) -> None:
        """Persist one replay-safe business fact for a rejected proposal."""

        action_key = self._next_action_key(
            "proposal_rejected",
            field_id,
            attempt=attempt,
            input_hash=proposal_hash,
        )
        payload: dict[str, Any] = {
            "issue_codes": [str(code) for code in issue_codes if str(code)],
            "evidence_ids": [str(value) for value in evidence_ids if str(value)],
        }
        if proposal_hash:
            payload["proposal_hash"] = str(proposal_hash)
        self._append_event(
            "proposal_rejected",
            action_key=action_key,
            status="rejected",
            field_id=field_id,
            attempt=attempt,
            error_code=error_code,
            retryable=error_code in {ERROR_EVIDENCE_UNAVAILABLE, "draft_validation_failed"},
            payload=payload,
        )

    def _run_tool(
        self,
        tool_name: str,
        field_id: str,
        result_type: Any,
        operation: Callable[[str], Any],
        *,
        attempt: int = 1,
        input_hash: str | None = None,
    ) -> Any:
        normalized, field_error = self._tool_field_status(field_id)
        if field_error is not None:
            return result_type(
                status="rejected", field_id=normalized, error_code=ERROR_FIELD_NOT_REGISTERED,
                issues=[self._issue(ERROR_FIELD_NOT_REGISTERED, "field is not registered in the frozen schema", field_id=normalized)],
            )
        try:
            self.require_agent_tool(tool_name)
        except AgentToolNotAllowed:
            return result_type(
                status="rejected", field_id=normalized, error_code=ERROR_AGENT_TOOL_NOT_ALLOWED,
                issues=[self._issue(ERROR_AGENT_TOOL_NOT_ALLOWED, "tool is not in the frozen agent allowlist", field_id=normalized)],
            )
        policy = _value(self.policy, "policy", self.policy)
        max_calls = int(_value(policy, "max_agent_tool_calls", 60) or 60)
        if self._tool_call_count >= max_calls:
            self.degraded_reasons = _unique_reasons([*self.degraded_reasons, REASON_AGENT_TOOL_BUDGET_EXHAUSTED])
            action_key = self._next_action_key(tool_name, normalized, attempt=attempt, input_hash=input_hash)
            self._append_event(
                "tool_rejected", action_key=action_key, status="rejected", field_id=normalized,
                tool_name=tool_name, attempt=attempt, error_code=ERROR_AGENT_TOOL_BUDGET,
                payload={"issue_codes": [ERROR_AGENT_TOOL_BUDGET]},
            )
            return result_type(
                status="rejected", field_id=normalized, error_code=ERROR_AGENT_TOOL_BUDGET,
                issues=[self._issue(ERROR_AGENT_TOOL_BUDGET, "agent tool-call budget is exhausted", field_id=normalized)],
            )
        self._tool_call_count += 1
        action_key = self._next_action_key(tool_name, normalized, attempt=attempt, input_hash=input_hash)
        self._append_event(
            "tool_called", action_key=action_key, status="started", field_id=normalized,
            tool_name=tool_name, attempt=attempt,
        )
        started = time.monotonic()
        status = "succeeded"
        error_code = None
        retryable = False
        try:
            self._check_lease()
            with observe.tool(
                "hdb.authoring.agent.tool",
                operation=tool_name,
                field_id=normalized,
            ) as observation:
                result = operation(normalized)
                observation.outcome(str(_value(result, "status", "succeeded")))
        except AgentToolNotAllowed:
            status = "rejected"
            error_code = ERROR_AGENT_TOOL_NOT_ALLOWED
            result = result_type(
                status="rejected", field_id=normalized, error_code=error_code,
                issues=[self._issue(error_code, "tool is not allowlisted", field_id=normalized)],
            )
        except PermissionError as exc:
            status = "rejected"
            error_code = getattr(exc, "error_code", ERROR_EVIDENCE_UNAVAILABLE)
            result = result_type(
                status="rejected", field_id=normalized, error_code=error_code,
                issues=[self._issue(error_code, "tool operation was rejected by the coordinator", field_id=normalized)],
            )
        except Exception as exc:
            from src.document_authoring.harness.policy import HarnessLeaseLost

            if isinstance(exc, HarnessLeaseLost):
                raise
            status = "unavailable"
            error_code = getattr(exc, "error_code", REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE)
            result = result_type(
                status="unavailable", field_id=normalized, error_code=error_code,
                issues=[self._issue(error_code, "tool operation is temporarily unavailable", field_id=normalized, retryable=True)],
            )
            retryable = True
        duration = time.monotonic() - started
        record_authoring_tool(tool=tool_name, status=status, duration_s=duration)
        event_type = "tool_succeeded" if status == "succeeded" else "tool_rejected"
        # ``unavailable`` is a tool-result status, while the append-only
        # execution-event contract deliberately uses the smaller lifecycle
        # vocabulary.  Preserve the distinction in the result/error code and
        # record infrastructure unavailability as a failed tool attempt.
        event_status = status if status in {"succeeded", "rejected"} else "failed"
        self._append_event(
            event_type, action_key=action_key, status=event_status, field_id=normalized,
            tool_name=tool_name, attempt=attempt, error_code=error_code,
            retryable=retryable, duration_seconds=duration,
            payload={
                "status": str(_value(result, "status", status)),
                "error_code": _value(result, "error_code", error_code),
                "issue_codes": self._issue_codes(_value(result, "issues", []) or []),
            },
        )
        return result

    def _build_requirement(self, field_id: str) -> Any:
        """Build the same bounded requirement used by the fixed graph."""

        context = self._current_context()
        if context.snapshot is None:
            raise AgentInfrastructureUnavailable("frozen source snapshot is unavailable")
        from src.document_authoring.harness.graph import _requirement_for_unit

        _normalized, field = self._field_schema(field_id)
        return _requirement_for_unit(
            {"unit_id": f"field:{_normalized}", "kind": "field", "schema": field},
            context.work_order,
            context.snapshot,
        )

    def _registry_entry_for_storage(self, evidence: Any) -> EvidenceRegistryEntry:
        context = self._current_context()
        run = context.harness_run
        order = context.work_order
        snapshot = context.snapshot
        evidence_id = str(_value(evidence, "id", "")).strip()
        if not evidence_id:
            raise EvidenceAccessError(ERROR_EVIDENCE_UNAVAILABLE, "retrieved evidence has no stable id")
        raw_content = str(_value(evidence, "content", "") or "")
        digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        source_name = str(_value(evidence, "source_name", "unknown-source") or "unknown-source")[:300]
        source_version = str(_value(evidence, "source_version_id", "") or "")[:200]
        source_identity = f"{source_name}|{source_version}|{evidence_id}"[:700]
        scope_type = str(_value(order, "scope_type", "project"))
        if scope_type == "knowledge_base":
            knowledge_base_id = str(_value(order, "knowledge_base_id", "") or "kb:" + str(_value(order, "knowledge_base_name", "")))
            project_id = None
        else:
            project_id = str(_value(order, "project_id", "") or "project:unknown")
            knowledge_base_id = None
        summary = f"source={source_name}; chars={len(raw_content)}; evidence={evidence_id}"
        return EvidenceRegistryEntry(
            evidence_id=evidence_id,
            tenant_id=str(_value(run, "tenant_id", _value(order, "tenant_id", "default"))),
            harness_run_id=str(_value(run, "harness_run_id", "")),
            work_order_id=str(_value(order, "work_order_id", "")),
            knowledge_base_id=knowledge_base_id,
            project_id=project_id,
            source_set_snapshot_id=str(_value(snapshot, "source_set_snapshot_id", "")),
            snapshot_content_hash=str(_value(snapshot, "content_hash", "")),
            content_hash=digest,
            source_identity=source_identity,
            redacted_summary=summary[:1000],
            reload_handle="evidence-handle-" + digest,
        )

    def _register_evidence(self, evidence: Any) -> EvidenceRegistryEntry:
        entry = self._registry_entry_for_storage(evidence)
        existing = self._evidence_registry.get(entry.evidence_id)
        if existing is not None:
            if existing.content_hash != entry.content_hash:
                raise EvidenceAccessError(ERROR_EVIDENCE_UNAVAILABLE, "evidence registry entry changed")
            return existing
        context = self._current_context()
        store = _value(context.extra, "evidence_store", _value(context.extra, "store", None))
        register = getattr(store, "register_evidence", None)
        stored = register(entry) if callable(register) else entry
        self._evidence_registry[entry.evidence_id] = stored
        self._evidence_payloads[entry.evidence_id] = {
            "id": entry.evidence_id,
            "content": str(_value(evidence, "content", "") or ""),
            "source_name": str(_value(evidence, "source_name", "") or ""),
            "score": _value(evidence, "score", 0),
            "metadata": dict(_value(evidence, "metadata", {}) or {}),
            "locator": dict(_value(evidence, "locator", {}) or {}),
        }
        return stored

    def _get_registry_entry(self, evidence_id: str) -> EvidenceRegistryEntry | None:
        entry = self._evidence_registry.get(str(evidence_id))
        if entry is not None:
            return entry
        context = self._current_context()
        store = _value(context.extra, "evidence_store", _value(context.extra, "store", None))
        getter = getattr(store, "get_evidence_entry", None)
        if callable(getter):
            entry = getter(str(_value(context.harness_run, "harness_run_id", "")), str(evidence_id))
            if entry is not None:
                self._evidence_registry[str(evidence_id)] = entry
        return entry

    def _load_evidence_payload(self, evidence_id: str) -> dict[str, Any] | None:
        context = self._current_context()
        loader = _value(context.extra, "load_evidence", None)
        if callable(loader):
            payload = loader(str(evidence_id))
            if isinstance(payload, Mapping):
                return dict(payload)
        # The original retrieval result remains request-scoped.  It is not put
        # in LangGraph state, but it is available to the coordinator for the
        # immediate deterministic validator pass.
        return self._evidence_payloads.get(str(evidence_id))

    def _validate_agent_draft(
        self,
        draft: DocumentUnitDraft,
        evidence_by_id: dict[str, dict[str, Any]],
        expected_type: str,
    ) -> DocumentUnitDraft:
        validated = self.validator.validate_unit_draft(draft, evidence_by_id)
        validated = self.validator.validate_typed_field_draft(
            validated, evidence_by_id, expected_value_type=expected_type,
        )
        context = self._current_context()
        contamination = self.validator.detect_template_contamination(
            validated, list(context.legacy_claims),
        )
        if contamination:
            validated = validated.model_copy(update={
                "validation_status": "requires_human",
                "validation_notes": [*validated.validation_notes, "template contamination detected"],
            })
        return validated

    @staticmethod
    def _proposal_typed_value(value: Any, value_type: str, evidence_ids: list[str]) -> TypedFieldValue:
        kind = _expected_agent_typed_kind(value_type)
        if kind is None:
            raise ValueError("unsupported value type")
        if kind == "scalar":
            if isinstance(value, (dict, list, tuple, set)):
                raise TypeError("scalar proposal must be a scalar")
            display = str(value if value is not None else "").strip()
            if not display:
                raise ValueError("scalar proposal cannot be empty")
            normalized = [display]
        else:
            values = list(value) if isinstance(value, (list, tuple, set)) else [value]
            normalized = list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
            if not normalized:
                raise ValueError("enumeration proposal cannot be empty")
            display = ", ".join(normalized)
        return TypedFieldValue(
            kind=kind, normalized_values=normalized, display_value=display,
            evidence_ids=list(evidence_ids),
        )

    def _build_agent_result(self) -> Any:
        from src.document_authoring.harness.graph import HarnessExecutionResult

        context = self._current_context()
        statuses = dict(_value(context.harness_run, "unit_statuses", {}) or {})
        for field_id in context.field_ids:
            normalized = str(field_id).removeprefix("field:")
            unit_id = f"field:{normalized}"
            if normalized in self._accepted_drafts:
                statuses[unit_id] = "ready_to_render"
            elif normalized in self._missing_statuses:
                statuses[unit_id] = self._missing_statuses[normalized]
            elif normalized in self._waiting_fields:
                statuses[unit_id] = "requires_human"
        result = HarnessExecutionResult(
            outcomes=dict(self._field_outcomes),
            retrieval_ledger=list(self._retrieval_ledger),
            drafts=[*self._accepted_drafts.values(), *self._pending_proposals.values()],
            unit_statuses=statuses,
            issues=[
                {"kind": "agent_degraded", "reason": reason}
                for reason in self.degraded_reasons
            ],
            step_count=self._tool_call_count,
            retrieval_round_count=sum(self._retrieval_attempts.values()),
            agent_token_usage=dict(self._agent_token_usage),
        )
        result.matrix_rows = [
            {
                "field_id": field_id,
                "coverage_status": "supported" if field_id in self._accepted_drafts else "missing",
                "evidence_ids": list(getattr(draft, "evidence_ids", [])) if draft is not None else [],
            }
            for field_id, draft in self._accepted_drafts.items()
        ]
        return result

    def _tool_field_status(self, field_id: str) -> tuple[str, str | None]:
        normalized = str(field_id).strip().removeprefix("field:")
        context = self._active_context
        if context is not None and normalized not in _schema_field_ids(context.schema):
            return normalized, "field_not_registered"
        return normalized, None

    def _tool_unavailable(self, tool_name: str, field_id: str, result_type: Any) -> Any:
        normalized, field_error = self._tool_field_status(field_id)
        if field_error is not None:
            status = "rejected"
            error_code = field_error
        elif not self.agent_tools_implemented:
            status = "unavailable"
            error_code = REASON_AGENT_TOOLS_NOT_IMPLEMENTED
        else:
            try:
                self.require_agent_tool(tool_name)
            except AgentToolNotAllowed:
                status = "rejected"
                error_code = ERROR_AGENT_TOOL_NOT_ALLOWED
            else:
                status = "unavailable"
                error_code = REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE
        return result_type(status=status, field_id=normalized, error_code=error_code)

    def read_field_brief(self, field_id: str) -> Any:
        from src.document_authoring.harness.agent_contracts import FieldBriefResult

        def operation(normalized: str) -> Any:
            _normalized, field = self._field_schema(normalized)
            context = self._current_context()
            brief = dict(_value(context.work_order, "generation_brief", {}) or {})
            summaries = [
                dict(row)
                for row in self._retrieval_ledger
                if str(row.get("unit_id")) in {normalized, f"field:{normalized}"}
            ]
            policy = _value(self.policy, "policy", self.policy)
            return FieldBriefResult(
                status="succeeded",
                field_id=normalized,
                field_contract={
                    "field_id": normalized,
                    "label": _value(field, "label", normalized),
                    "description": _value(field, "description", ""),
                    "required": bool(_value(field, "required", True)),
                    "value_type": _value(field, "value_type", "text"),
                    "missing_policy": _value(field, "missing_policy", "mark_tbd"),
                    "allow_derivation": bool(_value(field, "allow_derivation", False)),
                    "required_capabilities": list(_value(field, "required_capabilities", []) or []),
                    "preferred_source_roles": list(_value(field, "preferred_source_roles", []) or []),
                },
                evidence_summaries=summaries[:10],
                brief_constraints={
                    "missing_data_policy": brief.get("missing_data_policy"),
                    "inference_policy": brief.get("inference_policy"),
                    "allowed_derivations": list(brief.get("allowed_derivations") or []),
                    "max_proposal_retries_per_field": int(_value(policy, "max_proposal_retries_per_field", 2) or 2),
                },
            )

        return self._run_tool("read_field_brief", field_id, FieldBriefResult, operation)

    def retrieve_evidence(
        self,
        field_id: str,
        query: str,
        top_k: int = 5,
        retriever_kind: str = "default",
    ) -> Any:
        from src.document_authoring.harness.agent_contracts import EvidenceRetrievalResult

        def operation(normalized: str) -> Any:
            from src.agents.claim_evidence import RetrievalOutcome

            _normalized, field = self._field_schema(normalized)
            del field
            context = self._current_context()
            policy = _value(self.policy, "policy", self.policy)
            current_attempt = self._retrieval_attempts.get(normalized, 0) + 1
            max_attempts = int(_value(policy, "max_retrieval_attempts_per_unit", 2) or 2)
            if current_attempt > max_attempts:
                return EvidenceRetrievalResult(
                    status="rejected", field_id=normalized, error_code=ERROR_AGENT_TOOL_BUDGET,
                    issues=[self._issue(ERROR_AGENT_TOOL_BUDGET, "retrieval retry budget is exhausted", field_id=normalized)],
                )
            self._retrieval_attempts[normalized] = current_attempt
            requirement = self._build_requirement(normalized)
            target = context.retrieve
            if not callable(target):
                raise AgentInfrastructureUnavailable("retrieval provider is unavailable")
            query_text = str(query or "").strip()[:2000]
            if not query_text:
                query_text = " ".join(requirement.retrieval_query_terms)
            outcome = _call_candidates(
                target,
                [
                    ((requirement, current_attempt, query_text), {}),
                    ((requirement, current_attempt, query_text, False), {}),
                    ((), {"requirement": requirement, "attempt": current_attempt, "query_override": query_text}),
                    ((requirement,), {"attempt": current_attempt, "query_override": query_text}),
                ],
            )
            if not isinstance(outcome, RetrievalOutcome):
                outcome = RetrievalOutcome.model_validate(outcome)
            snapshot_id = str(_value(context.snapshot, "source_set_snapshot_id", ""))
            if outcome.applied_source_set_snapshot_id != snapshot_id:
                raise EvidenceAccessError(
                    "evidence_snapshot_mismatch",
                    "retrieval outcome was not produced for this frozen snapshot",
                )
            self._field_outcomes[normalized] = outcome
            refs = []
            for evidence in list(outcome.evidences or [])[:max(1, min(int(top_k), 10))]:
                entry = self._register_evidence(evidence)
                refs.append({
                    "evidence_id": entry.evidence_id,
                    "registry_run_id": entry.harness_run_id,
                    "snapshot_id": entry.source_set_snapshot_id,
                    "content_hash": entry.content_hash,
                })
            source_summaries = [
                {
                    "source": str(_value(source, "source_version_id", ""))[:160],
                    "status": str(_value(source, "status", "")),
                    "hit_count": len(_value(source, "evidence_ids", []) or []),
                }
                for source in (outcome.source_outcomes or [])
            ]
            ledger = {
                "unit_id": f"field:{normalized}",
                "attempt": current_attempt,
                "query_hash": hashlib.sha256(query_text.encode("utf-8")).hexdigest(),
                "retriever_kind": str(retriever_kind or "default")[:80],
                "status": outcome.status,
                "evidence_ids": [ref["evidence_id"] for ref in refs],
                "sources": source_summaries,
            }
            self._retrieval_ledger.append(ledger)
            return EvidenceRetrievalResult(
                status="succeeded",
                field_id=normalized,
                error_code=None if refs else ERROR_EVIDENCE_UNAVAILABLE,
                issues=[] if refs else [self._issue(ERROR_EVIDENCE_UNAVAILABLE, "no evidence was found in the frozen source scope", field_id=normalized, retryable=outcome.status == "success_empty")],
                evidence_refs=refs,
                truncated_summary="; ".join(
                    f"{row['source']}:{row['hit_count']}" for row in source_summaries
                )[:1000],
            )

        return self._run_tool(
            "retrieve_evidence", field_id, EvidenceRetrievalResult, operation,
            input_hash=hashlib.sha256(str(query or "").strip().encode("utf-8")).hexdigest(),
        )

    def propose_field_value(
        self,
        field_id: str,
        value: Any,
        value_type: str,
        evidence_ids: Sequence[str],
        note: str = "",
        confidence: float = 0.0,
    ) -> Any:
        from src.document_authoring.harness.agent_contracts import FieldProposalResult

        def operation(normalized: str) -> Any:
            _normalized, field = self._field_schema(normalized)
            context = self._current_context()
            policy = _value(self.policy, "policy", self.policy)
            attempt = self._proposal_attempts.get(normalized, 0) + 1
            self._proposal_attempts[normalized] = attempt
            max_retries = int(_value(policy, "max_proposal_retries_per_field", 2) or 2)
            if attempt > max_retries + 1:
                issue = self._issue(ERROR_PROPOSAL_BUDGET, "proposal retry budget is exhausted", field_id=normalized)
                self._record_proposal_rejection(
                    normalized,
                    attempt=attempt,
                    error_code=ERROR_PROPOSAL_BUDGET,
                    issue_codes=[ERROR_PROPOSAL_BUDGET],
                )
                return FieldProposalResult(
                    status="rejected", field_id=normalized, error_code=ERROR_PROPOSAL_BUDGET,
                    issues=[issue], validation_status="unsupported",
                )
            expected_type = str(_value(field, "value_type", "text"))
            supplied_type = str(value_type or "").strip()
            if _expected_agent_typed_kind(supplied_type) != _expected_agent_typed_kind(expected_type):
                issue = self._issue(ERROR_VALUE_TYPE_MISMATCH, "proposal value_type does not match the frozen field contract", field_id=normalized)
                self._record_proposal_rejection(
                    normalized,
                    attempt=attempt,
                    error_code=ERROR_VALUE_TYPE_MISMATCH,
                    issue_codes=[ERROR_VALUE_TYPE_MISMATCH],
                )
                return FieldProposalResult(
                    status="rejected", field_id=normalized, error_code=ERROR_VALUE_TYPE_MISMATCH,
                    issues=[issue], validation_status="unsupported",
                )
            ids = list(dict.fromkeys(str(item).strip() for item in (evidence_ids or []) if str(item).strip()))
            if not ids:
                issue = self._issue(ERROR_EVIDENCE_UNAVAILABLE, "proposal must cite registered evidence", field_id=normalized, retryable=True)
                self._record_proposal_rejection(
                    normalized,
                    attempt=attempt,
                    error_code=ERROR_EVIDENCE_UNAVAILABLE,
                    issue_codes=[ERROR_EVIDENCE_UNAVAILABLE],
                )
                return FieldProposalResult(
                    status="rejected", field_id=normalized, error_code=ERROR_EVIDENCE_UNAVAILABLE,
                    issues=[issue], validation_status="unsupported",
                )
            evidence_by_id: dict[str, dict[str, Any]] = {}
            for evidence_id in ids:
                entry = self._get_registry_entry(evidence_id)
                try:
                    validate_evidence_access(
                        entry,
                        tenant_id=str(_value(context.harness_run, "tenant_id", _value(context.work_order, "tenant_id", "default"))),
                        harness_run_id=str(_value(context.harness_run, "harness_run_id", "")),
                        source_set_snapshot_id=str(_value(context.snapshot, "source_set_snapshot_id", "")),
                    )
                except EvidenceAccessError as exc:
                    issue = self._issue(getattr(exc, "error_code", ERROR_EVIDENCE_UNAVAILABLE), "evidence reference is not valid for this run", field_id=normalized)
                    self._record_proposal_rejection(
                        normalized,
                        attempt=attempt,
                        error_code=issue.code,
                        issue_codes=[issue.code],
                        evidence_ids=ids,
                    )
                    return FieldProposalResult(
                        status="rejected", field_id=normalized, error_code=issue.code,
                        issues=[issue], validation_status="unsupported",
                    )
                payload = self._evidence_payloads.get(evidence_id)
                if payload is None:
                    payload = self._load_evidence_payload(evidence_id)
                if payload is None:
                    issue = self._issue(ERROR_EVIDENCE_UNAVAILABLE, "registered evidence cannot be reloaded", field_id=normalized)
                    self._record_proposal_rejection(
                        normalized,
                        attempt=attempt,
                        error_code=ERROR_EVIDENCE_UNAVAILABLE,
                        issue_codes=[ERROR_EVIDENCE_UNAVAILABLE],
                        evidence_ids=ids,
                    )
                    return FieldProposalResult(
                        status="rejected", field_id=normalized, error_code=ERROR_EVIDENCE_UNAVAILABLE,
                        issues=[issue], validation_status="unsupported",
                    )
                evidence_by_id[evidence_id] = payload
            try:
                typed = self._proposal_typed_value(value, supplied_type, ids)
                display = typed.display_value
                proposal_value_hash = content_hash({"value": value, "value_type": supplied_type, "evidence_ids": ids})
                draft = DocumentUnitDraft(
                    unit_id=f"field:{normalized}",
                    run_id=str(_value(context.harness_run, "harness_run_id", "")),
                    generated_by="external_agent",
                    content=str(note or display)[:4000],
                    proposed_value=value,
                    typed_value=typed,
                    assertions=[DraftAssertion(
                        assertion_id=f"agent-assertion-{proposal_value_hash[:20]}",
                        text=display,
                        claim_id=f"agent-claim-{proposal_value_hash[:20]}",
                        value=value,
                        evidence_ids=ids,
                    )],
                    evidence_ids=ids,
                    metadata={"agent_proposal": True, "proposal_attempt": attempt},
                )
            except (TypeError, ValueError):
                issue = self._issue(ERROR_VALUE_TYPE_MISMATCH, "proposal value is not compatible with the frozen type contract", field_id=normalized)
                self._record_proposal_rejection(
                    normalized,
                    attempt=attempt,
                    error_code=ERROR_VALUE_TYPE_MISMATCH,
                    issue_codes=[ERROR_VALUE_TYPE_MISMATCH],
                    evidence_ids=ids,
                )
                return FieldProposalResult(
                    status="rejected", field_id=normalized, error_code=ERROR_VALUE_TYPE_MISMATCH,
                    issues=[issue], validation_status="unsupported",
                )
            validated = self._validate_agent_draft(draft, evidence_by_id, expected_type)
            if validated.validation_status != "supported":
                issues = [
                    self._issue("draft_validation_failed", str(message), field_id=normalized, retryable=True)
                    for message in validated.validation_notes[:10]
                ] or [self._issue("draft_validation_failed", "proposal failed deterministic validation", field_id=normalized, retryable=True)]
                self._record_proposal_rejection(
                    normalized,
                    attempt=attempt,
                    error_code="draft_validation_failed",
                    issue_codes=self._issue_codes(issues),
                    evidence_ids=ids,
                    proposal_hash=content_hash(validated),
                )
                return FieldProposalResult(
                    status="rejected", field_id=normalized, error_code="draft_validation_failed",
                    issues=issues, proposal_hash=content_hash(validated), validation_status="unsupported",
                )
            proposal_hash = content_hash(validated)
            threshold = float(_value(policy, "min_agent_confidence", _value(policy, "agent_confidence_threshold", 0.7)) or 0.7)
            self._append_event(
                "proposal_submitted", action_key=self._next_action_key("propose_field_value", normalized, attempt=attempt, input_hash=proposal_hash),
                status="started", field_id=normalized, attempt=attempt,
                payload={"proposal_hash": proposal_hash, "evidence_ids": ids},
            )
            self._check_lease()
            if float(confidence) < threshold:
                pending_event_id = f"pending-agent-proposal-{proposal_hash[:32]}"
                expires = datetime.now(timezone.utc) + timedelta(minutes=30)
                pending = {
                    "event_id": pending_event_id,
                    "proposal_hash": proposal_hash,
                    "field_id": normalized,
                    "evidence_ids": ids,
                    "issues": [],
                    "action_schema": {"type": "object", "properties": {"decision": {"enum": ["approve", "reject"]}}, "required": ["decision"]},
                    "actor_scope": {"tenant_id": str(_value(context.harness_run, "tenant_id", "default")), "user_id": str(_value(context.work_order, "created_by", ""))},
                    "expires_at": expires.isoformat(),
                }
                self._pending_proposals[normalized] = validated.model_copy(update={
                    "validation_status": "requires_human",
                    "validation_notes": [
                        *validated.validation_notes,
                        "proposal confidence is below the frozen human-review threshold",
                    ],
                })
                self._waiting_fields.add(normalized)
                self._persist_run(
                    status="waiting_human", current_node="await_human", pending_human_event=pending,
                    unit_statuses={**dict(_value(context.harness_run, "unit_statuses", {}) or {}), f"field:{normalized}": "requires_human"},
                )
                self._append_event(
                    "human_waiting", action_key=self._next_action_key("human_waiting", normalized, attempt=attempt, input_hash=proposal_hash),
                    status="waiting", field_id=normalized, attempt=attempt,
                    error_code=ERROR_PROPOSAL_REQUIRES_HUMAN,
                    payload={"pending_event_id": pending_event_id, "proposal_hash": proposal_hash, "evidence_ids": ids},
                )
                return FieldProposalResult(
                    status="waiting_human", field_id=normalized, error_code=ERROR_PROPOSAL_REQUIRES_HUMAN,
                    issues=[self._issue(ERROR_PROPOSAL_REQUIRES_HUMAN, "proposal confidence is below the frozen human-review threshold", field_id=normalized)],
                    proposal_hash=proposal_hash, validation_status="requires_human", waiting_human=True,
                )
            validated = validated.model_copy(update={"metadata": {**validated.metadata, "confidence": float(confidence)}})
            self._accepted_drafts[normalized] = validated
            self._persist_run(
                unit_statuses={**dict(_value(context.harness_run, "unit_statuses", {}) or {}), f"field:{normalized}": "ready_to_render"},
                current_node="agent_field_harness",
            )
            action_key = self._next_action_key("proposal_accepted", normalized, attempt=attempt, input_hash=proposal_hash)
            self._append_event(
                "proposal_accepted", action_key=action_key, status="succeeded", field_id=normalized, attempt=attempt,
                payload={"proposal_hash": proposal_hash, "evidence_ids": ids, "confidence": float(confidence)},
            )
            self._append_event(
                "draft_persisted", action_key=action_key, status="succeeded", field_id=normalized, attempt=attempt,
                payload={"proposal_hash": proposal_hash, "evidence_ids": ids},
            )
            return FieldProposalResult(
                status="succeeded", field_id=normalized, proposal_hash=proposal_hash,
                validation_status="supported", accepted=True,
            )

        return self._run_tool(
            "propose_field_value", field_id, FieldProposalResult, operation,
            attempt=self._proposal_attempts.get(str(field_id).strip().removeprefix("field:"), 0) + 1,
            input_hash=hashlib.sha256(canonical_json({"value": value, "value_type": value_type, "evidence_ids": list(evidence_ids or [])}).encode("utf-8")).hexdigest(),
        )

    def mark_missing(self, field_id: str, reason: str) -> Any:
        from src.document_authoring.harness.agent_contracts import MissingFieldResult

        def operation(normalized: str) -> Any:
            _normalized, field = self._field_schema(normalized)
            context = self._current_context()
            brief = dict(_value(context.work_order, "generation_brief", {}) or {})
            from src.document_authoring.harness.agent_contracts import effective_missing_policy

            policy = effective_missing_policy(
                brief.get("missing_data_policy"), _value(field, "missing_policy", "mark_tbd"),
            ) or "mark_tbd"
            status = {"block_generation": "blocked", "mark_tbd": "tbd", "keep_blank": "ready_to_render"}[policy]
            self._missing_statuses[normalized] = status
            self._persist_run(
                unit_statuses={**dict(_value(context.harness_run, "unit_statuses", {}) or {}), f"field:{normalized}": status},
                current_node="agent_field_harness",
            )
            action_key = self._next_action_key("mark_missing", normalized, input_hash=hashlib.sha256(str(reason or "").strip().encode("utf-8")).hexdigest())
            self._append_event(
                "missing_marked", action_key=action_key, status="succeeded", field_id=normalized,
                error_code=None if policy != "block_generation" else "block_generation_unresolved_missing",
                payload={"missing_policy": policy, "status": status, "reason_hash": hashlib.sha256(str(reason or "").encode("utf-8")).hexdigest()},
            )
            return MissingFieldResult(status="succeeded", field_id=normalized, missing_policy_applied=policy)

        return self._run_tool("mark_missing", field_id, MissingFieldResult, operation)


def _attach_result_metadata(result: Any, metadata: AgentHarnessExecutionMetadata) -> None:
    if result is None:
        return
    for key, value in {
        "effective_executor": metadata.effective_executor,
        "degraded_reasons": list(metadata.degraded_reasons),
        "agent_thread_ids": dict(metadata.agent_thread_ids),
        "fallback_field_ids": list(metadata.fallback_field_ids),
    }.items():
        try:
            setattr(result, key, value)
        except Exception:
            continue


def _empty_execution_result(harness_run: Any, schema: Any) -> Any:
    try:
        from src.document_authoring.harness.graph import HarnessExecutionResult

        statuses = dict(_value(harness_run, "unit_statuses", {}) or {})
        for field_id in _schema_field_ids(schema):
            statuses.setdefault(f"field:{field_id}", "committed")
        return HarnessExecutionResult(unit_statuses=statuses)
    except Exception:
        return None


def _merge_execution_results(results: Sequence[Any], harness_run: Any, schema: Any) -> Any:
    if not results:
        return _empty_execution_result(harness_run, schema)
    if len(results) == 1:
        return results[0]
    try:
        from src.document_authoring.harness.graph import HarnessExecutionResult

        if not all(isinstance(result, HarnessExecutionResult) for result in results):
            # A protocol adapter is allowed to return an application-specific
            # result.  Do not manufacture an empty graph result merely because
            # that result has no graph aggregation fields.
            return results[0]
        merged = HarnessExecutionResult()
        for result in results:
            for name in ("requirements", "outcomes", "unit_statuses"):
                value = getattr(result, name, None)
                if isinstance(value, Mapping):
                    getattr(merged, name).update(value)
            for name in ("matrix_rows", "retrieval_ledger", "drafts", "issues"):
                value = getattr(result, name, None)
                if isinstance(value, list):
                    getattr(merged, name).extend(value)
            merged.step_count += int(getattr(result, "step_count", 0) or 0)
            merged.retrieval_round_count += int(getattr(result, "retrieval_round_count", 0) or 0)
            usage = getattr(result, "agent_token_usage", None)
            if isinstance(usage, Mapping):
                merged_usage = merged.agent_token_usage
                merged_usage["usage_returned"] = bool(
                    merged_usage.get("usage_returned") or usage.get("usage_returned")
                )
                for name in ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"):
                    values = [merged_usage.get(name), usage.get(name)]
                    known = [int(value) for value in values if value is not None]
                    if known:
                        merged_usage[name] = sum(known)
        return merged
    except Exception:
        # A custom executor result may not be mergeable.  The first result is
        # still the most compatible return value; metadata records every
        # field that was sent to the fallback.
        return results[0]


def select_harness_executor(
    schema: Any,
    work_order: Any,
    *,
    policy: Any | None = None,
    harness_policy: Any | None = None,
    fallback_executor: Any | None = None,
    deterministic_executor: Any | None = None,
    requested_executor: str | None = None,
    agent_mode_enabled: bool | None = None,
    flag_enabled: bool | None = None,
    settings: Any | None = None,
    agent_infrastructure_available: Any | None = None,
    infrastructure_available: Any | None = None,
    agent_runner: Callable[..., Any] | None = None,
    agent_tools_implemented: bool = False,
    run_manifest: Any | None = None,
    manifest: Any | None = None,
    harness_run: Any | None = None,
    expected_policy_hash: str | None = None,
    fallback_policy: Any | None = None,
) -> ExecutorSelection:
    """Select the only allowed executor for one immutable execution request.

    Gate order is deliberate: contract and policy errors are raised before
    flag/infrastructure degradation is considered.  This prevents an invalid
    policy from being hidden by a disabled feature flag.
    """

    if policy is not None and harness_policy is not None and policy is not harness_policy:
        if _policy_hash(policy) != _policy_hash(harness_policy):
            raise HarnessPolicyError(
                "policy and harness_policy arguments disagree",
                error_code=ERROR_POLICY_BINDING_MISMATCH,
            )
    active_policy = policy if policy is not None else harness_policy
    active_manifest = run_manifest if run_manifest is not None else manifest
    mode = validate_execution_contract(
        schema=schema,
        work_order=work_order,
        requested_executor=requested_executor,
        harness_run=harness_run,
        run_manifest=active_manifest,
    )

    if mode == "deterministic_only":
        executor = DeterministicRuleExecutor(
            deterministic_executor if deterministic_executor is not None else fallback_executor,
        )
        selection = ExecutorSelection(
            executor=executor,
            requested_executor=mode,
            effective_executor="deterministic_rule",
            gates={
                "schema_order_requested_consistent": True,
                "feature_flag": False,
                "approved_policy": True,
                "agent_infrastructure": False,
            },
        )
        if harness_run is not None:
            selection.apply_to_run(harness_run)
        return selection

    validated_policy = validate_approved_harness_policy(
        active_policy,
        work_order=work_order,
        run_manifest=active_manifest,
        expected_policy_hash=expected_policy_hash,
        require_binding=True,
    )
    candidate_fallback_policy = (
        fallback_policy
        if fallback_policy is not None
        else _executor_policy(fallback_executor)
    )
    if candidate_fallback_policy is not None:
        validated_fallback_policy = validate_approved_harness_policy(
            candidate_fallback_policy,
            work_order=work_order,
            run_manifest=active_manifest,
            expected_policy_hash=expected_policy_hash,
            require_binding=True,
        )
        if _policy_hash(validated_fallback_policy) != _policy_hash(validated_policy):
            raise HarnessPolicyError(
                "fallback policy is not the same approved policy version",
                error_code=ERROR_POLICY_BINDING_MISMATCH,
            )

    graph_executor = _coerce_executor(fallback_executor, effective_executor="authoring_graph")
    if mode == "internal_harness":
        selection = ExecutorSelection(
            executor=graph_executor,
            requested_executor=mode,
            effective_executor="authoring_graph",
            gates={
                "schema_order_requested_consistent": True,
                "feature_flag": True,
                "approved_policy": True,
                "agent_infrastructure": True,
            },
        )
        if harness_run is not None:
            selection.apply_to_run(harness_run)
        return selection

    flag = _read_agent_mode_flag(
        agent_mode_enabled if agent_mode_enabled is not None else flag_enabled,
        settings,
    )
    infrastructure = _probe_infrastructure(
        agent_infrastructure_available
        if agent_infrastructure_available is not None
        else infrastructure_available
    )
    gates = {
        "schema_order_requested_consistent": True,
        "feature_flag": flag,
        "approved_policy": True,
        "agent_infrastructure": infrastructure,
    }
    reasons: list[str] = []
    if not flag:
        reasons.append(REASON_AGENT_MODE_DISABLED)
    if not infrastructure:
        reasons.append(REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE)
    if reasons:
        selection = ExecutorSelection(
            executor=graph_executor,
            requested_executor=mode,
            effective_executor="authoring_graph",
            degraded_reasons=_unique_reasons(reasons),
            gates=gates,
        )
        if harness_run is not None:
            selection.apply_to_run(harness_run)
        return selection

    agent_executor = AgentFieldHarness(
        graph_executor,
        policy=validated_policy,
        agent_runner=agent_runner,
        agent_tools_implemented=agent_tools_implemented,
    )
    selection = ExecutorSelection(
        executor=agent_executor,
        requested_executor=mode,
        effective_executor="agent_field_harness",
        gates=gates,
    )
    if harness_run is not None:
        selection.apply_to_run(harness_run)
    return selection


def select_executor(schema: Any, work_order: Any, **kwargs: Any) -> HarnessExecutor:
    """Return only the executor for legacy callers; metadata remains on it."""

    selection = select_harness_executor(schema, work_order, **kwargs)
    executor = selection.executor
    try:
        setattr(executor, "selection", selection)
        if not isinstance(executor, AgentFieldHarness):
            setattr(executor, "degraded_reasons", list(selection.degraded_reasons))
            setattr(executor, "gates", dict(selection.gates))
    except Exception:
        pass
    return executor


def resolve_harness_executor(schema: Any, work_order: Any, **kwargs: Any) -> ExecutorSelection:
    return select_harness_executor(schema, work_order, **kwargs)


choose_executor = select_executor


__all__ = [
    "AGENT_TOOL_ALLOWLIST",
    "EXECUTION_MODES",
    "FORBIDDEN_AGENT_TOOLS",
    "REQUIRED_EXCLUDED_AGENT_CAPABILITIES",
    "AgentFieldHarness",
    "AgentHarnessExecutionMetadata",
    "AgentInfrastructureUnavailable",
    "AgentToolNotAllowed",
    "AgentToolsNotImplemented",
    "ExecutionMode",
    "EffectiveExecutor",
    "ERROR_AGENT_PROFILE_INVALID",
    "ERROR_AGENT_TOOL_NOT_ALLOWED",
    "ERROR_EXECUTOR_MISMATCH",
    "ERROR_FALLBACK_UNAVAILABLE",
    "ERROR_POLICY_BINDING_MISMATCH",
    "ERROR_POLICY_EXPIRED",
    "ERROR_POLICY_HASH_MISMATCH",
    "ERROR_POLICY_NOT_APPROVED",
    "ERROR_POLICY_REQUIRED",
    "ERROR_POLICY_REVOKED",
    "ExecutorSelection",
    "FallbackExecutorUnavailable",
    "HarnessExecutionContext",
    "HarnessExecutor",
    "HarnessExecutorSelectionError",
    "HarnessExecutorSelection",
    "HarnessPolicyError",
    "InternalGraphExecutor",
    "DeterministicRuleExecutor",
    "REASON_AGENT_INFRASTRUCTURE_UNAVAILABLE",
    "REASON_AGENT_MODE_DISABLED",
    "REASON_AGENT_TOOLS_NOT_IMPLEMENTED",
    "agent_thread_id",
    "build_agent_thread_id",
    "choose_executor",
    "resolve_harness_executor",
    "select_executor",
    "select_harness_executor",
    "validate_agent_policy",
    "validate_approved_harness_policy",
    "validate_execution_contract",
    "validate_executor_contract",
    "validate_harness_policy",
]
