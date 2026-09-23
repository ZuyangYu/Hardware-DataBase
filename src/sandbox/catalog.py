"""Curated execution-bundle catalog.

An execution task may reference only a bundle/profile that has passed the
declaration/review/test/enable lifecycle here.  The catalog stores a digest
and policy summary, never a command, image arguments, host path or secret.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Mapping

import src.settings as settings

from .policy import require_execution_enabled
from .schema import SchemaValidationError, normalize_schema, validate_value


class BundleCatalogError(ValueError):
    pass


class BundleNotFound(BundleCatalogError):
    pass


class BundleNotAuthorized(BundleCatalogError):
    pass


class BundleStateConflict(BundleCatalogError):
    pass


_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$", re.IGNORECASE)
_FORBIDDEN_KEYS = frozenset(
    {
        "command",
        "cmd",
        "args",
        "arguments",
        "shell",
        "script",
        "env",
        "environment",
        "volume",
        "volumes",
        "host_path",
        "install",
        "install_script",
        "docker_args",
    }
)
_STATES = frozenset({"draft", "reviewed", "tested", "enabled", "revoked"})
_CAPABILITIES = frozenset(
    {"sandbox.execute", "sandbox.network", "sandbox.internal_data", "sandbox.write_artifact"}
)
_FORBIDDEN_METADATA_KEYS = _FORBIDDEN_KEYS | frozenset(
    {"secret", "secrets", "password", "token", "authorization", "headers", "credential"}
)
_STATE_TRANSITIONS = {
    "draft": frozenset({"reviewed", "revoked"}),
    "reviewed": frozenset({"tested", "draft", "revoked"}),
    "tested": frozenset({"enabled", "draft", "revoked"}),
    "enabled": frozenset({"revoked"}),
    "revoked": frozenset({"draft"}),
}


def _ref(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise BundleCatalogError(f"{field_name} is not a valid reference")
    return text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate bounded catalog labels without persisting credentials/recipes."""

    def clean(item: Any, path: str = "metadata", depth: int = 0) -> Any:
        if depth > 4:
            raise BundleCatalogError(f"{path} is too deeply nested")
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key).strip()
                normalized = key.lower().replace("-", "_")
                if not key or len(key) > 80:
                    raise BundleCatalogError(f"{path} contains an invalid metadata key")
                if normalized in _FORBIDDEN_METADATA_KEYS:
                    raise BundleCatalogError(f"{path}.{key} is not allowed in a catalog declaration")
                result[key] = clean(child, f"{path}.{key}", depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            if len(item) > 100:
                raise BundleCatalogError(f"{path} contains too many values")
            return [clean(child, f"{path}[{index}]", depth + 1) for index, child in enumerate(item)]
        if item is None or isinstance(item, (str, int, float, bool)):
            if isinstance(item, str) and len(item) > 500:
                raise BundleCatalogError(f"{path} contains an oversized value")
            return item
        raise BundleCatalogError(f"{path} contains a non-JSON value")

    result = clean(dict(value or {}))
    if len(_json(result).encode("utf-8")) > 16 * 1024:
        raise BundleCatalogError("catalog metadata exceeds the configured limit")
    return result


def _canonical_digest(value: Any) -> str:
    return str(value or "").strip().lower().removeprefix("sha256:")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExecutionBundle:
    bundle_ref: str
    bundle_digest: str
    profile_ref: str
    approval_state: str = "draft"
    catalog_source: str = "curated"
    required_capabilities: tuple[str, ...] = ("sandbox.execute",)
    network_policy_ref: str = ""
    sbom_ref: str = ""
    signature_ref: str = ""
    created_by: str = ""
    updated_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    tool_allowlist: tuple[str, ...] = ()
    runtime_policy_ref: str = ""
    runtime_policy_digest: str = ""

    @property
    def enabled(self) -> bool:
        return self.approval_state == "enabled"

    @property
    def policy_snapshot_hash(self) -> str:
        """Digest of immutable admission inputs, not executable contents."""

        payload = {
            "bundle_ref": self.bundle_ref,
            "bundle_digest": _canonical_digest(self.bundle_digest),
            "profile_ref": self.profile_ref,
            "catalog_source": self.catalog_source,
            "required_capabilities": sorted(set(self.required_capabilities)),
            "network_policy_ref": self.network_policy_ref,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "tool_allowlist": list(self.tool_allowlist),
            "runtime_policy_ref": self.runtime_policy_ref,
            "runtime_policy_digest": self.runtime_policy_digest,
        }
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_ref": self.bundle_ref,
            "bundle_digest": self.bundle_digest,
            "profile_ref": self.profile_ref,
            "approval_state": self.approval_state,
            "catalog_source": self.catalog_source,
            "required_capabilities": list(self.required_capabilities),
            "network_policy_ref": self.network_policy_ref,
            "sbom_ref": self.sbom_ref,
            "signature_ref": self.signature_ref,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "tool_allowlist": list(self.tool_allowlist),
            "runtime_policy_ref": self.runtime_policy_ref,
            "runtime_policy_digest": self.runtime_policy_digest,
            "policy_snapshot_hash": self.policy_snapshot_hash,
        }


_DDL = """
CREATE TABLE IF NOT EXISTS sandbox_execution_bundles (
    bundle_ref TEXT PRIMARY KEY,
    bundle_digest TEXT NOT NULL,
    profile_ref TEXT NOT NULL,
    approval_state TEXT NOT NULL DEFAULT 'draft',
    catalog_source TEXT NOT NULL DEFAULT 'curated',
    required_capabilities_json TEXT NOT NULL DEFAULT '["sandbox.execute"]',
    network_policy_ref TEXT NOT NULL DEFAULT '',
    sbom_ref TEXT NOT NULL DEFAULT '',
    signature_ref TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    input_schema_json TEXT NOT NULL DEFAULT '{}',
    output_schema_json TEXT NOT NULL DEFAULT '{}',
    tool_allowlist_json TEXT NOT NULL DEFAULT '[]',
    runtime_policy_ref TEXT NOT NULL DEFAULT '',
    runtime_policy_digest TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sandbox_bundles_state
    ON sandbox_execution_bundles(approval_state, updated_at);
"""


class SandboxCatalog:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = os.path.abspath(str(db_path or getattr(settings, "SANDBOX_TASK_DB_PATH", "sandbox_tasks.db")))
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, closing(self._connect()) as conn:
            conn.executescript(_DDL)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sandbox_execution_bundles)")}
            migrations = {
                "input_schema_json": "TEXT NOT NULL DEFAULT '{}'",
                "output_schema_json": "TEXT NOT NULL DEFAULT '{}'",
                "tool_allowlist_json": "TEXT NOT NULL DEFAULT '[]'",
                "runtime_policy_ref": "TEXT NOT NULL DEFAULT ''",
                "runtime_policy_digest": "TEXT NOT NULL DEFAULT ''",
            }
            for column, ddl in migrations.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE sandbox_execution_bundles ADD COLUMN {column} {ddl}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _row_to_bundle(row: sqlite3.Row) -> ExecutionBundle:
        try:
            capabilities = tuple(json.loads(str(row["required_capabilities_json"] or "[]")) or [])
        except (TypeError, ValueError, json.JSONDecodeError):
            capabilities = ()
        try:
            metadata = dict(json.loads(str(row["metadata_json"] or "{}")) or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        keys = set(row.keys())

        def _mapping(column: str) -> dict[str, Any]:
            if column not in keys:
                return {}
            try:
                value = json.loads(str(row[column] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}
            return dict(value) if isinstance(value, Mapping) else {}

        try:
            tool_allowlist = tuple(json.loads(str(row["tool_allowlist_json"] or "[]")) or [])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            tool_allowlist = ()
        return ExecutionBundle(
            bundle_ref=str(row["bundle_ref"]),
            bundle_digest=str(row["bundle_digest"]),
            profile_ref=str(row["profile_ref"]),
            approval_state=str(row["approval_state"]),
            catalog_source=str(row["catalog_source"]),
            required_capabilities=capabilities,
            network_policy_ref=str(row["network_policy_ref"] or ""),
            sbom_ref=str(row["sbom_ref"] or ""),
            signature_ref=str(row["signature_ref"] or ""),
            created_by=str(row["created_by"] or ""),
            updated_by=str(row["updated_by"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            metadata=metadata,
            input_schema=_mapping("input_schema_json"),
            output_schema=_mapping("output_schema_json"),
            tool_allowlist=tuple(str(item) for item in tool_allowlist),
            runtime_policy_ref=str(row["runtime_policy_ref"] or "") if "runtime_policy_ref" in keys else "",
            runtime_policy_digest=str(row["runtime_policy_digest"] or "") if "runtime_policy_digest" in keys else "",
        )

    def get(self, bundle_ref: str) -> ExecutionBundle | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sandbox_execution_bundles WHERE bundle_ref = ?", (str(bundle_ref),)
            ).fetchone()
        return self._row_to_bundle(row) if row else None

    def require(self, bundle_ref: str) -> ExecutionBundle:
        item = self.get(bundle_ref)
        if item is None:
            raise BundleNotFound(str(bundle_ref))
        return item

    def list(self, *, include_revoked: bool = False) -> list[ExecutionBundle]:
        clause = "" if include_revoked else " WHERE approval_state <> 'revoked'"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM sandbox_execution_bundles{clause} ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_bundle(row) for row in rows]

    def register(
        self,
        *,
        bundle_ref: str,
        bundle_digest: str,
        profile_ref: str,
        required_capabilities: Any = ("sandbox.execute",),
        network_policy_ref: str = "",
        sbom_ref: str = "",
        signature_ref: str = "",
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        tool_allowlist: Any = (),
        runtime_policy_ref: str = "",
        runtime_policy_digest: str = "",
        catalog_source: str = "curated",
        actor: str = "",
        metadata: Mapping[str, Any] | None = None,
        declaration: Mapping[str, Any] | None = None,
    ) -> ExecutionBundle:
        if declaration:
            forbidden = _FORBIDDEN_KEYS.intersection(str(key).strip().lower() for key in declaration)
            if forbidden:
                raise BundleCatalogError("bundle declaration cannot contain command, args, env, volume or install fields")
        ref = _ref(bundle_ref, "bundle_ref")
        profile = _ref(profile_ref, "profile_ref")
        digest = _canonical_digest(bundle_digest)
        if not _DIGEST_RE.fullmatch(digest):
            raise BundleCatalogError("bundle_digest must be a SHA-256 digest")
        capabilities = tuple(sorted({str(item).strip() for item in (required_capabilities or ()) if str(item).strip()}))
        unknown = set(capabilities) - _CAPABILITIES
        if unknown:
            raise BundleCatalogError("unsupported bundle capabilities: " + ", ".join(sorted(unknown)))
        if "sandbox.execute" not in capabilities:
            raise BundleCatalogError("sandbox.execute capability is required")
        network = str(network_policy_ref or "").strip()
        if network and "sandbox.network" not in capabilities:
            raise BundleCatalogError("network_policy_ref requires sandbox.network")
        try:
            normalized_input_schema = normalize_schema(input_schema or {}, field_name="input_schema")
            normalized_output_schema = normalize_schema(output_schema or {}, field_name="output_schema")
        except SchemaValidationError as exc:
            raise BundleCatalogError(str(exc)) from exc
        allowlist = tuple(sorted({str(item).strip() for item in (tool_allowlist or ()) if str(item).strip()}))
        if len(allowlist) > 256 or any(
            len(item) > 200 or any(ord(char) < 32 for char in item) for item in allowlist
        ):
            raise BundleCatalogError("tool_allowlist contains an invalid tool name")
        policy_ref = str(runtime_policy_ref or "").strip()
        if policy_ref:
            policy_ref = _ref(policy_ref, "runtime_policy_ref")
        policy_digest = _canonical_digest(runtime_policy_digest)
        if policy_digest and not _DIGEST_RE.fullmatch(policy_digest):
            raise BundleCatalogError("runtime_policy_digest must be a SHA-256 digest")
        safe_metadata = _safe_metadata(metadata)
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute(
                    "SELECT * FROM sandbox_execution_bundles WHERE bundle_ref = ?", (ref,)
                ).fetchone()
                old_item = self._row_to_bundle(old) if old is not None else None
                immutable_changed = bool(
                    old_item
                    and (
                        _canonical_digest(old_item.bundle_digest) != digest
                        or old_item.profile_ref != profile
                        or old_item.catalog_source != str(catalog_source or "curated")[:50]
                        or tuple(old_item.required_capabilities) != capabilities
                        or old_item.network_policy_ref != network
                        or old_item.sbom_ref != str(sbom_ref or "")[:256]
                        or old_item.signature_ref != str(signature_ref or "")[:256]
                        or _json(dict(old_item.metadata)) != _json(safe_metadata)
                        or _json(dict(old_item.input_schema)) != _json(normalized_input_schema)
                        or _json(dict(old_item.output_schema)) != _json(normalized_output_schema)
                        or tuple(old_item.tool_allowlist) != allowlist
                        or old_item.runtime_policy_ref != policy_ref
                        or _canonical_digest(old_item.runtime_policy_digest) != policy_digest
                    )
                )
                if old_item is not None and old_item.enabled and immutable_changed:
                    raise BundleStateConflict("enabled bundle policy cannot be replaced in place")
                approval_state = (
                    "draft"
                    if old_item is None or immutable_changed or old_item.approval_state == "revoked"
                    else old_item.approval_state
                )
                conn.execute(
                    """
                    INSERT INTO sandbox_execution_bundles(
                        bundle_ref, bundle_digest, profile_ref, approval_state, catalog_source,
                        required_capabilities_json, network_policy_ref, sbom_ref, signature_ref,
                        created_by, updated_by, created_at, updated_at, metadata_json,
                        input_schema_json, output_schema_json, tool_allowlist_json,
                        runtime_policy_ref, runtime_policy_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bundle_ref) DO UPDATE SET
                        bundle_digest = excluded.bundle_digest,
                        profile_ref = excluded.profile_ref,
                        catalog_source = excluded.catalog_source,
                        required_capabilities_json = excluded.required_capabilities_json,
                        network_policy_ref = excluded.network_policy_ref,
                        sbom_ref = excluded.sbom_ref,
                        signature_ref = excluded.signature_ref,
                        approval_state = excluded.approval_state,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at,
                        metadata_json = excluded.metadata_json,
                        input_schema_json = excluded.input_schema_json,
                        output_schema_json = excluded.output_schema_json,
                        tool_allowlist_json = excluded.tool_allowlist_json,
                        runtime_policy_ref = excluded.runtime_policy_ref,
                        runtime_policy_digest = excluded.runtime_policy_digest
                    """,
                    (
                        ref,
                        digest,
                        profile,
                        approval_state,
                        str(catalog_source or "curated")[:50],
                        _json(list(capabilities)),
                        network,
                        str(sbom_ref or "")[:256],
                        str(signature_ref or "")[:256],
                        str(actor or "")[:128],
                        str(actor or "")[:128],
                        now,
                        now,
                        _json(safe_metadata),
                        _json(normalized_input_schema),
                        _json(normalized_output_schema),
                        _json(list(allowlist)),
                        policy_ref,
                        policy_digest,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM sandbox_execution_bundles WHERE bundle_ref = ?", (ref,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_bundle(row)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _set_state(self, bundle_ref: str, state: str, *, actor: str = "") -> ExecutionBundle:
        if state not in _STATES:
            raise BundleStateConflict("invalid bundle approval state")
        item = self.require(bundle_ref)
        if state == item.approval_state:
            return item
        if state not in _STATE_TRANSITIONS.get(item.approval_state, frozenset()):
            raise BundleStateConflict(
                f"cannot transition bundle {item.approval_state} -> {state}"
            )
        if state == "enabled":
            # Publishing an executable catalog entry is the point at which all
            # deployment gates must be present.  Draft/review metadata may be
            # prepared while the global switch is off.
            try:
                require_execution_enabled(operation="sandbox bundle enable")
            except Exception as exc:  # noqa: BLE001 - keep catalog error type stable
                raise BundleStateConflict(str(exc)) from exc
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE sandbox_execution_bundles SET approval_state = ?, updated_by = ?, updated_at = ? WHERE bundle_ref = ?",
                (state, str(actor or "")[:128], now, str(bundle_ref)),
            )
        return self.require(item.bundle_ref)

    def review(self, bundle_ref: str, *, actor: str = "") -> ExecutionBundle:
        return self._set_state(bundle_ref, "reviewed", actor=actor)

    def mark_tested(self, bundle_ref: str, *, actor: str = "") -> ExecutionBundle:
        return self._set_state(bundle_ref, "tested", actor=actor)

    def enable(self, bundle_ref: str, *, actor: str = "") -> ExecutionBundle:
        item = self.require(bundle_ref)
        if item.approval_state not in {"tested", "enabled"}:
            raise BundleStateConflict("bundle must be tested before enable")
        return self._set_state(bundle_ref, "enabled", actor=actor)

    def revoke(self, bundle_ref: str, *, actor: str = "") -> ExecutionBundle:
        return self._set_state(bundle_ref, "revoked", actor=actor)

    def authorize(
        self,
        *,
        bundle_ref: str,
        bundle_digest: str,
        profile_ref: str,
        required_capabilities: Any,
        network_policy_ref: str = "",
        parameters: Mapping[str, Any] | None = None,
    ) -> ExecutionBundle:
        item = self.require(bundle_ref)
        if not item.enabled:
            raise BundleNotAuthorized("bundle is not enabled")
        if _canonical_digest(item.bundle_digest) != _canonical_digest(bundle_digest):
            raise BundleNotAuthorized("bundle digest is not the reviewed digest")
        if item.profile_ref != str(profile_ref):
            raise BundleNotAuthorized("profile does not match the reviewed bundle")
        requested = set(str(value).strip() for value in (required_capabilities or ()) if str(value).strip())
        if not requested.issubset(set(item.required_capabilities)):
            raise BundleNotAuthorized("requested capability is not approved for the bundle")
        if str(network_policy_ref or "") != item.network_policy_ref:
            raise BundleNotAuthorized("network policy does not match the reviewed bundle")
        if parameters is not None:
            try:
                validate_value(parameters, item.input_schema, path="parameters")
            except SchemaValidationError as exc:
                raise BundleNotAuthorized(str(exc)) from exc
        return item

    def validate_output(self, bundle: ExecutionBundle, manifest: Mapping[str, Any]) -> None:
        """Validate a Runner output manifest against the reviewed bundle schema."""

        try:
            validate_value(manifest, bundle.output_schema, path="output_manifest")
        except SchemaValidationError as exc:
            raise BundleNotAuthorized(str(exc)) from exc


BundleCatalog = SandboxCatalog


__all__ = [
    "BundleCatalog",
    "BundleCatalogError",
    "BundleNotAuthorized",
    "BundleNotFound",
    "BundleStateConflict",
    "ExecutionBundle",
    "SandboxCatalog",
]
