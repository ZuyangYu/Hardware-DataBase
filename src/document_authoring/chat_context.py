"""Versioned, server-owned context carried by document-authoring chat turns.

The browser is allowed to submit only immutable references.  Tenant, owner and
the effective permission are derived again from the authenticated request at
this boundary and are never trusted from client JSON.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DOCUMENT_CONTEXT_VERSION = "v1"
DOCUMENT_AUTHORING_CONTEXT_VERSION = "v2"
DEFAULT_DOCUMENT_CONTEXT_TTL_SECONDS = 30 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class DocumentContextInput(BaseModel):
    """The only document context shape accepted from a client.

    Deliberately does not define tenant/user/permission fields.  Pydantic's
    ``extra='forbid'`` makes attempts to smuggle those fields fail closed.
    """

    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(min_length=1, max_length=200)
    template_version_id: str = Field(min_length=1, max_length=200)
    knowledge_base_name: str = Field(min_length=1, max_length=200)
    version: str | int = DOCUMENT_CONTEXT_VERSION
    expiry: str | None = None
    client_request_id: str = Field(default="", max_length=128)
    generation_session_id: str | None = Field(default=None, max_length=200)

    @field_validator(
        "analysis_id", "template_version_id", "knowledge_base_name", "client_request_id",
        "generation_session_id", mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        if value is None:
            return value
        return str(value).strip()

    @field_validator("version")
    @classmethod
    def _version(cls, value: str | int) -> str:
        normalized = str(value).strip().lower()
        if normalized in {"1", "v1"}:
            return DOCUMENT_CONTEXT_VERSION
        raise ValueError("unsupported document context version")


class DocumentAuthoringContextInput(BaseModel):
    """Client reference for template-free or template-bound authoring.

    Unlike the v1 shape, every source and template reference is optional.  A
    v2 context is still useful when a conversation has only an authorized
    knowledge-base/attachment scope, or when it resumes an existing task or
    OutputSpec.  Tenant, owner and permissions are deliberately absent from
    this input and are populated by :func:`build_document_context`.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal["v2"] = DOCUMENT_AUTHORING_CONTEXT_VERSION
    knowledge_base_name: str | None = Field(default=None, max_length=200)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    source_scope: Literal[
        "auto",
        "attachment_only",
        "knowledge_base_only",
        "attachment_and_knowledge_base",
    ] = "auto"
    task_id: str | None = Field(default=None, max_length=200)
    generation_session_id: str | None = Field(default=None, max_length=200)
    output_spec_id: str | None = Field(default=None, max_length=200)
    output_spec_version: int | None = Field(default=None, ge=1)
    template_version_id: str | None = Field(default=None, max_length=200)
    analysis_id: str | None = Field(default=None, max_length=200)
    expiry: str | None = None
    client_request_id: str = Field(default="", max_length=128)

    @field_validator(
        "knowledge_base_name",
        "task_id",
        "generation_session_id",
        "output_spec_id",
        "template_version_id",
        "analysis_id",
        "client_request_id",
        mode="before",
    )
    @classmethod
    def _strip_optional_text(cls, value: Any) -> Any:
        if value is None:
            return None
        return str(value).strip() or None

    @field_validator("attachment_ids", mode="before")
    @classmethod
    def _normalize_attachment_ids(cls, value: Any) -> list[str]:
        values = value or []
        if not isinstance(values, (list, tuple, set)):
            raise ValueError("attachment_ids must be a list")
        normalized = [str(item).strip() for item in values if str(item).strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("attachment_ids must be unique")
        return normalized

    @model_validator(mode="after")
    def _require_scope_or_task(self) -> "DocumentAuthoringContextInput":
        if not (
            self.knowledge_base_name
            or self.attachment_ids
            or self.task_id
            or self.generation_session_id
            or self.output_spec_id
        ):
            raise ValueError(
                "v2 document context requires an authorized source or task reference"
            )
        if (self.output_spec_id is None) != (self.output_spec_version is None):
            raise ValueError("output_spec_id and output_spec_version must be provided together")
        return self


class DocumentContext(BaseModel):
    """Canonical context persisted on a ChatTurn and passed to tools."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["v1"] = DOCUMENT_CONTEXT_VERSION
    tenant_id: str = Field(min_length=1, max_length=200)
    owner_user_id: str = Field(min_length=1, max_length=200)
    knowledge_base_name: str = Field(min_length=1, max_length=200)
    analysis_id: str = Field(min_length=1, max_length=200)
    template_version_id: str = Field(min_length=1, max_length=200)
    generation_session_id: str | None = Field(default=None, max_length=200)
    created_at: str
    expiry: str
    permission_use: Literal["read", "write"] = "read"
    client_request_id: str = Field(min_length=1, max_length=128)

    @property
    def expired(self) -> bool:
        return _parse_timestamp(self.expiry, name="expiry") <= _now()

    def assert_scope(
        self,
        *,
        ctx: Any,
        expected_kb: str | None = None,
        required_permission: Literal["read", "write"] = "read",
    ) -> None:
        """Re-authorize the context immediately before a tool operation."""

        owner = str(getattr(ctx, "user_id", "") or "")
        tenant = str(
            getattr(ctx, "tenant_id", None)
            or (getattr(ctx, "metadata", {}) or {}).get("tenant_id")
            or "default"
        )
        kb = str(expected_kb or self.knowledge_base_name).strip()
        if self.owner_user_id != owner or self.tenant_id != tenant:
            raise PermissionError("document context owner or tenant mismatch")
        if kb != self.knowledge_base_name:
            raise PermissionError("document context knowledge base mismatch")
        if not getattr(ctx, "has_kb_permission", lambda *_args: False)(kb, required_permission):
            raise PermissionError(f"knowledge base {required_permission} permission is required")
        if required_permission == "write" and self.expired:
            raise PermissionError("document context has expired")


class DocumentAuthoringContext(BaseModel):
    """Server-owned v2 authoring context with an optional template."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["v2"] = DOCUMENT_AUTHORING_CONTEXT_VERSION
    tenant_id: str = Field(min_length=1, max_length=200)
    owner_user_id: str = Field(min_length=1, max_length=200)
    knowledge_base_name: str | None = Field(default=None, max_length=200)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    source_scope: Literal[
        "auto",
        "attachment_only",
        "knowledge_base_only",
        "attachment_and_knowledge_base",
    ] = "auto"
    task_id: str | None = Field(default=None, max_length=200)
    generation_session_id: str | None = Field(default=None, max_length=200)
    output_spec_id: str | None = Field(default=None, max_length=200)
    output_spec_version: int | None = Field(default=None, ge=1)
    template_version_id: str | None = Field(default=None, max_length=200)
    analysis_id: str | None = Field(default=None, max_length=200)
    created_at: str
    expiry: str
    permission_use: Literal["read", "write"] = "read"
    client_request_id: str = Field(min_length=1, max_length=128)

    @property
    def expired(self) -> bool:
        return _parse_timestamp(self.expiry, name="expiry") <= _now()

    @property
    def has_template(self) -> bool:
        return bool(self.template_version_id and self.analysis_id)

    @property
    def has_source_scope(self) -> bool:
        return bool(self.knowledge_base_name or self.attachment_ids)

    def assert_scope(
        self,
        *,
        ctx: Any,
        expected_kb: str | None = None,
        required_permission: Literal["read", "write"] = "read",
    ) -> None:
        owner = str(getattr(ctx, "user_id", "") or "")
        tenant = str(
            getattr(ctx, "tenant_id", None)
            or (getattr(ctx, "metadata", {}) or {}).get("tenant_id")
            or "default"
        )
        if self.owner_user_id != owner or self.tenant_id != tenant:
            raise PermissionError("document context owner or tenant mismatch")
        expected = str(expected_kb or "").strip()
        if expected and self.knowledge_base_name and expected != self.knowledge_base_name:
            raise PermissionError("document context knowledge base mismatch")
        kb = str(self.knowledge_base_name or expected).strip()
        if kb and not getattr(ctx, "has_kb_permission", lambda *_args: False)(
            kb, required_permission
        ):
            raise PermissionError(f"knowledge base {required_permission} permission is required")
        if required_permission == "write" and self.expired:
            raise PermissionError("document context has expired")


# Explicit aliases make the versioned boundary discoverable to API clients and
# keep hidden/third-party integrations from depending on one spelling.
DocumentContextV2Input: TypeAlias = DocumentAuthoringContextInput
DocumentContextV2: TypeAlias = DocumentAuthoringContext


def build_document_context(
    raw: DocumentContextInput
    | DocumentAuthoringContextInput
    | dict[str, Any]
    | DocumentContext
    | DocumentAuthoringContext,
    *,
    ctx: Any,
    expected_kb: str | None = None,
    ttl_seconds: int = DEFAULT_DOCUMENT_CONTEXT_TTL_SECONDS,
    now: datetime | None = None,
) -> DocumentContext | DocumentAuthoringContext:
    """Create canonical server-owned context from a client reference."""

    point = (now or _now()).astimezone(timezone.utc)
    if isinstance(raw, DocumentContext):
        context = raw
        context.assert_scope(ctx=ctx, expected_kb=expected_kb, required_permission="read")
        return context
    if isinstance(raw, DocumentAuthoringContext):
        raw.assert_scope(ctx=ctx, expected_kb=expected_kb, required_permission="read")
        return raw
    raw_mapping = raw if isinstance(raw, dict) else raw.model_dump(mode="json")
    is_v2 = str(raw_mapping.get("version", "")).strip().lower() == "v2"
    if is_v2:
        client_v2 = (
            raw if isinstance(raw, DocumentAuthoringContextInput)
            else DocumentAuthoringContextInput.model_validate(raw_mapping)
        )
        kb = str(expected_kb or client_v2.knowledge_base_name or "").strip() or None
        if expected_kb and client_v2.knowledge_base_name and client_v2.knowledge_base_name != expected_kb:
            raise PermissionError("document context knowledge base mismatch")
        if kb and not getattr(ctx, "has_kb_permission", lambda *_args: False)(kb, "read"):
            raise PermissionError("knowledge base read permission is required")
        point = (now or _now()).astimezone(timezone.utc)
        maximum = point + timedelta(seconds=max(60, int(ttl_seconds)))
        if client_v2.expiry:
            expiry = _parse_timestamp(client_v2.expiry, name="expiry")
            expiry = min(expiry, maximum)
        else:
            expiry = maximum
        tenant = str(
            getattr(ctx, "tenant_id", None)
            or (getattr(ctx, "metadata", {}) or {}).get("tenant_id")
            or "default"
        )
        owner = str(getattr(ctx, "user_id", "") or "")
        if not owner or owner == "anonymous":
            raise PermissionError("authenticated document context is required")
        request_id = client_v2.client_request_id
        if not request_id:
            request_id = "document-authoring-context-" + hashlib.sha256(
                (
                    f"{tenant}\0{owner}\0{kb or ''}\0"
                    f"{','.join(client_v2.attachment_ids)}\0{client_v2.output_spec_id or ''}"
                ).encode()
            ).hexdigest()[:32]
        return DocumentAuthoringContext(
            version=DOCUMENT_AUTHORING_CONTEXT_VERSION,
            tenant_id=tenant,
            owner_user_id=owner,
            knowledge_base_name=kb,
            attachment_ids=list(client_v2.attachment_ids),
            source_scope=client_v2.source_scope,
            task_id=client_v2.task_id,
            generation_session_id=client_v2.generation_session_id,
            output_spec_id=client_v2.output_spec_id,
            output_spec_version=client_v2.output_spec_version,
            template_version_id=client_v2.template_version_id,
            analysis_id=client_v2.analysis_id,
            created_at=point.isoformat(),
            expiry=expiry.isoformat(),
            permission_use="read",
            client_request_id=request_id,
        )
    client = raw if isinstance(raw, DocumentContextInput) else DocumentContextInput.model_validate(raw_mapping)
    kb = str(expected_kb or client.knowledge_base_name).strip()
    if client.knowledge_base_name != kb:
        raise PermissionError("document context knowledge base mismatch")
    if not getattr(ctx, "has_kb_permission", lambda *_args: False)(kb, "read"):
        raise PermissionError("knowledge base read permission is required")

    maximum = point + timedelta(seconds=max(60, int(ttl_seconds)))
    if client.expiry:
        expiry = _parse_timestamp(client.expiry, name="expiry")
        # A client may shorten the lease, but never extend the server TTL.
        expiry = min(expiry, maximum)
    else:
        expiry = maximum
    tenant = str(
        getattr(ctx, "tenant_id", None)
        or (getattr(ctx, "metadata", {}) or {}).get("tenant_id")
        or "default"
    )
    owner = str(getattr(ctx, "user_id", "") or "")
    if not owner or owner == "anonymous":
        raise PermissionError("authenticated document context is required")
    request_id = client.client_request_id
    if not request_id:
        request_id = "document-context-" + hashlib.sha256(
            f"{tenant}\0{owner}\0{kb}\0{client.analysis_id}\0{client.template_version_id}".encode()
        ).hexdigest()[:32]
    return DocumentContext(
        version=DOCUMENT_CONTEXT_VERSION,
        tenant_id=tenant,
        owner_user_id=owner,
        knowledge_base_name=kb,
        analysis_id=client.analysis_id,
        template_version_id=client.template_version_id,
        generation_session_id=client.generation_session_id,
        created_at=point.isoformat(),
        expiry=expiry.isoformat(),
        permission_use="read",
        client_request_id=request_id,
    )


__all__ = [
    "DOCUMENT_CONTEXT_VERSION",
    "DOCUMENT_AUTHORING_CONTEXT_VERSION",
    "DocumentContext",
    "DocumentContextInput",
    "DocumentAuthoringContext",
    "DocumentAuthoringContextInput",
    "DocumentContextV2",
    "DocumentContextV2Input",
    "build_document_context",
]
