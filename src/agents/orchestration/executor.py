"""Validated, dependency-aware execution of conversational action plans."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.agents.orchestration.contracts import (
    ActionExecutionResult,
    ActionReceipt,
    ActionSpec,
    ConversationActionPlan,
    action_requires_resolved_reference,
)
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
    """

    def __init__(
        self,
        *,
        handlers: Mapping[str, ActionHandler],
        available_capabilities: set[str] | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._available_capabilities = set(available_capabilities or ())

    def execute(self, plan: ConversationActionPlan) -> ActionExecutionResult:
        forbidden = {
            str(constraint.value).strip()
            for constraint in plan.constraints
            if constraint.kind == "forbid_action"
        }
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

            for action in ready:
                pending.pop(action.action_id)
                receipt = self._execute_action(
                    action,
                    receipt_by_id,
                    forbidden,
                    has_unresolved_references=bool(plan.unresolved_refs),
                )
                receipts.append(receipt)
                receipt_by_id[action.action_id] = receipt

        return ActionExecutionResult(plan_id=plan.plan_id, receipts=receipts)

    def _execute_action(
        self,
        action: ActionSpec,
        prior: Mapping[str, ActionReceipt],
        forbidden: set[str],
        *,
        has_unresolved_references: bool = False,
    ) -> ActionReceipt:
        if any(prior[item].status not in _SUCCESS_STATUSES for item in action.depends_on):
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
            outcome = handler(action, prior)
        except PermissionError as exc:
            return ActionReceipt(
                action_id=action.action_id,
                status="blocked",
                error_code="permission_denied",
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - preserve a receipt boundary
            return ActionReceipt(
                action_id=action.action_id,
                status="failed",
                error_code="handler_error",
                message=str(exc)[:500],
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


__all__ = ["ActionHandler", "ConversationActionExecutor"]
