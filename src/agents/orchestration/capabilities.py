"""Resolve the least privilege capability set for one conversation turn.

Effective capability is always the intersection of what is deployed, what the
caller is authorized to use, the selected evidence scope, the current object
state, and the user's explicit constraints.  This is intentionally a pure
module so the runner, tools, and action-plan validator share one answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

import src.settings as settings
from src.agents.orchestration.contracts import ActionKind


CapabilityStatus = Literal["available", "unavailable"]


# Every action type has a single canonical capability.  Adapters may enforce
# further domain-specific checks (ACL, version/CAS, workflow gate) at call
# time, but no action may bypass this first boundary.
ACTION_CAPABILITIES: dict[ActionKind, str] = {
    "search": "evidence.search",
    "inspect": "evidence.read",
    "compare": "evidence.compare",
    "summarize": "evidence.summarize",
    "create_document": "document.authoring",
    "revise_document": "document.revise",
    "revise_attachment": "attachment.revise",
    "export_result": "result.export",
    "get_status": "document.task.read",
    "pause": "document.task.control",
    "cancel": "document.task.control",
    "resume": "document.task.resume",
    "retry": "document.task.control",
    "rebase_sources": "document.rebase",
    "request_release": "document.release.request",
    "resolve_decision": "document.scope.resolve",
}

_CAPABILITY_KINDS = {
    "evidence.search": "retrieval",
    "evidence.read": "retrieval",
    "evidence.compare": "retrieval",
    "evidence.summarize": "retrieval",
    "attachment.search": "retrieval",
    "attachment.revise": "document",
    "knowledge_base.catalog": "retrieval",
    "knowledge_base.search": "retrieval",
    "document.authoring": "document",
    "document.revise": "document",
    "document.task.read": "control",
    "document.task.control": "control",
    "document.task.resume": "control",
    "document.rebase": "document",
    "document.release.request": "release",
    "document.scope.resolve": "document",
    "result.export": "export",
    # External MCP plugins: tools that leave the local evidence boundary and
    # therefore get their own capability kind.  ``external_tool.write`` is the
    # phase-2 candidate-artifact channel and is deployed only when the write
    # switch is on.
    "external_tool.invoke": "external",
    "external_tool.write": "external",
}

_ACTION_ALIASES = {
    "generate": "create_document",
    "create": "create_document",
    "revise": "revise_document",
    "modify": "revise_document",
    "export": "export_result",
    "release": "request_release",
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


def _normalise_action(action: ActionKind | str) -> str:
    key = str(action or "").strip()
    return _ACTION_ALIASES.get(key, key)


def capability_for_action(action: ActionKind | str) -> str:
    """Return the canonical capability for an action, or empty if unknown."""

    return ACTION_CAPABILITIES.get(_normalise_action(action), "")


def capabilities_for_forbidden_actions(actions: Iterable[str]) -> set[str]:
    """Translate explicit no-action constraints into the capabilities to drop."""

    return {
        capability
        for capability in (capability_for_action(action) for action in actions)
        if capability
    }


def _constraint_value(constraint: Any) -> str:
    if isinstance(constraint, str):
        return constraint.strip()
    kind = getattr(constraint, "kind", "")
    if kind == "forbid_action":
        return str(getattr(constraint, "value", "")).strip()
    if isinstance(constraint, Mapping) and constraint.get("kind") == "forbid_action":
        return str(constraint.get("value", "")).strip()
    return ""


def _state_has(state: Mapping[str, object], *keys: str) -> bool:
    return any(bool(state.get(key)) for key in keys)


def compute_effective_capabilities(
    *,
    deployed: Iterable[str],
    authorized: Iterable[str],
    source_scope: str = "auto",
    object_state: Mapping[str, object] | None = None,
    accepted_constraints: Iterable[Any] | None = None,
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

    state = dict(object_state or {})
    if not _state_has(state, "artifact_ref", "candidate_artifact_ref"):
        effective.discard("document.revise")
        effective.discard("document.release.request")
    if not _state_has(state, "task_ref", "document_task_ref"):
        effective.difference_update({
            "document.task.read",
            "document.task.control",
            "document.task.resume",
            "document.rebase",
            "document.scope.resolve",
        })

    forbidden = {
        value
        for value in (_constraint_value(item) for item in accepted_constraints or ())
        if value
    }
    effective.difference_update(capabilities_for_forbidden_actions(forbidden))
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
    if bool(getattr(settings, "AGENT_DOCUMENT_TOOLS_ENABLED", False)):
        deployed.update({
            "document.authoring",
            "document.scope.resolve",
        })
        if bool(getattr(settings, "DOCUMENT_PLANNING_V2_ENABLED", False)):
            deployed.add("document.revise")
        if bool(getattr(settings, "DOCUMENT_TASK_READ_ENABLED", True)):
            deployed.add("document.task.read")
        if bool(getattr(settings, "DOCUMENT_TASK_WRITE_ENABLED", True)):
            deployed.update({
                "document.task.control",
                "document.task.resume",
                "document.rebase",
                "document.release.request",
            })
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
    has_document_context: bool,
    has_task: bool,
    has_candidate_artifact: bool = False,
    allow_document_write: bool = True,
    has_mcp_access: bool = False,
    has_mcp_write_access: bool = False,
) -> set[str]:
    """Derive capability grants from already authenticated turn facts.

    This intentionally accepts booleans rather than a request object so the
    authorization decision remains explicit and testable.  It does *not*
    replace the ACL/CAS checks in each domain service; it only determines what
    the conversational model and action planner may attempt.
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
            "attachment.search",
        })
        if allow_document_write:
            authorized.add("attachment.revise")

    # Attachment-only authoring still has a server-normalized owner-bound
    # context.  Its tools re-check attachment ACLs; do not make a KB write
    # permission a prerequisite for that valid scope.
    can_write_document = (
        allow_document_write
        and has_document_context
        and (has_kb_write or has_attachments)
    )
    if can_write_document:
        authorized.add("document.authoring")
    if has_document_context and (has_kb_read or has_attachments):
        authorized.add("document.task.read")
    if has_task and can_write_document:
        authorized.update({
            "document.scope.resolve",
            "document.task.control",
            "document.task.resume",
            "document.rebase",
        })
    if has_candidate_artifact and can_write_document:
        authorized.update({
            "document.revise",
            "document.release.request",
        })
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
        object_state: Mapping[str, object] | None = None,
        accepted_constraints: Iterable[Any] | None = None,
    ) -> set[str]:
        return compute_effective_capabilities(
            deployed=self.deployed,
            authorized=authorized,
            source_scope=source_scope,
            object_state=object_state,
            accepted_constraints=accepted_constraints,
        )

    def describe(
        self,
        *,
        authorized: Iterable[str],
        source_scope: str = "auto",
        object_state: Mapping[str, object] | None = None,
        accepted_constraints: Iterable[Any] | None = None,
    ) -> list[CapabilityDescriptor]:
        authorized_set = {str(item).strip() for item in authorized if str(item).strip()}
        effective = self.effective(
            authorized=authorized_set,
            source_scope=source_scope,
            object_state=object_state,
            accepted_constraints=accepted_constraints,
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
    "ACTION_CAPABILITIES",
    "CapabilityDescriptor",
    "CapabilityResolver",
    "authorized_capabilities_for_conversation",
    "capabilities_for_forbidden_actions",
    "capability_for_action",
    "compute_effective_capabilities",
    "deployed_capabilities_from_settings",
]
