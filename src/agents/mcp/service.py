"""Business facade for external MCP plugin governance and subscriptions.

Routes stay thin: this module owns validation, the enable precondition
(test-fingerprint), tool governance projection and subscription rules.  It is
also the single place where management discovery happens, so the runtime
policy path and the admin path cannot drift apart.
"""

from __future__ import annotations

import time
from typing import Any

import src.settings as settings
from src.agents.mcp.artifacts import McpArtifactRejected, McpArtifactStore
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.client import McpDiscoveryError, load_server_tools, tool_is_read_only
from src.agents.mcp.models import McpServerRecord, McpSubscription, McpToolInfo
from src.agents.mcp.policy import mcp_prerequisites_met
from src.agents.mcp.store import (
    McpCredentialError,
    McpServerExistsError,
    McpServerNotFoundError,
    build_mcp_store,
)
from src.agents.mcp.validation import (
    McpValidationError,
    normalize_slug,
    validate_department_scope,
    validate_headers,
    validate_name,
    validate_timeout,
    validate_transport,
    validate_url,
)
from src.agents.mcp.write_store import (
    McpWriteProposal,
    build_write_proposal_store,
)


class McpServiceError(Exception):
    """Domain error carrying the HTTP status the route should surface."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def _clean_names(values: Any, *, limit: int = 200) -> list[str]:
    cleaned: list[str] = []
    for item in values or ():
        name = str(item or "").strip()
        if name and name not in cleaned:
            cleaned.append(name)
        if len(cleaned) >= limit:
            break
    return cleaned


def _clean_tags(values: Any) -> list[str]:
    tags: list[str] = []
    for item in values or ():
        tag = str(item or "").strip()[:32]
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= 10:
            break
    return tags


class McpPluginService:
    """Admin governance + employee subscription operations for MCP plugins."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        write_store: Any | None = None,
        artifact_store: Any | None = None,
    ) -> None:
        self.store = store or build_mcp_store()
        self.write_store = write_store or build_write_proposal_store()
        self.artifact_store = artifact_store or McpArtifactStore()

    # ------------------------------------------------------------------
    # status / admin reads
    def status(self) -> dict[str, Any]:
        met, reason = mcp_prerequisites_met()
        return {
            "enabled": bool(getattr(settings, "MCP_ENABLED", False)),
            "write_enabled": bool(getattr(settings, "MCP_WRITE_ENABLED", False)),
            "prerequisites_met": met,
            "prerequisites_reason": reason,
            "credentials_key_configured": bool(
                getattr(settings, "MCP_CREDENTIALS_KEY", "") or ""
            ),
        }

    def list_servers(self) -> list[McpServerRecord]:
        return self.store.list_all()

    def get_server(self, slug: str) -> McpServerRecord:
        record = self.store.get(str(slug or "").strip())
        if record is None:
            raise McpServiceError(f"未找到 MCP 服务：{slug}", status_code=404)
        return record

    # ------------------------------------------------------------------
    # admin writes
    def create_server(
        self,
        *,
        slug: Any,
        name: Any,
        transport: Any = "streamable_http",
        url: Any,
        description: Any = "",
        headers: Any = None,
        timeout_s: Any = None,
        sse_read_timeout_s: Any = None,
        department_scope: Any = "none",
        department_ids: Any = (),
        disabled_tools: Any = (),
        write_tools: Any = (),
        tags: Any = (),
        icon: Any = "",
        actor: str = "",
    ) -> McpServerRecord:
        try:
            normalized_slug = normalize_slug(slug)
            scope, scope_ids = validate_department_scope(department_scope, department_ids)
            return self.store.create(
                slug=normalized_slug,
                name=validate_name(name),
                description=str(description or "")[:1000],
                transport=validate_transport(transport),
                url=validate_url(
                    url,
                    allow_insecure_http=bool(settings.MCP_ALLOW_INSECURE_HTTP),
                    allow_private_hosts=bool(settings.MCP_ALLOW_PRIVATE_HOSTS),
                ),
                headers=validate_headers(headers),
                timeout_s=validate_timeout(timeout_s, field="timeout_s"),
                sse_read_timeout_s=validate_timeout(
                    sse_read_timeout_s, field="sse_read_timeout_s"
                ),
                department_scope=scope,
                department_ids=scope_ids,
                disabled_tools=_clean_names(disabled_tools),
                write_tools=_clean_names(write_tools),
                tags=_clean_tags(tags),
                icon=str(icon or "")[:16],
                created_by=str(actor or ""),
            )
        except McpValidationError as exc:
            raise McpServiceError(str(exc)) from exc
        except McpServerExistsError as exc:
            raise McpServiceError(str(exc), status_code=409) from exc
        except McpCredentialError as exc:
            raise McpServiceError(str(exc), status_code=400) from exc

    def update_server(
        self, slug: str, fields: dict[str, Any], *, actor: str = ""
    ) -> McpServerRecord:
        record = self.get_server(slug)
        updates: dict[str, Any] = {}
        try:
            if "name" in fields:
                updates["name"] = validate_name(fields["name"])
            if "description" in fields:
                updates["description"] = str(fields["description"] or "")[:1000]
            if "transport" in fields:
                updates["transport"] = validate_transport(fields["transport"])
            if "url" in fields:
                updates["url"] = validate_url(
                    fields["url"],
                    allow_insecure_http=bool(settings.MCP_ALLOW_INSECURE_HTTP),
                    allow_private_hosts=bool(settings.MCP_ALLOW_PRIVATE_HOSTS),
                )
            if fields.get("clear_headers"):
                updates["headers"] = None
            elif "headers" in fields:
                updates["headers"] = validate_headers(fields["headers"])
            if "timeout_s" in fields:
                updates["timeout_s"] = validate_timeout(fields["timeout_s"], field="timeout_s")
            if "sse_read_timeout_s" in fields:
                updates["sse_read_timeout_s"] = validate_timeout(
                    fields["sse_read_timeout_s"], field="sse_read_timeout_s"
                )
            if "department_scope" in fields or "department_ids" in fields:
                updates["department_scope"], updates["department_ids"] = (
                    validate_department_scope(
                        fields.get("department_scope", record.department_scope),
                        fields.get("department_ids", record.department_ids),
                    )
                )
            if "disabled_tools" in fields:
                updates["disabled_tools"] = _clean_names(fields["disabled_tools"])
            if "write_tools" in fields:
                updates["write_tools"] = _clean_names(fields["write_tools"])
            if "tags" in fields:
                updates["tags"] = _clean_tags(fields["tags"])
            if "icon" in fields:
                updates["icon"] = str(fields["icon"] or "")[:16]
            return self.store.update(slug, updated_by=str(actor or ""), **updates)
        except McpValidationError as exc:
            raise McpServiceError(str(exc)) from exc
        except McpCredentialError as exc:
            raise McpServiceError(str(exc), status_code=400) from exc

    def delete_server(self, slug: str) -> bool:
        self.get_server(slug)
        return self.store.delete(slug)

    def set_enabled(self, slug: str, enabled: bool, *, actor: str = "") -> McpServerRecord:
        record = self.get_server(slug)
        if enabled:
            if not bool(getattr(settings, "MCP_ENABLED", False)):
                raise McpServiceError(
                    "MCP 总开关未启用（MCP_ENABLED=false），不能启用单个服务", status_code=409
                )
            if not record.credentials_ok:
                raise McpServiceError(
                    f"凭据不可用，无法启用：{record.headers_error or '未知原因'}", status_code=409
                )
            if not record.test_passed_for_current_config():
                raise McpServiceError(
                    "当前配置尚未通过连接测试，请先执行测试连接", status_code=409
                )
        return self.store.set_enabled(slug, bool(enabled), str(actor or ""))

    def test_server(self, slug: str) -> dict[str, Any]:
        """Test the current config without bypassing the global MCP kill switch.

        Registration and governance reads remain available while MCP is off, but
        this endpoint must not create an external connection unless the operator
        explicitly enables the separate break-glass diagnostic flag.
        """

        record = self.get_server(slug)
        if not bool(getattr(settings, "MCP_ENABLED", False)) and not bool(
            getattr(settings, "SANDBOX_DIAGNOSTIC_TEST_ENABLED", False)
        ):
            raise McpServiceError(
                "MCP 总开关未启用，连接测试不会发起外部连接；如需 break-glass 诊断，请显式启用诊断开关",
                status_code=409,
            )
        started = time.monotonic()
        try:
            discovered = load_server_tools(record, force_refresh=True)
        except McpDiscoveryError as exc:
            self.store.record_test_result(
                slug,
                ok=False,
                error=f"{exc.kind}: {exc}"[:500],
                fingerprint=record.connection_fingerprint,
            )
            raise McpServiceError(
                f"连接测试失败（{exc.kind}）：{exc}", status_code=502
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        self.store.record_test_result(
            slug, ok=True, fingerprint=record.connection_fingerprint
        )
        stats = dict(discovered.stats)
        return {
            "ok": True,
            "tool_count": int(stats.get("total", len(discovered.tools))),
            "read_only_tool_count": int(stats.get("read_only", 0)),
            "blocked_count": len(record.disabled_tools),
            "latency_ms": latency_ms,
            "message": "连接成功",
        }

    def list_tools(self, slug: str) -> list[McpToolInfo]:
        if not bool(getattr(settings, "MCP_ENABLED", False)):
            raise McpServiceError(
                "MCP 总开关未启用，工具发现不会发起外部连接", status_code=409
            )
        record = self.get_server(slug)
        try:
            discovered = load_server_tools(record, force_refresh=True)
        except McpDiscoveryError as exc:
            raise McpServiceError(
                f"工具发现失败（{exc.kind}）：{exc}", status_code=502
            ) from exc
        disabled = set(record.disabled_tools)
        write_tools = set(record.write_tools)
        write_channel = bool(getattr(settings, "MCP_WRITE_ENABLED", False))
        infos: list[McpToolInfo] = []
        for tool in discovered.tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name:
                continue
            read_only = tool_is_read_only(tool)
            is_disabled = name in disabled
            write_allowed = name in write_tools
            if is_disabled:
                blocked_reason = "disabled"
            elif not read_only and not write_allowed:
                blocked_reason = "not_read_only"
            elif write_allowed and not write_channel:
                blocked_reason = "write_disabled"
            else:
                blocked_reason = ""
            infos.append(
                McpToolInfo(
                    name=name,
                    description=str(getattr(tool, "description", "") or "")[:500],
                    read_only=read_only,
                    disabled=is_disabled,
                    write_allowed=write_allowed,
                    blocked_reason=blocked_reason,
                )
            )
        return infos

    def refresh_tools(self, slug: str) -> list[McpToolInfo]:
        """Force re-discovery and drop this process's cached entry first."""

        if not bool(getattr(settings, "MCP_ENABLED", False)):
            raise McpServiceError(
                "MCP 总开关未启用，工具刷新不会发起外部连接", status_code=409
            )
        record = self.get_server(slug)
        get_tool_cache().clear(record.slug)
        return self.list_tools(slug)

    def toggle_tool(
        self, slug: str, tool_name: str, *, actor: str = ""
    ) -> tuple[bool, McpServerRecord]:
        try:
            return self.store.toggle_tool(slug, tool_name, str(actor or ""))
        except McpServerNotFoundError as exc:
            raise McpServiceError(f"未找到 MCP 服务：{slug}", status_code=404) from exc
        except ValueError as exc:
            raise McpServiceError(str(exc)) from exc

    def set_write_tools(
        self, slug: str, write_tools: Any, *, actor: str = ""
    ) -> McpServerRecord:
        self.get_server(slug)
        return self.store.set_write_tools(slug, _clean_names(write_tools), str(actor or ""))

    # ------------------------------------------------------------------
    # employee reads / subscription
    def catalog(self, *, user_id: int | None, department_id: Any) -> list[dict[str, Any]]:
        """Employee catalog: display fields only, never connection data.

        Tool counts come from the discovery cache; a cold cache shows 0 instead
        of making the catalog page perform network calls.
        """

        if not bool(getattr(settings, "MCP_ENABLED", False)):
            return []
        subscriptions = {
            item.slug: item
            for item in (
                self.store.list_subscriptions(int(user_id)) if user_id is not None else []
            )
        }
        cache = get_tool_cache()
        items: list[dict[str, Any]] = []
        for record in self.store.list_enabled():
            if not record.department_allows(department_id):
                continue
            cached = cache.get(record.slug, record.connection_fingerprint)
            stats = dict(cached.stats) if cached is not None else {}
            subscription = subscriptions.get(record.slug)
            items.append(
                record.catalog_view(
                    subscribed=subscription is not None,
                    allow_write=bool(subscription.allow_write) if subscription else False,
                    tool_count=int(stats.get("total", 0)),
                    read_only_tool_count=int(stats.get("read_only", 0)),
                )
            )
        items.sort(key=lambda item: str(item.get("name") or ""))
        return items

    def subscriptions(self, user_id: int) -> list[McpSubscription]:
        return self.store.list_subscriptions(int(user_id))

    def subscribe(
        self,
        user_id: int,
        slug: str,
        *,
        allow_write: bool = False,
        department_id: Any = None,
    ) -> McpSubscription:
        if not bool(getattr(settings, "MCP_ENABLED", False)):
            raise McpServiceError("MCP 扩展未启用", status_code=409)
        record = self.get_server(slug)
        if not record.enabled:
            raise McpServiceError("该服务未启用", status_code=409)
        if not record.department_allows(department_id):
            raise McpServiceError("该服务不在你所在部门的可见范围内", status_code=403)
        if allow_write:
            if not bool(getattr(settings, "MCP_WRITE_ENABLED", False)):
                raise McpServiceError("写工具通道未启用", status_code=409)
            if not record.write_tools:
                raise McpServiceError("该服务没有可用的写工具", status_code=409)
        return self.store.upsert_subscription(
            int(user_id), slug, allow_write=bool(allow_write)
        )

    def unsubscribe(self, user_id: int, slug: str) -> bool:
        self.get_server(slug)
        return self.store.delete_subscription(int(user_id), slug)

    def set_write_consent(
        self,
        user_id: int,
        slug: str,
        allow_write: bool,
        *,
        department_id: Any = None,
    ) -> McpSubscription:
        if self.store.get_subscription(int(user_id), slug) is None:
            raise McpServiceError("请先订阅该服务", status_code=404)
        return self.subscribe(
            user_id, slug, allow_write=bool(allow_write), department_id=department_id
        )

    # ------------------------------------------------------------------
    # phase-2 write proposals (candidate artifacts)
    def list_write_proposals(
        self,
        user_id: int,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[McpWriteProposal]:
        self.write_store.expire_overdue()
        return self.write_store.list_for_user(int(user_id), statuses=statuses, limit=limit)

    def confirm_write_proposal(
        self, user_id: int, proposal_id: str
    ) -> McpWriteProposal:
        if not bool(getattr(settings, "MCP_WRITE_ENABLED", False)):
            raise McpServiceError("写工具通道未启用", status_code=409)
        proposal, code = self.write_store.confirm(int(user_id), proposal_id)
        if code == "not_found" or proposal is None:
            raise McpServiceError("未找到写入提案", status_code=404)
        if code == "expired":
            raise McpServiceError("写入提案已过期，请重新发起", status_code=409)
        if code != "ok":
            raise McpServiceError(
                f"写入提案当前状态为 {proposal.status}，不能确认", status_code=409
            )
        return proposal

    def reject_write_proposal(self, user_id: int, proposal_id: str) -> McpWriteProposal:
        proposal, code = self.write_store.reject(int(user_id), proposal_id)
        if code == "not_found" or proposal is None:
            raise McpServiceError("未找到写入提案", status_code=404)
        if code == "expired":
            raise McpServiceError("写入提案已过期", status_code=409)
        if code != "ok":
            raise McpServiceError(
                f"写入提案当前状态为 {proposal.status}，不能拒绝", status_code=409
            )
        return proposal

    def read_write_artifact(self, user_id: int, proposal_id: str) -> tuple[bytes, str]:
        proposal = self.write_store.get(int(user_id), proposal_id)
        if proposal is None:
            raise McpServiceError("未找到写入提案", status_code=404)
        if proposal.status != "completed" or not proposal.artifact_ref:
            raise McpServiceError("候选产物尚未就绪", status_code=409)
        try:
            content = self.artifact_store.read(proposal.artifact_ref)
        except FileNotFoundError as exc:
            raise McpServiceError("候选产物不存在", status_code=404) from exc
        except McpArtifactRejected as exc:
            raise McpServiceError("候选产物不可读", status_code=409) from exc
        return content, str(proposal.artifact_ref).rsplit("/", 1)[-1]


__all__ = ["McpPluginService", "McpServiceError"]
