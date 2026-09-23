"""Governance metadata and API boundary for a sandboxed MCP Gateway.

This module intentionally contains no MCP process launcher and no container
client.  A gateway row is a reviewed deployment/profile reference; the actual
stdio server lifecycle belongs to the independent Sandbox Runner.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Mapping

import src.settings as settings

from src.sandbox.client import SandboxServiceClient, validate_service_url
from src.sandbox.schema import SchemaValidationError, normalize_schema, validate_value


class GatewayPolicyError(ValueError):
    pass


class GatewayNotFoundError(KeyError):
    pass


class GatewayStateError(RuntimeError):
    pass


_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$", re.IGNORECASE)
_FORBIDDEN_REGISTRATION_KEYS = {
    "command",
    "cmd",
    "args",
    "arguments",
    "env",
    "environment",
    "install",
    "install_script",
    "docker_args",
    "volume",
    "volumes",
    "host_path",
}
_FORBIDDEN_METADATA_KEYS = _FORBIDDEN_REGISTRATION_KEYS | {
    "secret",
    "secrets",
    "password",
    "token",
    "authorization",
    "headers",
}
_GATEWAY_CAPABILITIES = frozenset(
    {"external_tool.invoke", "external_tool.write", "sandbox.network"}
)
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,199}$")
_STATE_TRANSITIONS = {
    "draft": frozenset({"reviewed", "revoked"}),
    "reviewed": frozenset({"tested", "draft", "revoked"}),
    "tested": frozenset({"enabled", "draft", "revoked"}),
    "enabled": frozenset({"disabled", "revoked"}),
    "disabled": frozenset({"enabled", "revoked", "draft"}),
    "revoked": frozenset({"draft"}),
}

GATEWAY_STATES = frozenset({"draft", "reviewed", "tested", "enabled", "disabled", "revoked"})


def _ref(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if (
        not _REF_RE.fullmatch(text)
        or ".." in text
        or "//" in text
        or any(ord(char) < 32 for char in text)
    ):
        raise GatewayPolicyError(f"{field_name} must be a bounded deployment reference")
    return text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    def clean(item: Any, path: str = "metadata", depth: int = 0) -> Any:
        if depth > 4:
            raise GatewayPolicyError(f"{path} is too deeply nested")
        if isinstance(item, Mapping):
            output: dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key).strip()
                normalized = key.lower().replace("-", "_")
                if not key or len(key) > 80 or normalized in _FORBIDDEN_METADATA_KEYS:
                    raise GatewayPolicyError(f"{path} contains a forbidden metadata field")
                output[key] = clean(child, f"{path}.{key}", depth + 1)
            return output
        if isinstance(item, (list, tuple)):
            if len(item) > 100:
                raise GatewayPolicyError(f"{path} contains too many values")
            return [clean(child, f"{path}[{index}]", depth + 1) for index, child in enumerate(item)]
        if item is None or isinstance(item, (str, int, float, bool)):
            if isinstance(item, str) and len(item) > 500:
                raise GatewayPolicyError(f"{path} contains an oversized value")
            return item
        raise GatewayPolicyError(f"{path} must be JSON-compatible")

    output = clean(dict(value or {}))
    if len(_json(output).encode("utf-8")) > 16 * 1024:
        raise GatewayPolicyError("Gateway metadata exceeds the configured limit")
    return output


def _canonical_digest(value: Any) -> str:
    return str(value or "").strip().lower().removeprefix("sha256:")


def _normalize_tool_allowlist(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize the reviewed Gateway tool contract.

    The declaration accepts either a name-only iterable (legacy callers) or a
    mapping of tool name to a bounded input schema.  A remote discovery result
    never expands this map; unknown tools remain invisible and uncallable.
    """

    if value in (None, ""):
        return {}
    if isinstance(value, Mapping):
        entries = value.items()
    elif isinstance(value, (list, tuple, set, frozenset)):
        entries = ((item, {}) for item in value)
    else:
        raise GatewayPolicyError("tool_allowlist must be a mapping or list of tool names")
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_schema in entries:
        name = str(raw_name or "").strip()
        if not _TOOL_NAME_RE.fullmatch(name):
            raise GatewayPolicyError("tool_allowlist contains an invalid tool name")
        # A name-only legacy entry has an unconstrained reviewed schema.  A
        # mapping entry is still restricted to the deliberately small schema
        # subset; unsupported JSON-Schema features fail closed.
        if raw_schema in (None, ""):
            schema = {}
        elif isinstance(raw_schema, Mapping) and "schema" in raw_schema:
            extras = set(raw_schema) - {"schema"}
            if extras:
                raise GatewayPolicyError("tool allowlist metadata is not supported in this contract")
            schema = normalize_schema(raw_schema.get("schema"), field_name=f"tool_allowlist.{name}")
        else:
            schema = normalize_schema(raw_schema, field_name=f"tool_allowlist.{name}")
        if name in result and result[name] != schema:
            raise GatewayPolicyError("tool_allowlist contains duplicate tool schemas")
        result[name] = schema
        if len(result) > 256:
            raise GatewayPolicyError("tool_allowlist contains too many tools")
    return dict(sorted(result.items()))


def _reject_runtime_arguments(value: Any, *, path: str = "arguments", depth: int = 0) -> None:
    """Recursively reject runtime-control fields in Gateway arguments.

    This is a second boundary in addition to the reviewed input schema: a
    name-only allowlist entry has no reviewed schema, so the forbidden-key
    check must still run for every invocation.
    """

    if depth > 8:
        raise GatewayPolicyError(f"{path} is too deeply nested")
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_METADATA_KEYS or normalized in {"credential", "api_key"}:
                raise GatewayPolicyError(f"{path}.{key} is not an allowed Gateway argument")
            _reject_runtime_arguments(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise GatewayPolicyError(f"{path} contains too many values")
        for index, child in enumerate(value):
            _reject_runtime_arguments(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > 20_000:
            raise GatewayPolicyError(f"{path} contains an oversized value")
        return
    raise GatewayPolicyError(f"{path} must be JSON-compatible")


def _tool_is_write(deployment: "GatewayDeployment", tool_name: str) -> bool:
    raw = deployment.metadata.get("write_tools", ()) if isinstance(deployment.metadata, Mapping) else ()
    return str(tool_name) in {str(item) for item in raw if str(item).strip()}


@dataclass(frozen=True)
class GatewayDeployment:
    deployment_ref: str
    profile_ref: str
    endpoint: str
    digest: str
    catalog_source: str = "curated"
    approval_state: str = "draft"
    requires_gateway: bool = True
    network_policy_ref: str = ""
    capabilities: tuple[str, ...] = ()
    sbom_ref: str = ""
    enabled: bool = False
    health_status: str = "unknown"
    created_by: str = ""
    updated_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tool_schemas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_ref": self.deployment_ref,
            "profile_ref": self.profile_ref,
            "endpoint": self.endpoint,
            "digest": self.digest,
            "catalog_source": self.catalog_source,
            "approval_state": self.approval_state,
            "requires_gateway": self.requires_gateway,
            "network_policy_ref": self.network_policy_ref,
            "capabilities": list(self.capabilities),
            "sbom_ref": self.sbom_ref,
            "enabled": self.enabled,
            "health_status": self.health_status,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
            "tool_allowlist": sorted(str(name) for name in self.tool_schemas),
            "tool_schemas": {str(name): dict(schema) for name, schema in self.tool_schemas.items()},
        }


_DDL = """
CREATE TABLE IF NOT EXISTS sandbox_gateway_deployments (
    deployment_ref TEXT PRIMARY KEY,
    profile_ref TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    digest TEXT NOT NULL,
    catalog_source TEXT NOT NULL DEFAULT 'curated',
    approval_state TEXT NOT NULL DEFAULT 'draft',
    requires_gateway INTEGER NOT NULL DEFAULT 1,
    network_policy_ref TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    sbom_ref TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    health_status TEXT NOT NULL DEFAULT 'unknown',
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    tool_schemas_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sandbox_gateway_subscriptions (
    deployment_ref TEXT NOT NULL,
    user_id TEXT NOT NULL,
    department_id TEXT NOT NULL DEFAULT '',
    allow_write INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(deployment_ref, user_id),
    FOREIGN KEY(deployment_ref) REFERENCES sandbox_gateway_deployments(deployment_ref) ON DELETE CASCADE
);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class GatewayRegistry:
    """Durable registry for reviewed Gateway deployment references."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = os.path.abspath(str(db_path or settings.SANDBOX_TASK_DB_PATH))
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, closing(self._connect()) as conn:
            conn.executescript(_DDL)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sandbox_gateway_deployments)")}
            if "tool_schemas_json" not in columns:
                conn.execute(
                    "ALTER TABLE sandbox_gateway_deployments ADD COLUMN tool_schemas_json TEXT NOT NULL DEFAULT '{}'"
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _row_to_deployment(row: sqlite3.Row) -> GatewayDeployment:
        try:
            capabilities = tuple(json.loads(str(row["capabilities_json"] or "[]")) or [])
        except (ValueError, TypeError, json.JSONDecodeError):
            capabilities = ()
        try:
            metadata = dict(json.loads(str(row["metadata_json"] or "{}")) or {})
        except (ValueError, TypeError, json.JSONDecodeError):
            metadata = {}
        try:
            raw_schemas = json.loads(str(row["tool_schemas_json"] or "{}"))
            tool_schemas = {
                str(name): dict(schema)
                for name, schema in (raw_schemas.items() if isinstance(raw_schemas, Mapping) else ())
                if isinstance(schema, Mapping)
            }
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            tool_schemas = {}
        return GatewayDeployment(
            deployment_ref=str(row["deployment_ref"]),
            profile_ref=str(row["profile_ref"]),
            endpoint=str(row["endpoint"]),
            digest=str(row["digest"]),
            catalog_source=str(row["catalog_source"]),
            approval_state=str(row["approval_state"]),
            requires_gateway=bool(row["requires_gateway"]),
            network_policy_ref=str(row["network_policy_ref"] or ""),
            capabilities=capabilities,
            sbom_ref=str(row["sbom_ref"] or ""),
            enabled=bool(row["enabled"]),
            health_status=str(row["health_status"] or "unknown"),
            created_by=str(row["created_by"] or ""),
            updated_by=str(row["updated_by"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            metadata=metadata,
            tool_schemas=tool_schemas,
        )

    def get(self, deployment_ref: str) -> GatewayDeployment | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sandbox_gateway_deployments WHERE deployment_ref = ?",
                (str(deployment_ref),),
            ).fetchone()
        return self._row_to_deployment(row) if row else None

    def require(self, deployment_ref: str) -> GatewayDeployment:
        item = self.get(deployment_ref)
        if item is None:
            raise GatewayNotFoundError(str(deployment_ref))
        return item

    def list(self, *, include_revoked: bool = False) -> list[GatewayDeployment]:
        clause = "" if include_revoked else " WHERE approval_state <> 'revoked'"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM sandbox_gateway_deployments{clause} ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_deployment(row) for row in rows]

    def register(
        self,
        *,
        deployment_ref: str,
        profile_ref: str,
        endpoint: str,
        digest: str,
        catalog_source: str = "curated",
        network_policy_ref: str = "",
        capabilities: Any = (),
        sbom_ref: str = "",
        tool_allowlist: Mapping[str, Any] | list[str] | tuple[str, ...] | None = None,
        actor: str = "",
        metadata: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> GatewayDeployment:
        """Create/update declaration metadata; no executable recipe is accepted."""

        if extra:
            forbidden = _FORBIDDEN_REGISTRATION_KEYS.intersection(
                str(key).strip().lower() for key in extra
            )
            if forbidden:
                raise GatewayPolicyError("Gateway registration cannot contain command, args, env or install fields")
        ref = _ref(deployment_ref, "deployment_ref")
        profile = _ref(profile_ref, "profile_ref")
        endpoint_value = validate_service_url(endpoint, allow_insecure=False)
        configured_endpoint = str(getattr(settings, "SANDBOX_GATEWAY_URL", "") or "").strip()
        if configured_endpoint:
            configured_endpoint = validate_service_url(configured_endpoint, allow_insecure=False)
            if endpoint_value != configured_endpoint:
                raise GatewayPolicyError("Gateway endpoint must match the deployment-controlled SANDBOX_GATEWAY_URL")
        digest_value = _canonical_digest(digest)
        if not _DIGEST_RE.fullmatch(digest_value):
            raise GatewayPolicyError("Gateway digest must be a SHA-256 digest")
        network = str(network_policy_ref or "").strip()
        if network:
            _ref(network, "network_policy_ref")
        capability_values = tuple(sorted({str(item).strip() for item in (capabilities or ()) if str(item).strip()}))
        unknown_capabilities = set(capability_values) - _GATEWAY_CAPABILITIES
        if unknown_capabilities:
            raise GatewayPolicyError(
                "unsupported Gateway capabilities: " + ", ".join(sorted(unknown_capabilities))
            )
        try:
            reviewed_tools = _normalize_tool_allowlist(tool_allowlist)
        except (GatewayPolicyError, SchemaValidationError) as exc:
            raise GatewayPolicyError(str(exc)) from exc
        safe_metadata = _safe_metadata(metadata)
        write_tools = {
            str(item).strip()
            for item in safe_metadata.get("write_tools", ())
            if str(item).strip()
        }
        if not write_tools.issubset(set(reviewed_tools)):
            raise GatewayPolicyError("metadata.write_tools must be a subset of tool_allowlist")
        if write_tools and "external_tool.write" not in set(capability_values):
            raise GatewayPolicyError("write_tools requires external_tool.write capability")
        if network and "sandbox.network" not in set(capability_values):
            raise GatewayPolicyError("network_policy_ref requires sandbox.network capability")
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM sandbox_gateway_deployments WHERE deployment_ref = ?", (ref,)
                ).fetchone()
                old_item = self._row_to_deployment(existing) if existing is not None else None
                immutable_changed = bool(
                    old_item
                    and (
                        _canonical_digest(old_item.digest) != digest_value
                        or old_item.profile_ref != profile
                        or old_item.endpoint != endpoint_value
                        or old_item.catalog_source != str(catalog_source or "curated")[:50]
                        or tuple(old_item.capabilities) != capability_values
                        or old_item.network_policy_ref != network
                        or old_item.sbom_ref != str(sbom_ref or "")[:256]
                        or _json(dict(old_item.metadata)) != _json(safe_metadata)
                        or _json(dict(old_item.tool_schemas)) != _json(reviewed_tools)
                    )
                )
                if old_item is not None and old_item.enabled and immutable_changed:
                    raise GatewayStateError("enabled Gateway policy cannot be replaced in place")
                approval_state = (
                    "draft"
                    if old_item is None or immutable_changed or old_item.approval_state == "revoked"
                    else old_item.approval_state
                )
                conn.execute(
                    """
                    INSERT INTO sandbox_gateway_deployments(
                        deployment_ref, profile_ref, endpoint, digest, catalog_source,
                        approval_state, requires_gateway, network_policy_ref, capabilities_json,
                        sbom_ref, enabled, health_status, created_by, updated_by,
                        created_at, updated_at, metadata_json
                        ,tool_schemas_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 'unknown', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(deployment_ref) DO UPDATE SET
                        profile_ref = excluded.profile_ref,
                        endpoint = excluded.endpoint,
                        digest = excluded.digest,
                        catalog_source = excluded.catalog_source,
                        approval_state = excluded.approval_state,
                        network_policy_ref = excluded.network_policy_ref,
                        capabilities_json = excluded.capabilities_json,
                        sbom_ref = excluded.sbom_ref,
                        enabled = CASE WHEN excluded.approval_state = 'draft' THEN 0 ELSE sandbox_gateway_deployments.enabled END,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at,
                        metadata_json = excluded.metadata_json,
                        tool_schemas_json = excluded.tool_schemas_json
                    """,
                    (
                        ref,
                        profile,
                        endpoint_value,
                        digest_value,
                        str(catalog_source or "curated")[:50],
                        approval_state,
                        network,
                        _json(list(capability_values)),
                        str(sbom_ref or "")[:256],
                        str(actor or "")[:128],
                        str(actor or "")[:128],
                        now,
                        now,
                        _json(safe_metadata),
                        _json(reviewed_tools),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM sandbox_gateway_deployments WHERE deployment_ref = ?", (ref,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_deployment(row)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def review(self, deployment_ref: str, *, actor: str = "") -> GatewayDeployment:
        return self._set_state(deployment_ref, "reviewed", actor=actor)

    def mark_tested(self, deployment_ref: str, *, actor: str = "", health_status: str = "healthy") -> GatewayDeployment:
        return self._set_state(deployment_ref, "tested", actor=actor, health_status=health_status)

    def enable(self, deployment_ref: str, *, actor: str = "") -> GatewayDeployment:
        item = self.require(deployment_ref)
        if item.approval_state not in {"tested", "enabled", "disabled"}:
            raise GatewayStateError("Gateway must pass review and health test before enable")
        if not bool(getattr(settings, "SANDBOX_GATEWAY_ENABLED", False)):
            raise GatewayStateError("sandbox Gateway switch is disabled")
        configured_endpoint = str(getattr(settings, "SANDBOX_GATEWAY_URL", "") or "").strip()
        if not configured_endpoint or validate_service_url(configured_endpoint, allow_insecure=False) != item.endpoint:
            raise GatewayStateError("Gateway endpoint is not bound to the deployment configuration")
        return self._set_state(deployment_ref, "enabled", actor=actor, enabled=True)

    def disable(self, deployment_ref: str, *, actor: str = "") -> GatewayDeployment:
        return self._set_state(deployment_ref, "disabled", actor=actor, enabled=False)

    def revoke(self, deployment_ref: str, *, actor: str = "") -> GatewayDeployment:
        return self._set_state(deployment_ref, "revoked", actor=actor, enabled=False)

    def _set_state(
        self,
        deployment_ref: str,
        state: str,
        *,
        actor: str = "",
        enabled: bool | None = None,
        health_status: str | None = None,
    ) -> GatewayDeployment:
        if state not in GATEWAY_STATES:
            raise GatewayStateError("invalid Gateway state")
        item = self.require(deployment_ref)
        if state == item.approval_state:
            return item
        if state not in _STATE_TRANSITIONS.get(item.approval_state, frozenset()):
            raise GatewayStateError(
                f"cannot transition Gateway {item.approval_state} -> {state}"
            )
        assignments = ["approval_state = ?", "updated_by = ?", "updated_at = ?"]
        params: list[Any] = [state, str(actor or "")[:128], _now()]
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(1 if enabled else 0)
        if health_status is not None:
            assignments.append("health_status = ?")
            params.append(str(health_status)[:50])
        params.append(str(deployment_ref))
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE sandbox_gateway_deployments SET {', '.join(assignments)} WHERE deployment_ref = ?",
                params,
            )
        return self.require(deployment_ref)

    def attach(self, deployment_ref: str, *, client: SandboxServiceClient | None = None, envelope: Any = None) -> dict[str, Any]:
        item = self.require(deployment_ref)
        if not item.enabled or item.approval_state != "enabled":
            raise GatewayStateError("Gateway deployment is not enabled")
        if not bool(getattr(settings, "SANDBOX_GATEWAY_ENABLED", False)):
            raise GatewayStateError("sandbox Gateway switch is disabled")
        if client is None:
            return {"deployment_ref": item.deployment_ref, "attached": False, "reason": "client_not_configured"}
        if client.gateway_url and client.gateway_url != item.endpoint:
            raise GatewayStateError("configured Gateway endpoint does not match the reviewed deployment")
        result = client.gateway_attach(
            deployment_ref=item.deployment_ref, profile_ref=item.profile_ref, envelope=envelope
        )
        expected_profile = str(getattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "") or "").lower().removeprefix("sha256:")
        if (
            result.get("attached") is not True
            or result.get("deployment_ref") != item.deployment_ref
            or result.get("profile_ref") != item.profile_ref
            or result.get("image_digest") != f"sha256:{item.digest.lower().removeprefix('sha256:')}"
            or not expected_profile
            or result.get("profile_digest") != expected_profile
        ):
            raise GatewayStateError("Gateway attachment does not match the reviewed image and runtime profile")
        current = self.require(item.deployment_ref)
        if (
            not current.enabled
            or current.approval_state != "enabled"
            or current.profile_ref != item.profile_ref
            or current.digest != item.digest
            or current.endpoint != item.endpoint
        ):
            try:
                client.gateway_disable(item.deployment_ref, envelope=envelope)
            except Exception:  # noqa: BLE001 - retain local deny and mark the cleanup as uncertain
                self._set_health(item.deployment_ref, "cleanup_pending")
            raise GatewayStateError("Gateway deployment changed during attachment")
        self._set_health(item.deployment_ref, "healthy")
        return result

    def health(self, deployment_ref: str, *, client: SandboxServiceClient | None = None, envelope: Any = None) -> dict[str, Any]:
        item = self.require(deployment_ref)
        if client is None:
            return {"deployment_ref": item.deployment_ref, "status": item.health_status}
        if client.gateway_url and client.gateway_url != item.endpoint:
            raise GatewayStateError("configured Gateway endpoint does not match the reviewed deployment")
        result = client.gateway_health(envelope=envelope)
        status = str(result.get("status") or result.get("health_status") or "unknown")[:50]
        self._set_health(item.deployment_ref, status)
        return result

    def discover(self, deployment_ref: str, *, client: SandboxServiceClient, envelope: Any = None) -> dict[str, Any]:
        item = self.require(deployment_ref)
        if not item.enabled:
            raise GatewayStateError("Gateway deployment is not enabled")
        if client.gateway_url and client.gateway_url != item.endpoint:
            raise GatewayStateError("configured Gateway endpoint does not match the reviewed deployment")
        result = client.gateway_discover(item.deployment_ref, envelope=envelope)
        return self.filter_discovery(item.deployment_ref, result)

    def filter_discovery(self, deployment_ref: str, result: Mapping[str, Any]) -> dict[str, Any]:
        """Return only tools present in the reviewed allowlist.

        Tool descriptions and schemas are downstream data.  The allowlist and
        locally stored schema are the authority, so a Gateway cannot broaden
        HDB's callable surface by advertising a new tool after deployment.
        """

        item = self.require(deployment_ref)
        raw_tools = result.get("tools", []) if isinstance(result, Mapping) else []
        if not isinstance(raw_tools, (list, tuple)):
            raise GatewayStateError("Gateway discovery returned an invalid tool list")
        tools: list[dict[str, Any]] = []
        for raw in list(raw_tools)[:512]:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or raw.get("tool_name") or "").strip()
            if name not in item.tool_schemas:
                continue
            reviewed_schema = dict(item.tool_schemas[name])
            # If the remote advertises a schema, it must not differ from the
            # reviewed contract.  This prevents a server update from widening
            # argument shape without a new review.
            advertised = raw.get("inputSchema", raw.get("input_schema"))
            if advertised not in (None, {}):
                try:
                    normalized = normalize_schema(advertised, field_name=f"discovery.{name}")
                except SchemaValidationError as exc:
                    raise GatewayStateError("Gateway advertised an invalid tool schema") from exc
                # A name-only allowlist entry has no reviewed schema.  In that
                # legacy case the advertised shape is not adopted as policy;
                # argument validation still runs the forbidden-key boundary.
                if reviewed_schema and normalized != reviewed_schema:
                    raise GatewayStateError("Gateway tool schema differs from the reviewed schema")
            tools.append(
                {
                    "name": name,
                    "description": str(raw.get("description") or "")[:2000],
                    "inputSchema": reviewed_schema,
                    "readOnly": not _tool_is_write(item, name),
                }
            )
        return {
            "deployment_ref": item.deployment_ref,
            "profile_ref": item.profile_ref,
            "tools": tools,
            "tool_count": len(tools),
        }

    def subscribe(
        self,
        deployment_ref: str,
        *,
        user_id: str | int,
        department_id: str | int,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        item = self.require(deployment_ref)
        if not item.enabled or item.approval_state != "enabled":
            raise GatewayStateError("Gateway deployment is not enabled")
        user = str(user_id or "").strip()
        department = str(department_id or "").strip()
        if not user or not department:
            raise GatewayPolicyError("Gateway subscription requires user and department scope")
        if allow_write and "external_tool.write" not in set(item.capabilities):
            raise GatewayPolicyError("Gateway deployment does not authorize write tools")
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO sandbox_gateway_subscriptions(deployment_ref, user_id, department_id, allow_write, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT(deployment_ref, user_id) DO UPDATE SET department_id=excluded.department_id, allow_write=excluded.allow_write, enabled=1, updated_at=excluded.updated_at",
                (item.deployment_ref, user, department, 1 if allow_write else 0, now, now),
            )
        return {
            "deployment_ref": item.deployment_ref,
            "user_id": user,
            "department_id": department,
            "allow_write": bool(allow_write),
            "enabled": True,
        }

    def unsubscribe(self, deployment_ref: str, *, user_id: str | int) -> None:
        self.require(deployment_ref)
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE sandbox_gateway_subscriptions SET enabled=0, updated_at=? WHERE deployment_ref=? AND user_id=?",
                (_now(), str(user_id or ""), str(deployment_ref)),
            )

    def _subscription(self, deployment_ref: str, user_id: str | int) -> sqlite3.Row | None:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM sandbox_gateway_subscriptions WHERE deployment_ref=? AND user_id=? AND enabled=1",
                (str(deployment_ref), str(user_id or "")),
            ).fetchone()

    def authorize_invocation(
        self,
        deployment_ref: str,
        *,
        user_id: str | int,
        department_id: str | int,
        tool_name: str,
        arguments: Mapping[str, Any],
        capabilities: Any = (),
        is_admin: bool = False,
    ) -> GatewayDeployment:
        item = self.require(deployment_ref)
        if not item.enabled or item.approval_state != "enabled":
            raise GatewayStateError("Gateway deployment is not enabled")
        name = str(tool_name or "").strip()
        if name not in item.tool_schemas:
            raise GatewayPolicyError("tool is not in the reviewed Gateway allowlist")
        granted = {str(value).strip() for value in (capabilities or ()) if str(value).strip()}
        if "external_tool.invoke" not in granted:
            raise GatewayPolicyError("external_tool.invoke capability is not authorized")
        if _tool_is_write(item, name):
            if "external_tool.write" not in granted:
                raise GatewayPolicyError("external_tool.write capability is not authorized")
            subscription = self._subscription(deployment_ref, user_id)
            if subscription is None or not bool(subscription["allow_write"]):
                raise GatewayPolicyError("write invocation requires an explicit subscription consent")
        elif not is_admin:
            subscription = self._subscription(deployment_ref, user_id)
            if subscription is None:
                raise GatewayPolicyError("Gateway deployment is not subscribed by this user")
        if not isinstance(arguments, Mapping):
            raise GatewayPolicyError("Gateway arguments must be an object")
        _reject_runtime_arguments(arguments)
        try:
            validate_value(arguments, item.tool_schemas[name], path=f"arguments.{name}")
        except SchemaValidationError as exc:
            raise GatewayPolicyError(str(exc)) from exc
        department = str(department_id or "")
        if not is_admin:
            subscription = self._subscription(deployment_ref, user_id)
            if subscription is None or str(subscription["department_id"]) != department:
                raise GatewayPolicyError("Gateway subscription department scope does not match")
        return item

    def _set_health(self, deployment_ref: str, status: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE sandbox_gateway_deployments SET health_status = ?, updated_at = ? WHERE deployment_ref = ?",
                (str(status)[:50], _now(), str(deployment_ref)),
            )


GatewayService = GatewayRegistry


__all__ = [
    "GATEWAY_STATES",
    "GatewayDeployment",
    "GatewayNotFoundError",
    "GatewayPolicyError",
    "GatewayRegistry",
    "GatewayService",
    "GatewayStateError",
]
