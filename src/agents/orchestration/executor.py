"""Validated, dependency-aware execution of conversational action plans."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import src.settings
from src.agents.orchestration.contracts import (
    ActionExecutionResult,
    ActionReceipt,
    ActionSpec,
    ConversationActionPlan,
    ResolvedAction,
    action_requires_resolved_reference,
    resolve_action,
)
from src.agents.orchestration.receipts import ActionExecutionScope, ActionReceiptStore
from src.agents.orchestration.capabilities import capability_for_action


ActionHandler = Callable[[ActionSpec, Mapping[str, ActionReceipt]], Any]

_ACTION_ALIASES = {
    "request_release": "release",
    "create_document": "generate",
    "revise_document": "revise",
}
_SUCCESS_STATUSES = {"accepted", "completed"}


class ConversationActionExecutor:
    """Dispatch only server-validated actions through injected domain handlers.

    The executor deliberately knows no storage, files, or framework tools. It
    owns dependency ordering and safety checks; each handler remains the
    permission-checked adapter for its domain service.

    When a persistent ``receipt_store`` and an ``execution_scope`` are given
    (P2-001), durable outcomes are reused instead of re-dispatched and every
    executed action is recorded under the stable
    ``(tenant_id, session_generation, turn_id, plan_version, action_id)`` key.
    """

    def __init__(
        self,
        *,
        handlers: Mapping[str, ActionHandler],
        available_capabilities: set[str] | None = None,
        receipt_store: ActionReceiptStore | None = None,
        execution_scope: ActionExecutionScope | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._available_capabilities = set(available_capabilities or ())
        self._receipt_store = receipt_store
        self._execution_scope = execution_scope

    def execute(self, plan: ConversationActionPlan) -> ActionExecutionResult:
        forbidden = {
            str(constraint.value).strip()
            for constraint in plan.constraints
            if constraint.kind == "forbid_action"
        }
        actions_by_id = {action.action_id: action for action in plan.actions}
        resolved = {
            action.action_id: resolve_action(action, actions_by_id) for action in plan.actions
        }
        parallel_enabled = bool(getattr(src.settings, "ACTION_DAG_PARALLEL_ENABLED", False))
        max_parallel = max(1, int(getattr(src.settings, "ACTION_EXECUTOR_MAX_PARALLEL", 4)))
        reuse_enabled = self._receipt_store is not None and self._execution_scope is not None
        if parallel_enabled and not reuse_enabled:
            # The persistent receipt contract is delivered but not wired into
            # this executor instance; record it so production rollout can see
            # that parallel dispatch is running without recovery receipts.
            self._record_contract_missing()
        if reuse_enabled:
            try:
                self._receipt_store.save_plan(self._execution_scope, plan)
            except Exception:
                pass
        pending = {action.action_id: action for action in plan.actions}
        receipts: list[ActionReceipt] = []
        receipt_by_id: dict[str, ActionReceipt] = {}

        while pending:
            ready = [
                action for action in pending.values()
                if all(dependency in receipt_by_id for dependency in action.depends_on)
            ]
            if not ready:
                for action in pending.values():
                    receipt = ActionReceipt(
                        action_id=action.action_id,
                        status="blocked",
                        error_code="dependency_cycle",
                        message="action dependencies cannot be resolved",
                    )
                    receipts.append(receipt)
                    receipt_by_id[action.action_id] = receipt
                break

            batch_results: dict[str, ActionReceipt] = {}
            to_run: list[ActionSpec] = []
            for action in ready:
                reused = self._reused_receipt(plan, action)
                if reused is not None:
                    batch_results[action.action_id] = reused
                else:
                    to_run.append(action)
            if parallel_enabled and len(to_run) > 1:
                layer_results = self._execute_parallel_layer(
                    to_run,
                    resolved,
                    receipt_by_id,
                    forbidden,
                    has_unresolved_references=bool(plan.unresolved_refs),
                    max_parallel=max_parallel,
                    plan_id=plan.plan_id,
                )
                for receipt in layer_results.values():
                    self._persist_receipt(plan, receipt)
                batch_results.update(layer_results)
            for action in ready:
                pending.pop(action.action_id)
                receipt = batch_results.get(action.action_id)
                if receipt is None:
                    receipt = self._execute_action(
                        action,
                        receipt_by_id,
                        forbidden,
                        has_unresolved_references=bool(plan.unresolved_refs),
                        required_statuses=(
                            resolved[action.action_id].dependency_requirements
                            if parallel_enabled
                            else None
                        ),
                        idempotency_key=self._idempotency_key(plan.plan_id, action.action_id),
                        supports_idempotency=self._handler_supports_idempotency(action.kind),
                    )
                    self._persist_receipt(plan, receipt)
                receipts.append(receipt)
                receipt_by_id[action.action_id] = receipt

        if reuse_enabled:
            try:
                self._receipt_store.mark_plan_status(
                    self._execution_scope, plan.plan_id, _plan_status(receipts)
                )
            except Exception:
                pass
        return ActionExecutionResult(plan_id=plan.plan_id, receipts=receipts)

    # -- persistent receipt integration (P2-001) ---------------------------

    def _idempotency_key(self, plan_id: str, action_id: str) -> str:
        if self._execution_scope is None:
            return ""
        key = self._execution_scope.key_for(plan_id, action_id)
        return "|".join(str(part) for part in key)

    def _reused_receipt(self, plan: ConversationActionPlan, action: ActionSpec) -> ActionReceipt | None:
        if self._receipt_store is None or self._execution_scope is None:
            return None
        stored = self._receipt_store.get(self._execution_scope, plan.plan_id, action.action_id)
        if stored is None or stored.replayable:
            return None
        self._record_receipt_reused(stored.status)
        return stored.to_action_receipt()

    def _persist_receipt(self, plan: ConversationActionPlan, receipt: ActionReceipt) -> None:
        if self._receipt_store is None or self._execution_scope is None:
            return
        job_ref = ""
        data = getattr(receipt, "data", None)
        if isinstance(data, Mapping):
            job_ref = str(data.get("job_ref") or "")
        self._receipt_store.upsert(
            self._execution_scope,
            plan.plan_id,
            receipt,
            job_ref=job_ref,
            idempotency_key=self._idempotency_key(plan.plan_id, receipt.action_id),
        )

    def _handler_supports_idempotency(self, kind: str) -> bool:
        handler = self._handlers.get(kind)
        if handler is None:
            return False
        return _accepts_idempotency_key(handler)

    @staticmethod
    def _record_contract_missing() -> None:
        try:
            from src.observability.metrics import counter

            counter(
                "hdb.action.parallel.contract_missing",
                description="Parallel dispatch ran without a wired receipt store",
            )
        except Exception:
            pass

    @staticmethod
    def _record_receipt_reused(status: str) -> None:
        try:
            from src.observability.metrics import counter

            counter(
                "hdb.action.receipt.reused",
                attributes={"status": status},
                description="Actions skipped because a durable receipt already exists",
            )
        except Exception:
            pass

    def _execute_parallel_layer(
        self,
        ready: list[ActionSpec],
        resolved: Mapping[str, ResolvedAction],
        receipt_by_id: Mapping[str, ActionReceipt],
        forbidden: set[str],
        *,
        has_unresolved_references: bool,
        max_parallel: int,
        plan_id: str = "",
    ) -> dict[str, ActionReceipt]:
        """Run independent read-only actions concurrently; writes stay serial.

        Every action in the batch sees the same read-only snapshot of prior
        receipts and results are merged in the stable plan order.
        """
        snapshot = dict(receipt_by_id)
        parallel_actions: list[ActionSpec] = []
        used_keys: set[str] = set()
        for action in ready:
            info = resolved[action.action_id]
            if info.access_mode != "read_only":
                continue
            key = info.resource_key or f"read:{action.action_id}"
            if key in used_keys:
                continue
            used_keys.add(key)
            parallel_actions.append(action)
        if len(parallel_actions) < 2:
            return {}
        results: dict[str, ActionReceipt] = {}
        workers = min(max_parallel, len(parallel_actions))
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hdb-action") as pool:
            futures = {
                pool.submit(
                    self._execute_action,
                    action,
                    snapshot,
                    forbidden,
                    has_unresolved_references=has_unresolved_references,
                    required_statuses=resolved[action.action_id].dependency_requirements,
                    idempotency_key=self._idempotency_key(plan_id, action.action_id),
                    supports_idempotency=self._handler_supports_idempotency(action.kind),
                ): action.action_id
                for action in parallel_actions
            }
            for future, action_id in futures.items():
                try:
                    results[action_id] = future.result()
                except Exception as exc:  # noqa: BLE001 - receipt boundary per action
                    results[action_id] = ActionReceipt(
                        action_id=action_id,
                        status="failed",
                        error_code="parallel_handler_error",
                        message=str(exc)[:500],
                    )
        failed = any(receipt.status == "failed" for receipt in results.values())
        try:
            from src.observability.metrics import record_action_parallel

            record_action_parallel(
                layer_size=len(parallel_actions),
                duration_s=time.monotonic() - started,
                status="failed" if failed else "ok",
            )
        except Exception:
            pass
        return results

    def _execute_action(
        self,
        action: ActionSpec,
        prior: Mapping[str, ActionReceipt],
        forbidden: set[str],
        *,
        has_unresolved_references: bool = False,
        required_statuses: Mapping[str, str] | None = None,
        idempotency_key: str = "",
        supports_idempotency: bool = False,
    ) -> ActionReceipt:
        if required_statuses is not None:
            for dependency in action.depends_on:
                requirement = str(required_statuses.get(dependency, "completed"))
                status = prior[dependency].status if dependency in prior else ""
                satisfied = (
                    status == "completed"
                    if requirement == "completed"
                    else status in _SUCCESS_STATUSES
                )
                if not satisfied:
                    return ActionReceipt(
                        action_id=action.action_id,
                        status="skipped",
                        error_code="dependency_not_completed",
                        message="a prerequisite action did not complete",
                    )
        elif any(prior[item].status not in _SUCCESS_STATUSES for item in action.depends_on):
            return ActionReceipt(
                action_id=action.action_id,
                status="skipped",
                error_code="dependency_not_completed",
                message="a prerequisite action did not complete",
            )
        action_alias = _ACTION_ALIASES.get(action.kind, action.kind)
        if action.kind in forbidden or action_alias in forbidden:
            return ActionReceipt(
                action_id=action.action_id,
                status="blocked",
                error_code="forbidden_action",
                message="the user explicitly forbade this action",
            )
        if has_unresolved_references and action_requires_resolved_reference(action.kind):
            return ActionReceipt(
                action_id=action.action_id,
                status="blocked",
                error_code="unresolved_reference",
                message="the action target is ambiguous; clarification is required before writing",
            )
        # ``required_capabilities`` is model-provided metadata.  Never trust
        # its presence as the authorization boundary: every action kind has a
        # server-owned canonical capability that must be present as well.
        canonical_capability = capability_for_action(action.kind)
        required = {
            canonical_capability,
            *(str(capability).strip() for capability in action.required_capabilities),
        }
        missing = sorted(
            capability
            for capability in required
            if capability and capability not in self._available_capabilities
        )
        if missing:
            return ActionReceipt(
                action_id=action.action_id,
                status="blocked",
                error_code="unsupported_capability",
                message="missing capabilities: " + ", ".join(missing),
            )
        handler = self._handlers.get(action.kind)
        if handler is None:
            return ActionReceipt(
                action_id=action.action_id,
                status="rejected",
                error_code="unsupported_action",
                message=f"no handler is registered for {action.kind}",
            )
        try:
            outcome = _invoke_handler(handler, action, prior, idempotency_key)
        except PermissionError as exc:
            return ActionReceipt(
                action_id=action.action_id,
                status="blocked",
                error_code="permission_denied",
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - preserve a receipt boundary
            # A handler that cannot prove idempotent lookup may have partially
            # submitted work before failing: never auto-replay it as-is.
            data = {} if supports_idempotency else {"needs_reconciliation": True}
            return ActionReceipt(
                action_id=action.action_id,
                status="failed",
                error_code="handler_error",
                message=str(exc)[:500],
                data=data,
            )
        return self._receipt_from_outcome(action.action_id, outcome)

    @staticmethod
    def _receipt_from_outcome(action_id: str, outcome: Any) -> ActionReceipt:
        if isinstance(outcome, ActionReceipt):
            return outcome.model_copy(update={"action_id": action_id})
        if not isinstance(outcome, Mapping):
            return ActionReceipt(action_id=action_id, status="completed")
        status = str(outcome.get("status") or "completed").strip().lower()
        if status not in {"accepted", "completed", "blocked", "failed", "skipped", "rejected"}:
            status = "completed"
        refs = [str(item) for item in outcome.get("result_refs", []) if str(item)]
        return ActionReceipt(
            action_id=action_id,
            status=status,
            result_refs=refs,
            error_code=str(outcome.get("error_code") or ""),
            message=str(outcome.get("message") or ""),
            data=dict(outcome.get("data") or {}),
        )


def _plan_status(receipts: list[ActionReceipt]) -> str:
    """Plan status after one execution pass (P2-001 recovery classification)."""
    if any(receipt.status in {"failed", "blocked", "rejected"} for receipt in receipts):
        return "blocked"
    if any(receipt.status == "skipped" for receipt in receipts):
        return "awaiting_domain"
    return "completed"


def _accepts_idempotency_key(handler: ActionHandler) -> bool:
    try:
        parameters = inspect.signature(handler).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "idempotency_key"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _invoke_handler(
    handler: ActionHandler,
    action: ActionSpec,
    prior: Mapping[str, ActionReceipt],
    idempotency_key: str,
) -> Any:
    """Pass the stable key only to handlers that can use it for dedup."""
    if idempotency_key and _accepts_idempotency_key(handler):
        return handler(action, prior, idempotency_key=idempotency_key)  # type: ignore[call-arg]
    return handler(action, prior)


__all__ = ["ActionHandler", "ConversationActionExecutor"]
