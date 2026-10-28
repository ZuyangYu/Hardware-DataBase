"""Resolve the least privilege capability set for one conversation turn.

Effective capability is always the intersection of what is deployed, what the
caller is authorized to use, and the selected evidence scope.  This is
intentionally a pure module so the runner and tools share one answer.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

import src.settings as settings


CapabilityStatus = Literal["available", "unavailable"]

_CAPABILITY_KINDS = {
    "evidence.search": "retrieval",
    "evidence.read": "retrieval",
    "evidence.compare": "retrieval",
    "evidence.summarize": "retrieval",
    "attachment.search": "retrieval",
    "attachment.revise": "document",
    "knowledge_base.catalog": "retrieval",
    "knowledge_base.search": "retrieval",
    "result.export": "export",
    # External MCP plugins: tools that leave the local evidence boundary and
    # therefore get their own capability kind.
    "external_tool.invoke": "external",
    "external_tool.write": "external",
}


class CapabilityDescriptor(BaseModel):
    """Stable, read-only capability description for a conversation client."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str
    kind: str = ""
    scope: str = ""
    status: CapabilityStatus = "unavailable"
    reason_code: str = ""
    version: str = "1"


def compute_effective_capabilities(
    *,
    deployed: Iterable[str],
    authorized: Iterable[str],
    source_scope: str = "auto",
) -> set[str]:
    """Return capabilities that remain usable in this exact request context."""

    effective = {
        str(capability).strip()
        for capability in deployed
        if str(capability).strip()
    } & {
        str(capability).strip()
        for capability in authorized
        if str(capability).strip()
    }
    scope = str(source_scope or "auto").strip()
    if scope == "attachment_only":
        effective = {
            capability
            for capability in effective
            if not capability.startswith("knowledge_base.")
        }
    elif scope == "knowledge_base_only":
        effective = {
            capability
            for capability in effective
            if not capability.startswith("attachment.")
        }
    return effective


def deployed_capabilities_from_settings() -> set[str]:
    """Return capabilities this process is configured to provide."""

    deployed = {
        "attachment.search",
        "attachment.revise",
        "evidence.search",
        "evidence.read",
        "evidence.compare",
        "evidence.summarize",
        "knowledge_base.catalog",
        "knowledge_base.search",
        "result.export",
    }
    # MCP only exists on the unified tool path and relies on the resource
    # governor for its independent quota; a half-enabled deployment must not
    # advertise a capability that cannot execute.
    if (
        bool(getattr(settings, "MCP_ENABLED", False))
        and bool(getattr(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", False))
        and bool(getattr(settings, "RESOURCE_GOVERNOR_ENABLED", False))
    ):
        deployed.add("external_tool.invoke")
        if bool(getattr(settings, "MCP_WRITE_ENABLED", False)):
            deployed.add("external_tool.write")
    return deployed


def authorized_capabilities_for_conversation(
    *,
    has_kb_read: bool,
    has_kb_write: bool,
    has_attachments: bool,
    has_mcp_access: bool = False,
    has_mcp_write_access: bool = False,
) -> set[str]:
    """Derive capability grants from already authenticated turn facts.

    This intentionally accepts booleans rather than a request object so the
    authorization decision remains explicit and testable.  It does *not*
    replace the ACL/CAS checks in each domain service; it only determines what
    the conversational model may attempt.
    """

    authorized: set[str] = set()
    if has_kb_read:
        authorized.update({
            "evidence.search",
            "evidence.read",
            "evidence.compare",
            "evidence.summarize",
            "knowledge_base.catalog",
            "knowledge_base.search",
            "result.export",
        })
    if has_attachments:
        authorized.update({
            "evidence.search",
            "evidence.read",
            "evidence.compare",
            "attachment.search",
            "attachment.revise",
        })
    if has_kb_write:
        authorized.add("result.export")
    # The caller resolves the server snapshot (role/department/subscription)
    # before capability computation, so this only mirrors an already
    # authenticated turn fact; it cannot widen visibility by itself.
    if has_mcp_access:
        authorized.add("external_tool.invoke")
    # Write access additionally requires the user's per-server consent (the
    # caller derives it from the resolved snapshot, never from the model).
    if has_mcp_access and has_mcp_write_access:
        authorized.add("external_tool.write")
    return authorized


class CapabilityResolver:
    """Compute and describe capabilities without being able to widen them."""

    def __init__(self, *, deployed: Iterable[str], version: str = "1") -> None:
        self.deployed = {str(item).strip() for item in deployed if str(item).strip()}
        self.version = str(version or "1")

    def effective(
        self,
        *,
        authorized: Iterable[str],
        source_scope: str = "auto",
    ) -> set[str]:
        return compute_effective_capabilities(
            deployed=self.deployed,
            authorized=authorized,
            source_scope=source_scope,
        )

    def describe(
        self,
        *,
        authorized: Iterable[str],
        source_scope: str = "auto",
    ) -> list[CapabilityDescriptor]:
        authorized_set = {str(item).strip() for item in authorized if str(item).strip()}
        effective = self.effective(
            authorized=authorized_set,
            source_scope=source_scope,
        )
        return [
            CapabilityDescriptor(
                capability_id=capability,
                kind=_CAPABILITY_KINDS.get(capability, "other"),
                scope=str(source_scope),
                status="available" if capability in effective else "unavailable",
                reason_code="" if capability in effective else "context_restricted",
                version=self.version,
            )
            for capability in sorted(self.deployed)
        ]


__all__ = [
    "CapabilityDescriptor",
    "CapabilityResolver",
    "authorized_capabilities_for_conversation",
    "compute_effective_capabilities",
    "deployed_capabilities_from_settings",
]
