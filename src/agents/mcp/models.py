"""Data models and redaction views for external MCP plugins.

Credentials only exist decrypted inside :class:`McpServerRecord`; every view
that can leave the process (admin JSON, employee catalog, runtime connection
config) is produced here so a caller cannot accidentally serialize raw headers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID_TRANSPORTS = ("streamable_http", "sse")
VALID_DEPARTMENT_SCOPES = ("none", "departments", "all")

# Display marker for credentials that must never be echoed back verbatim.
MASKED_VALUE = "***"


def mask_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Return header names with masked values, preserving key visibility."""

    return {str(key): MASKED_VALUE for key in (headers or {})}


@dataclass(frozen=True)
class McpToolInfo:
    """Management view of one discovered MCP tool."""

    name: str
    description: str = ""
    read_only: bool = False
    disabled: bool = False
    write_allowed: bool = False
    blocked_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
            "disabled": self.disabled,
            "write_allowed": self.write_allowed,
            "blocked_reason": self.blocked_reason,
        }


@dataclass(frozen=True)
class McpSubscription:
    """One user's opt-in record for one MCP server."""

    user_id: int
    slug: str
    allow_write: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "allow_write": self.allow_write,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class McpServerRecord:
    """One MCP server row with decrypted credentials and derived errors.

    ``headers_error`` is set when stored credentials cannot be decrypted (for
    example after the key was rotated without re-saving).  Such a server stays
    visible to the administrator but is skipped by the runtime policy instead
    of breaking the whole catalog.
    """

    id: int
    slug: str
    name: str
    description: str = ""
    transport: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout_s: int | None = None
    sse_read_timeout_s: int | None = None
    enabled: bool = False
    department_scope: str = "none"
    department_ids: list[int] = field(default_factory=list)
    disabled_tools: list[str] = field(default_factory=list)
    write_tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    icon: str = ""
    connection_fingerprint: str = ""
    last_test_fingerprint: str = ""
    last_test_ok_at: str = ""
    last_test_error: str = ""
    headers_error: str = ""
    created_by: str = ""
    updated_by: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def credentials_ok(self) -> bool:
        """Whether runtime connection may use this record's credentials."""

        return not self.headers_error

    def test_passed_for_current_config(self) -> bool:
        """Enable precondition: the last successful test matches this config."""

        return bool(
            self.last_test_fingerprint
            and self.connection_fingerprint
            and self.last_test_fingerprint == self.connection_fingerprint
        )

    def department_allows(self, department_id: object) -> bool:
        """Fail-closed department check against the current request context."""

        if self.department_scope == "all":
            return True
        if self.department_scope != "departments":
            return False
        try:
            current = int(department_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return current in set(self.department_ids)

    def runtime_config(self) -> dict[str, Any]:
        """Connection parameters for the MCP client; never serialized to HTTP."""

        config: dict[str, Any] = {"transport": self.transport, "url": self.url}
        if self.headers:
            config["headers"] = dict(self.headers)
        if self.timeout_s:
            config["timeout"] = float(self.timeout_s)
        if self.sse_read_timeout_s:
            config["sse_read_timeout"] = float(self.sse_read_timeout_s)
        return config

    def admin_view(self) -> dict[str, Any]:
        """``system_admin`` view: URL visible, header values masked."""

        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "transport": self.transport,
            "url": self.url,
            "headers": mask_headers(self.headers),
            "has_credentials": bool(self.headers),
            "timeout_s": self.timeout_s,
            "sse_read_timeout_s": self.sse_read_timeout_s,
            "enabled": self.enabled,
            "department_scope": self.department_scope,
            "department_ids": list(self.department_ids),
            "disabled_tools": list(self.disabled_tools),
            "write_tools": list(self.write_tools),
            "tags": list(self.tags),
            "icon": self.icon,
            "connection_fingerprint": self.connection_fingerprint,
            "last_test_fingerprint": self.last_test_fingerprint,
            "last_test_ok_at": self.last_test_ok_at,
            "last_test_error": self.last_test_error,
            "credentials_error": self.headers_error,
            "test_passed_for_current_config": self.test_passed_for_current_config(),
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def catalog_view(
        self,
        *,
        subscribed: bool = False,
        allow_write: bool = False,
        tool_count: int = 0,
        read_only_tool_count: int = 0,
    ) -> dict[str, Any]:
        """Employee catalog view: display fields only, never connection data."""

        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "tags": list(self.tags),
            "tool_count": int(tool_count),
            "read_only_tool_count": int(read_only_tool_count),
            "subscribed": bool(subscribed),
            "allow_write": bool(allow_write),
            "writable": bool(self.write_tools),
        }


__all__ = [
    "MASKED_VALUE",
    "McpServerRecord",
    "McpSubscription",
    "McpToolInfo",
    "VALID_DEPARTMENT_SCOPES",
    "VALID_TRANSPORTS",
    "mask_headers",
]
