"""Runtime allowlist and budget enforcement for the internal Harness."""

from __future__ import annotations

from src.document_authoring.models import HarnessPolicy


class HarnessBudgetExceeded(RuntimeError):
    pass


class HarnessLeaseLost(RuntimeError):
    """Raised when a stale worker attempts a fenced state transition."""

    pass


class HarnessToolPolicy:
    def __init__(self, policy: HarnessPolicy):
        if policy.status != "approved":
            raise PermissionError("internal harness requires an approved policy")
        self.policy = policy

    def require_tool(self, tool_name: str) -> None:
        if tool_name not in self.policy.allowed_tools:
            raise PermissionError(f"harness tool is not allowlisted: {tool_name}")

    def require_step(self, step_count: int) -> None:
        if step_count > self.policy.max_steps:
            raise HarnessBudgetExceeded(f"harness max_steps exceeded: {self.policy.max_steps}")

    def require_retrieval_round(self, round_count: int) -> None:
        if round_count > self.policy.max_retrieval_rounds:
            raise HarnessBudgetExceeded(f"harness max_retrieval_rounds exceeded: {self.policy.max_retrieval_rounds}")
