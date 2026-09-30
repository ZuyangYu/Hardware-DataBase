"""External MCP plugin management (admin) and subscription (employee) routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, require_employee, require_system_admin
from src.api.schemas import (
    McpListResponse,
    McpServerCreateRequest,
    McpServerResponse,
    McpServerStatusRequest,
    McpServerUpdateRequest,
    McpSubscribeRequest,
    McpSubscriptionListResponse,
    McpSubscriptionResponse,
    McpTestResponse,
    McpToolsResponse,
    McpWriteConsentRequest,
    McpWriteProposalListResponse,
    McpWriteProposalResponse,
    McpWriteToolsRequest,
    OkResponse,
)
from src.agents.mcp.models import McpServerRecord
from src.agents.mcp.service import McpPluginService, McpServiceError
from src.core.auth import ROLE_SYSTEM_ADMIN, AuthService, AuthUser

router = APIRouter(prefix="/plugins/mcp", tags=["plugins"])


def get_mcp_plugin_service() -> McpPluginService:
    return McpPluginService()


def _raise_service_error(exc: McpServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc))


def _department_id(user: AuthUser, auth: AuthService) -> Any:
    ctx = build_context_for_user(user, auth=auth)
    metadata = dict(getattr(ctx, "metadata", {}) or {})
    return metadata.get("resource_department_id") or metadata.get("department_id")


def _record_audit(
    user: AuthUser,
    *,
    action: str,
    target_id: str,
    success: bool = True,
    error_message: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Governance audit must never break the management call itself."""

    try:
        from src.core.app_logs import AppLogService

        AppLogService().record_audit(
            action=action,
            actor=user,
            target_type="mcp_server",
            target_id=target_id,
            success=success,
            error_message=error_message[:500],
            metadata=metadata or {},
        )
    except Exception:
        pass


def _server_view(record: McpServerRecord) -> McpServerResponse:
    return McpServerResponse(server=record.admin_view())


def _list_payload(service: McpPluginService, items: list[dict[str, Any]]) -> McpListResponse:
    status = service.status()
    return McpListResponse(
        items=items,
        enabled=bool(status["enabled"]),
        write_enabled=bool(status["write_enabled"]),
        prerequisites_met=bool(status["prerequisites_met"]),
        prerequisites_reason=str(status.get("prerequisites_reason") or ""),
    )


# ---------------------------------------------------------------------------
# Shared: catalog for employees, governance list for system admins


@router.get("", response_model=McpListResponse)
def list_plugins(
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    if user.role == ROLE_SYSTEM_ADMIN:
        return _list_payload(service, [record.admin_view() for record in service.list_servers()])
    return _list_payload(
        service,
        service.catalog(user_id=user.id, department_id=_department_id(user, auth)),
    )


# ---------------------------------------------------------------------------
# Employee subscriptions (declared before /{slug} to keep literal paths stable)


@router.get("/subscriptions", response_model=McpSubscriptionListResponse)
def list_subscriptions(
    user: AuthUser = Depends(require_employee),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    return McpSubscriptionListResponse(
        items=[item.to_dict() for item in service.subscriptions(user.id)]
    )


@router.post("/{slug}/subscribe", response_model=McpSubscriptionResponse)
def subscribe(
    slug: str,
    body: McpSubscribeRequest,
    user: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        subscription = service.subscribe(
            user.id,
            slug,
            allow_write=body.allow_write,
            department_id=_department_id(user, auth),
        )
    except McpServiceError as exc:
        _record_audit(
            user,
            action="plugins.mcp.user_subscribe",
            target_id=slug,
            success=False,
            error_message=str(exc),
            metadata={"allow_write": bool(body.allow_write)},
        )
        _raise_service_error(exc)
    _record_audit(
        user,
        action="plugins.mcp.user_subscribe",
        target_id=slug,
        metadata={"allow_write": bool(body.allow_write)},
    )
    return McpSubscriptionResponse(subscription=subscription.to_dict())


@router.delete("/{slug}/subscribe", response_model=OkResponse)
def unsubscribe(
    slug: str,
    user: AuthUser = Depends(require_employee),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        removed = service.unsubscribe(user.id, slug)
    except McpServiceError as exc:
        _raise_service_error(exc)
    _record_audit(user, action="plugins.mcp.user_unsubscribe", target_id=slug)
    return OkResponse(ok=True, message="已退订" if removed else "未订阅")


@router.post("/{slug}/write-consent", response_model=McpSubscriptionResponse)
def set_write_consent(
    slug: str,
    body: McpWriteConsentRequest,
    user: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        subscription = service.set_write_consent(
            user.id,
            slug,
            body.allow_write,
            department_id=_department_id(user, auth),
        )
    except McpServiceError as exc:
        _raise_service_error(exc)
    _record_audit(
        user,
        action="plugins.mcp.write_consent",
        target_id=slug,
        metadata={"allow_write": bool(body.allow_write)},
    )
    return McpSubscriptionResponse(subscription=subscription.to_dict())


# ---------------------------------------------------------------------------
# Phase-2 write proposals (declared before /{slug} to keep literal paths stable)


@router.get("/write-proposals", response_model=McpWriteProposalListResponse)
def list_write_proposals(
    statuses: str | None = None,
    user: AuthUser = Depends(require_employee),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    wanted = [item.strip() for item in str(statuses or "").split(",") if item.strip()]
    items = service.list_write_proposals(user.id, statuses=wanted or None)
    return McpWriteProposalListResponse(
        items=[proposal.to_dict() for proposal in items]
    )


@router.post(
    "/write-proposals/{proposal_id}/confirm", response_model=McpWriteProposalResponse
)
def confirm_write_proposal(
    proposal_id: str,
    user: AuthUser = Depends(require_employee),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        proposal = service.confirm_write_proposal(user.id, proposal_id)
    except McpServiceError as exc:
        _record_audit(
            user,
            action="plugins.mcp.write_confirm",
            target_id=proposal_id,
            success=False,
            error_message=str(exc),
        )
        _raise_service_error(exc)
    _record_audit(user, action="plugins.mcp.write_confirm", target_id=proposal_id)
    return McpWriteProposalResponse(proposal=proposal.to_dict())


@router.post(
    "/write-proposals/{proposal_id}/reject", response_model=McpWriteProposalResponse
)
def reject_write_proposal(
    proposal_id: str,
    user: AuthUser = Depends(require_employee),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        proposal = service.reject_write_proposal(user.id, proposal_id)
    except McpServiceError as exc:
        _raise_service_error(exc)
    _record_audit(user, action="plugins.mcp.write_reject", target_id=proposal_id)
    return McpWriteProposalResponse(proposal=proposal.to_dict())


@router.get("/write-proposals/{proposal_id}/artifact")
def download_write_artifact(
    proposal_id: str,
    user: AuthUser = Depends(require_employee),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        content, filename = service.read_write_artifact(user.id, proposal_id)
    except McpServiceError as exc:
        _raise_service_error(exc)
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Artifact-Sha256": service.write_store.get(user.id, proposal_id).artifact_sha256,
        },
    )


# ---------------------------------------------------------------------------
# Admin governance


@router.post("", response_model=McpServerResponse)
def create_plugin(
    body: McpServerCreateRequest,
    actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        record = service.create_server(
            slug=body.slug,
            name=body.name,
            transport=body.transport,
            url=body.url,
            description=body.description,
            headers=body.headers,
            timeout_s=body.timeout_s,
            sse_read_timeout_s=body.sse_read_timeout_s,
            department_scope=body.department_scope,
            department_ids=body.department_ids,
            disabled_tools=body.disabled_tools,
            write_tools=body.write_tools,
            tags=body.tags,
            icon=body.icon,
            actor=actor.username,
        )
    except McpServiceError as exc:
        _record_audit(
            actor,
            action="plugins.mcp.create",
            target_id=str(body.slug),
            success=False,
            error_message=str(exc),
        )
        _raise_service_error(exc)
    _record_audit(
        actor,
        action="plugins.mcp.create",
        target_id=record.slug,
        metadata={"transport": record.transport},
    )
    return _server_view(record)


@router.get("/{slug}", response_model=McpServerResponse)
def get_plugin(
    slug: str,
    _actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        return _server_view(service.get_server(slug))
    except McpServiceError as exc:
        _raise_service_error(exc)


@router.put("/{slug}", response_model=McpServerResponse)
def update_plugin(
    slug: str,
    body: McpServerUpdateRequest,
    actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    fields = body.model_dump(exclude_unset=True)
    try:
        record = service.update_server(slug, fields, actor=actor.username)
    except McpServiceError as exc:
        _record_audit(
            actor,
            action="plugins.mcp.update",
            target_id=slug,
            success=False,
            error_message=str(exc),
        )
        _raise_service_error(exc)
    _record_audit(
        actor,
        action="plugins.mcp.update",
        target_id=slug,
        metadata={
            "connection_changed": not record.enabled,
            "fields": sorted(fields),
        },
    )
    return _server_view(record)


@router.delete("/{slug}", response_model=OkResponse)
def delete_plugin(
    slug: str,
    actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        removed = service.delete_server(slug)
    except McpServiceError as exc:
        _raise_service_error(exc)
    _record_audit(actor, action="plugins.mcp.delete", target_id=slug)
    return OkResponse(ok=bool(removed), message="已删除" if removed else "未找到")


@router.put("/{slug}/status", response_model=McpServerResponse)
def set_plugin_status(
    slug: str,
    body: McpServerStatusRequest,
    actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        record = service.set_enabled(slug, body.enabled, actor=actor.username)
    except McpServiceError as exc:
        _record_audit(
            actor,
            action="plugins.mcp.enable" if body.enabled else "plugins.mcp.disable",
            target_id=slug,
            success=False,
            error_message=str(exc),
        )
        _raise_service_error(exc)
    _record_audit(
        actor,
        action="plugins.mcp.enable" if body.enabled else "plugins.mcp.disable",
        target_id=slug,
    )
    return _server_view(record)


@router.post("/{slug}/test", response_model=McpTestResponse)
def test_plugin(
    slug: str,
    actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        result = service.test_server(slug)
    except McpServiceError as exc:
        _record_audit(
            actor,
            action="plugins.mcp.test",
            target_id=slug,
            success=False,
            error_message=str(exc),
        )
        _raise_service_error(exc)
    _record_audit(
        actor,
        action="plugins.mcp.test",
        target_id=slug,
        metadata={"tool_count": result.get("tool_count", 0)},
    )
    return McpTestResponse(**result)


@router.get("/{slug}/tools", response_model=McpToolsResponse)
def list_plugin_tools(
    slug: str,
    _actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        infos = service.list_tools(slug)
    except McpServiceError as exc:
        _raise_service_error(exc)
    return McpToolsResponse(items=[info.to_dict() for info in infos])


@router.post("/{slug}/refresh", response_model=McpToolsResponse)
def refresh_plugin_tools(
    slug: str,
    actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    """Force re-discovery after clearing this process's cache entry."""

    try:
        infos = service.refresh_tools(slug)
    except McpServiceError as exc:
        _record_audit(
            actor,
            action="plugins.mcp.refresh",
            target_id=slug,
            success=False,
            error_message=str(exc),
        )
        _raise_service_error(exc)
    _record_audit(
        actor,
        action="plugins.mcp.refresh",
        target_id=slug,
        metadata={"tool_count": len(infos)},
    )
    return McpToolsResponse(items=[info.to_dict() for info in infos])


@router.put("/{slug}/tools/{tool_name}/toggle", response_model=McpToolsResponse)
def toggle_plugin_tool(
    slug: str,
    tool_name: str,
    actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        _enabled, _record = service.toggle_tool(slug, tool_name, actor=actor.username)
        infos = service.list_tools(slug)
    except McpServiceError as exc:
        _raise_service_error(exc)
    _record_audit(
        actor,
        action="plugins.mcp.tool_toggle",
        target_id=slug,
        metadata={"tool": tool_name},
    )
    return McpToolsResponse(items=[info.to_dict() for info in infos])


@router.put("/{slug}/write-tools", response_model=McpServerResponse)
def set_plugin_write_tools(
    slug: str,
    body: McpWriteToolsRequest,
    actor: AuthUser = Depends(require_system_admin),
    service: McpPluginService = Depends(get_mcp_plugin_service),
):
    try:
        record = service.set_write_tools(slug, body.write_tools, actor=actor.username)
    except McpServiceError as exc:
        _raise_service_error(exc)
    _record_audit(
        actor,
        action="plugins.mcp.write_tools",
        target_id=slug,
        metadata={"write_tools": list(record.write_tools)},
    )
    return _server_view(record)
