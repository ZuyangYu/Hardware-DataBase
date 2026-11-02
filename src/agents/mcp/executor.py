"""Guarded execution wrapper for external MCP tools.

Every call re-reads the current server state (enabled / department /
subscription / disabled tool), consumes the per-turn call budget, passes
through the dedicated ``mcp`` governor resource, records its own diagnostics,
events and audit, and never touches the evidence chain or retrieval ledger.
"""

from __future__ import annotations

import json
import time
from typing import Any

import src.settings as settings
from src.agents.mcp.artifacts import (
    McpArtifactRejected,
    McpArtifactStore,
    extract_artifact_payload,
)
from src.agents.mcp.client import (
    classify_mcp_error,
    get_mcp_runtime,
    load_server_tools,
    stringify_result,
    sync_call_tool,
    tool_is_read_only,
)
from src.agents.mcp.policy import McpToolBinding, mcp_prerequisites_met
from src.agents.mcp.store import McpCredentialError, build_mcp_store
from src.agents.mcp.write_store import (
    McpWriteProposal,
    arguments_digest,
    build_write_proposal_store,
)
from src.core.cancellation import QueryCancelled
from src.core.concurrency import GovernorRejected, governor_acquire
from src.observability import observe
from src.observability.metrics import (
    record_mcp_artifact_rejected,
    record_mcp_call,
    record_mcp_skipped,
    record_mcp_truncated,
    record_mcp_write,
)

_MCP_ERROR_TEXT = {
    "policy": "[外部工具不可用] code=policy_rejected",
    "quota": "[外部工具不可用] code=turn_quota_exceeded",
    "invalid_argument": "[外部工具不可用] code=invalid_argument",
    "timeout": "[外部工具不可用] code=timeout",
    "connect": "[外部工具不可用] code=connect_failed",
    "tool_error": "[外部工具不可用] code=tool_error",
    "protocol": "[外部工具不可用] code=protocol_error",
}


def _error_text(kind: str) -> str:
    return _MCP_ERROR_TEXT.get(str(kind), _MCP_ERROR_TEXT["protocol"])


def _truncate_result(text: str) -> tuple[str, bool, int]:
    """Apply char and UTF-8 byte caps, appending the truncation marker."""

    max_chars = max(1, int(getattr(settings, "MCP_MAX_RESULT_CHARS", 20000)))
    max_bytes = max(1, int(getattr(settings, "MCP_MAX_RESULT_BYTES", 65536)))
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True
    if truncated:
        text = f"{text}…[已截断]"
    return text, truncated, len(text.encode("utf-8"))


class MCPToolExecutor:
    """Per-turn wrapper: bindings in, guarded sync tools out."""

    def __init__(
        self,
        rt: Any | None = None,
        *,
        runtime: Any | None = None,
        store: Any | None = None,
        write_store: Any | None = None,
        artifact_store: Any | None = None,
        user_id: int | None = None,
        department_id: Any = None,
        username: str = "",
    ) -> None:
        # ``rt`` is required for the turn path (wrap/call/propose); the worker
        # execution path constructs the executor without a runtime.
        self.rt = rt
        self.runtime = runtime or get_mcp_runtime()
        self.store = store or build_mcp_store()
        self.write_store = write_store or build_write_proposal_store()
        self.artifact_store = artifact_store or McpArtifactStore()
        self.user_id = user_id
        self.department_id = department_id
        self.username = str(username or "")

    def _new_call_id(self) -> str:
        if self.rt is not None:
            return self.rt.next_invocation_id()
        return f"mcp-{time.monotonic_ns()}"

    def wrap(self, binding: McpToolBinding) -> Any:
        """Build the sync LangChain tool the model actually sees.

        Read tools call the guarded executor; write tools only create a
        proposal and never touch the external service in this turn.
        """

        from langchain_core.tools import StructuredTool

        description = (
            f"[外部插件 {binding.server.slug}] "
            f"{getattr(binding.raw_tool, 'description', '') or binding.tool_name}"
        )
        if binding.write:
            description = f"{description}（写工具：仅创建候选产物提案，需用户确认）"

        def _run(**kwargs: Any) -> str:
            if binding.write:
                return self.propose(binding, kwargs)
            return self.call(binding, kwargs)

        return StructuredTool(
            name=binding.safe_name,
            description=description[:1000],
            args_schema=getattr(binding.raw_tool, "args_schema", None),
            func=_run,
        )

    # ------------------------------------------------------------------
    def _write_rejection(self, binding: McpToolBinding) -> str | None:
        """Phase-2 gates re-checked at proposal time."""

        if not bool(getattr(settings, "MCP_WRITE_ENABLED", False)):
            return "write_disabled"
        try:
            record = self.store.get(binding.server.slug)
            if record is None:
                return "policy"
            if binding.tool_name not in set(record.write_tools):
                return "not_allowed_write"
            subscription = self.store.get_subscription(int(self.user_id), record.slug)
            if subscription is None or not subscription.allow_write:
                return "not_allowed_write"
        except Exception:
            return "policy"
        return None

    def propose(
        self,
        binding: McpToolBinding,
        arguments: dict[str, Any],
        *,
        call_id: str | None = None,
    ) -> str:
        """Create a write proposal; the external call happens only after confirm."""

        rt = self.rt
        if rt is None:  # pragma: no cover - worker path never proposes
            raise RuntimeError("write proposals require a turn runtime")
        rt.check_cancel()
        call_id = call_id or self._new_call_id()
        started = time.monotonic()
        rt.tool_started(
            call_id=call_id,
            tool_name=binding.safe_name,
            tool_kind="mcp",
            server=binding.server.slug,
            tool=binding.tool_name,
            transport=binding.transport,
        )
        rejection = self._policy_rejection(binding) or self._write_rejection(binding)
        if rejection is not None:
            return self._finish(binding, "rejected", rejection, started, call_id=call_id)
        if not rt.reserve_mcp_call(int(getattr(settings, "MCP_CALLS_PER_TURN", 8))):
            return self._finish(binding, "rejected", "quota", started, call_id=call_id)
        try:
            payload = json.dumps(arguments, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = str(arguments)
        if len(payload) > int(getattr(settings, "MCP_MAX_ARGUMENT_CHARS", 12000)):
            return self._finish(
                binding, "rejected", "invalid_argument", started, call_id=call_id
            )

        try:
            proposal = self.write_store.create(
                user_id=int(self.user_id or 0),
                slug=binding.server.slug,
                tool_name=binding.tool_name,
                arguments_digest=arguments_digest(arguments),
                arguments=arguments,
                artifact_hint=self._artifact_hint(binding, arguments),
                ttl_seconds=int(getattr(settings, "MCP_WRITE_CONFIRM_TTL_SECONDS", 1800)),
            )
        except Exception:
            return self._finish(binding, "rejected", "store_error", started, call_id=call_id)

        latency_ms = int((time.monotonic() - started) * 1000)
        record_mcp_write(transport=binding.transport, status="proposed")
        rt.record_mcp_diagnostic(
            {
                "tool_name": binding.safe_name,
                "server": binding.server.slug,
                "tool": binding.tool_name,
                "status": "proposed",
                "error": "",
                "latency_ms": latency_ms,
                "result_bytes": 0,
                "truncated": False,
                "proposal_id": proposal.proposal_id,
            }
        )
        rt.tool_result(
            call_id=call_id,
            tool_name=binding.safe_name,
            tool_kind="mcp",
            server=binding.server.slug,
            tool=binding.tool_name,
            transport=binding.transport,
            status="proposed",
            latency_ms=latency_ms,
            result_bytes=0,
            truncated=False,
            hit_count=0,
        )
        rt.emit(
            "mcp_write_proposal",
            {
                "card": {
                    "kind": "mcp_write_proposal",
                    "proposal_id": proposal.proposal_id,
                    "server": binding.server.slug,
                    "server_name": binding.server.name,
                    "tool": binding.tool_name,
                    "arguments_digest": proposal.arguments_digest,
                    "artifact_hint": proposal.artifact_hint,
                    "status": proposal.status,
                    "expires_at": proposal.expires_at,
                    "confirm_url": f"/api/v1/plugins/mcp/write-proposals/{proposal.proposal_id}/confirm",
                    "reject_url": f"/api/v1/plugins/mcp/write-proposals/{proposal.proposal_id}/reject",
                }
            },
        )
        self._audit_write_proposal(binding, proposal)
        rt.check_cancel()
        return (
            f"写入提案已创建（proposal_id={proposal.proposal_id}），"
            "等待用户在写入提案中确认；确认前不会调用外部服务，也不会修改任何原件。"
        )

    @staticmethod
    def _artifact_hint(binding: McpToolBinding, arguments: dict[str, Any]) -> str:
        schema = getattr(binding.raw_tool, "args_schema", None)
        fields: list[str] = []
        if isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                fields = sorted(str(key) for key in properties)[:10]
        else:
            model_fields = getattr(schema, "model_fields", None)
            if isinstance(model_fields, dict):
                fields = sorted(str(key) for key in model_fields)[:10]
        if fields:
            return ",".join(fields)[:200]
        if arguments:
            return f"args={len(arguments)}"[:200]
        return ""

    def _audit_write_proposal(
        self, binding: McpToolBinding, proposal: McpWriteProposal
    ) -> None:
        try:
            from src.core.app_logs import AppLogService

            AppLogService().record_audit(
                action="plugins.mcp.write_proposal",
                target_type="mcp_write_proposal",
                target_id=proposal.proposal_id,
                department_id=self.department_id,
                success=True,
                metadata={
                    "server": binding.server.slug,
                    "tool": binding.tool_name,
                    "transport": binding.transport,
                    "status": proposal.status,
                    "arguments_digest": proposal.arguments_digest,
                    "expires_at": proposal.expires_at,
                    "username": self.username,
                },
            )
        except Exception:
            self.rt.emit(
                "degraded",
                {
                    "stage": "mcp_audit",
                    "component": "mcp",
                    "failure_type": "audit_failed",
                    "capability": "degraded",
                    "server": binding.server.slug,
                    "tool": binding.tool_name,
                },
            )

    # ------------------------------------------------------------------
    # worker-side execution (after user confirmation)
    def execute_proposal(
        self, proposal: McpWriteProposal, *, worker_id: str = ""
    ) -> dict[str, Any]:
        """Execute one claimed proposal; returns the final status and artifact."""

        record = self.store.get(proposal.slug)
        transport = str(getattr(record, "transport", ""))
        failure = self._execution_rejection(record, proposal)
        if failure is not None:
            return self.finish_proposal(
                proposal, error_code=failure, retryable=False, transport=transport
            )
        try:
            arguments = self.write_store.decrypt_arguments(proposal)
        except McpCredentialError:
            return self.finish_proposal(
                proposal,
                error_code="arguments_unavailable",
                retryable=False,
                transport=transport,
            )

        global_timeout = float(getattr(settings, "MCP_CALL_TIMEOUT_SECONDS", 60) or 60)
        call_timeout = min(float(record.timeout_s or global_timeout), global_timeout)
        call_started = time.monotonic()

        try:
            discovered = load_server_tools(record)
            raw_tool = next(
                (
                    tool
                    for tool in discovered.tools
                    if str(getattr(tool, "name", "") or "").strip() == proposal.tool_name
                ),
                None,
            )
            if raw_tool is None or tool_is_read_only(raw_tool):
                return self.finish_proposal(
                    proposal, error_code="tool_missing", retryable=False, transport=transport
                )
            with governor_acquire("mcp", interactive=False, timeout=call_timeout):
                raw = sync_call_tool(
                    self.runtime, raw_tool, arguments, timeout=call_timeout
                )
        except GovernorRejected:
            return self.finish_proposal(
                proposal, error_code="quota", retryable=True, transport=transport
            )
        except TimeoutError:
            return self.finish_proposal(
                proposal, error_code="timeout", retryable=True, transport=transport
            )
        except Exception as exc:  # noqa: BLE001 - classified into a fixed error code
            kind = classify_mcp_error(exc)
            record_mcp_call(
                transport=transport,
                status="timeout" if kind == "timeout" else "failed",
                duration_s=0.0,
            )
            return self.finish_proposal(
                proposal,
                error_code=kind,
                retryable=kind in {"connect", "timeout", "protocol", "tool_error"},
                transport=transport,
            )

        record_mcp_call(
            transport=transport,
            status="ok",
            duration_s=max(0.0, time.monotonic() - call_started),
        )
        payload = extract_artifact_payload(raw)
        if payload is None:
            return self.finish_proposal(
                proposal, error_code="artifact_missing", retryable=False, transport=transport
            )
        try:
            artifact = self.artifact_store.save(
                user_id=proposal.user_id,
                proposal_id=proposal.proposal_id,
                filename=payload.get("filename"),
                content_base64=payload.get("content_base64"),
                content_bytes=payload.get("content_bytes"),
                sha256=payload.get("sha256"),
                media_type=payload.get("media_type"),
            )
        except McpArtifactRejected as exc:
            record_mcp_artifact_rejected(reason=exc.reason)
            return self.finish_proposal(
                proposal,
                error_code=f"artifact_{exc.reason}",
                retryable=False,
                transport=transport,
            )

        completed = self.write_store.complete(
            proposal.proposal_id,
            proposal.idempotency_key,
            artifact_ref=artifact.artifact_ref,
            artifact_sha256=artifact.sha256,
        )
        if not completed:
            return {"status": "stale", "error_code": "idempotency_conflict"}
        record_mcp_write(transport=transport, status="completed")
        self._audit_artifact(proposal, artifact.to_dict())
        self._notify(
            proposal,
            kind="mcp_write_artifact_ready",
            title=f"外部插件候选产物已就绪：{artifact.filename}",
            body=f"{proposal.slug}/{proposal.tool_name} 已生成候选产物，可在扩展页下载。",
        )
        return {
            "status": "completed",
            "proposal_id": proposal.proposal_id,
            "artifact": artifact.to_dict(),
        }

    def _execution_rejection(
        self, record: Any, proposal: McpWriteProposal
    ) -> str | None:
        if not bool(getattr(settings, "MCP_WRITE_ENABLED", False)):
            return "write_disabled"
        if record is None or not record.enabled:
            return "server_disabled"
        if not record.credentials_ok:
            return "credentials"
        if proposal.tool_name not in set(record.write_tools):
            return "not_allowed_write"
        try:
            subscription = self.store.get_subscription(proposal.user_id, proposal.slug)
        except Exception:
            return "policy"
        if subscription is None or not subscription.allow_write:
            return "not_allowed_write"
        department = self._user_department(proposal.user_id)
        if department is not None and not record.department_allows(department):
            return "department"
        return None

    @staticmethod
    def _user_department(user_id: int) -> Any:
        try:
            from src.core.auth import AuthService

            user = AuthService(db_path=settings.AUTH_DB_PATH).get_user_by_id(int(user_id))
            return getattr(user, "department_id", None)
        except Exception:
            return None

    def finish_proposal(
        self,
        proposal: McpWriteProposal,
        *,
        error_code: str,
        retryable: bool,
        transport: str = "",
    ) -> dict[str, Any]:
        status = self.write_store.fail(
            proposal.proposal_id,
            proposal.idempotency_key,
            error_code=error_code,
            retryable=retryable,
        )
        record_mcp_write(
            transport=transport,
            status=status if status in {"confirmed", "failed"} else "failed",
        )
        if status == "failed":
            self._notify(
                proposal,
                kind="mcp_write_failed",
                title="外部插件写入执行失败",
                body=f"{proposal.slug}/{proposal.tool_name} 执行失败（{error_code}），可在扩展页重新发起。",
            )
        return {
            "status": status,
            "proposal_id": proposal.proposal_id,
            "error_code": error_code,
            "retryable": retryable,
        }

    def _audit_artifact(self, proposal: McpWriteProposal, artifact: dict[str, Any]) -> None:
        try:
            from src.core.app_logs import AppLogService

            AppLogService().record_audit(
                action="plugins.mcp.artifact_received",
                target_type="mcp_write_proposal",
                target_id=proposal.proposal_id,
                department_id=self.department_id,
                success=True,
                metadata={
                    "server": proposal.slug,
                    "tool": proposal.tool_name,
                    "filename": artifact.get("filename", ""),
                    "bytes": artifact.get("bytes", 0),
                    "sha256": artifact.get("sha256", ""),
                },
            )
        except Exception:
            pass

    def _notify(
        self, proposal: McpWriteProposal, *, kind: str, title: str, body: str
    ) -> None:
        try:
            from src.core.notifications import NotificationService

            NotificationService().create(
                user_id=proposal.user_id,
                kind=kind,
                title=title,
                body=body,
                target_type="mcp_write_proposal",
                target_id=proposal.proposal_id,
                payload={"server": proposal.slug, "tool": proposal.tool_name},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    def call(
        self,
        binding: McpToolBinding,
        arguments: dict[str, Any],
        *,
        call_id: str | None = None,
    ) -> str:
        rt = self.rt
        rt.check_cancel()
        call_id = call_id or self._new_call_id()
        started = time.monotonic()
        rt.tool_started(
            call_id=call_id,
            tool_name=binding.safe_name,
            tool_kind="mcp",
            server=binding.server.slug,
            tool=binding.tool_name,
            transport=binding.transport,
        )

        rejection = self._policy_rejection(binding)
        if rejection is not None:
            return self._finish(binding, "rejected", rejection, started, call_id=call_id)
        if not rt.reserve_mcp_call(int(getattr(settings, "MCP_CALLS_PER_TURN", 8))):
            return self._finish(binding, "rejected", "quota", started, call_id=call_id)
        try:
            payload = json.dumps(arguments, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = str(arguments)
        if len(payload) > int(getattr(settings, "MCP_MAX_ARGUMENT_CHARS", 12000)):
            return self._finish(
                binding, "rejected", "invalid_argument", started, call_id=call_id
            )

        global_timeout = float(getattr(settings, "MCP_CALL_TIMEOUT_SECONDS", 60) or 60)
        call_timeout = min(float(binding.server.timeout_s or global_timeout), global_timeout)
        slug = binding.server.slug
        tool_name = binding.tool_name
        status, failure, result_text, truncated, result_bytes = "ok", "", "", False, 0

        with observe.tool(
            f"mcp.{slug}.{tool_name}",
            **{
                "mcp.server.slug": slug,
                "mcp.transport": binding.transport,
                "mcp.tool": tool_name,
                "mcp.tool.read_only": True,
            },
        ) as span:
            try:
                with governor_acquire("mcp", interactive=True, timeout=call_timeout):
                    raw = sync_call_tool(
                        self.runtime, binding.raw_tool, arguments, timeout=call_timeout
                    )
                rt.check_cancel()
                result_text, truncated, result_bytes = _truncate_result(stringify_result(raw))
            except QueryCancelled:
                # Turn cancelled: late results must not enter any record.
                raise
            except GovernorRejected:
                status, failure = "rejected", "quota"
            except TimeoutError:
                status, failure = "timeout", "timeout"
            except Exception as exc:  # noqa: BLE001 - classified into a fixed error code
                kind = classify_mcp_error(exc)
                status = "timeout" if kind == "timeout" else "failed"
                failure = kind
            span.set("mcp.result.bytes", result_bytes)
            span.set("mcp.result.truncated", truncated)
            span.set("outcome", status)

        return self._finish(
            binding,
            status,
            failure,
            started,
            call_id=call_id,
            result_text=result_text,
            truncated=truncated,
            result_bytes=result_bytes,
        )

    # ------------------------------------------------------------------
    def _policy_rejection(self, binding: McpToolBinding) -> str | None:
        """Re-check visibility at call time; any doubt rejects the call."""

        ok, _reason = mcp_prerequisites_met()
        if not ok or self.user_id is None:
            return "policy"
        try:
            record = self.store.get(binding.server.slug)
            if record is None or not record.enabled:
                return "policy"
            if not record.department_allows(self.department_id):
                return "policy"
            if not record.credentials_ok:
                return "policy"
            if binding.tool_name in set(record.disabled_tools):
                return "policy"
            subscribed = self.store.list_subscribed_slugs(int(self.user_id))
            if binding.server.slug not in subscribed:
                return "policy"
        except Exception:
            return "policy"
        return None

    def _finish(
        self,
        binding: McpToolBinding,
        status: str,
        failure: str,
        started: float,
        *,
        call_id: str,
        result_text: str = "",
        truncated: bool = False,
        result_bytes: int = 0,
    ) -> str:
        rt = self.rt
        if rt is None:  # pragma: no cover - worker path does not emit turn events
            raise RuntimeError("turn events require a runtime")
        latency_ms = int((time.monotonic() - started) * 1000)
        record_mcp_call(
            transport=binding.transport, status=status, duration_s=latency_ms / 1000.0
        )
        if status == "rejected":
            record_mcp_skipped(reason=failure or "policy")
        if truncated:
            record_mcp_truncated(transport=binding.transport)

        rt.record_mcp_diagnostic(
            {
                "tool_name": binding.safe_name,
                "server": binding.server.slug,
                "tool": binding.tool_name,
                "status": status,
                "error": failure,
                "latency_ms": latency_ms,
                "result_bytes": result_bytes,
                "truncated": truncated,
            }
        )
        rt.tool_result(
            call_id=call_id,
            tool_name=binding.safe_name,
            tool_kind="mcp",
            server=binding.server.slug,
            tool=binding.tool_name,
            transport=binding.transport,
            status="done" if status == "ok" else status,
            latency_ms=latency_ms,
            result_bytes=result_bytes,
            truncated=truncated,
            hit_count=0,
            error=failure,
        )
        if status != "ok":
            rt.emit(
                "degraded",
                {
                    "stage": "mcp_call",
                    "component": "mcp",
                    "failure_type": failure or status,
                    "capability": "unavailable",
                    "server": binding.server.slug,
                    "tool": binding.tool_name,
                },
            )
        self._audit_call(binding, status, failure, latency_ms, result_bytes, truncated)
        rt.check_cancel()
        if status == "ok":
            return result_text
        return _error_text(failure)

    def _audit_call(
        self,
        binding: McpToolBinding,
        status: str,
        failure: str,
        latency_ms: int,
        result_bytes: int,
        truncated: bool,
    ) -> None:
        try:
            from src.core.app_logs import AppLogService

            AppLogService().record_audit(
                action="plugins.mcp.call",
                target_type="mcp_server",
                target_id=binding.server.slug,
                department_id=self.department_id,
                success=status == "ok",
                error_message="" if status == "ok" else str(failure or status),
                metadata={
                    "server": binding.server.slug,
                    "tool": binding.tool_name,
                    "transport": binding.transport,
                    "status": status,
                    "latency_ms": latency_ms,
                    "result_bytes": result_bytes,
                    "truncated": truncated,
                    "username": self.username,
                },
            )
        except Exception:
            self.rt.emit(
                "degraded",
                {
                    "stage": "mcp_audit",
                    "component": "mcp",
                    "failure_type": "audit_failed",
                    "capability": "degraded",
                    "server": binding.server.slug,
                    "tool": binding.tool_name,
                },
            )


__all__ = ["MCPToolExecutor"]
