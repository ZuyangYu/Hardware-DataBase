"""Connection budget for the single-host control plane (v3 §9.5).

Every process counts explicitly, including unpooled connections (Governor
short transactions, Memory store, migration/ops sessions).  The arithmetic is
pure so it can be reviewed, unit-tested and checked against the live
``max_connections`` before raising replica counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConnectionBudget:
    entries: dict[str, int] = field(default_factory=dict)
    reserve: int = 0

    @property
    def processes_total(self) -> int:
        return sum(self.entries.values())

    @property
    def total(self) -> int:
        return self.processes_total + max(0, int(self.reserve))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": dict(self.entries),
            "processes_total": self.processes_total,
            "reserve": max(0, int(self.reserve)),
            "total": self.total,
        }


def connection_budget(
    *,
    api_replicas: int = 1,
    api_control_pool: int = 8,
    api_checkpoint_pool: int = 4,
    api_unpooled: int = 4,
    worker_replicas: int = 1,
    worker_control_pool: int = 8,
    worker_checkpoint_pool: int = 4,
    worker_unpooled: int = 4,
    extra_processes: dict[str, int] | None = None,
    reserve: int = 0,
) -> ConnectionBudget:
    """Sum worst-case connections per process class.

    ``*_unpooled`` covers short-lived direct connections (Governor permits,
    Memory store, health probes) — they are peak concurrency, not pool caps.
    """
    budget = ConnectionBudget(reserve=max(0, int(reserve)))
    budget.entries["api"] = max(0, int(api_replicas)) * (
        max(0, int(api_control_pool))
        + max(0, int(api_checkpoint_pool))
        + max(0, int(api_unpooled))
    )
    budget.entries["worker"] = max(0, int(worker_replicas)) * (
        max(0, int(worker_control_pool))
        + max(0, int(worker_checkpoint_pool))
        + max(0, int(worker_unpooled))
    )
    for name, value in (extra_processes or {}).items():
        budget.entries[str(name)] = max(0, int(value))
    return budget


def budget_fits(
    budget: ConnectionBudget,
    *,
    max_connections: int,
    minimum_headroom_ratio: float = 0.3,
) -> tuple[bool, str]:
    """Check the budget against ``max_connections`` and a headroom margin."""
    limit = int(max_connections)
    if limit <= 0:
        return False, "max_connections must be positive"
    headroom = max(0.0, float(minimum_headroom_ratio))
    allowed = int(limit * (1.0 - headroom))
    if budget.total > allowed:
        return (
            False,
            f"budget={budget.total} exceeds {allowed} "
            f"({int(headroom * 100)}% headroom under max_connections={limit})",
        )
    return True, f"budget={budget.total} fits within {allowed} (max_connections={limit})"


def settings_connection_budget(
    settings,
    *,
    api_replicas: int = 1,
    worker_replicas: int = 1,
    reserve: int = 0,
) -> ConnectionBudget:
    """Budget from live settings with conservative defaults.

    Pools that are not yet wired (control-plane store) are counted at their
    planned defaults; ``AGENT_CHECKPOINT_POOL_SIZE`` is counted once per
    process for agent checkpoints and once more when the authoring
    checkpointer uses PostgreSQL in workers.
    """
    checkpoint_pool = int(getattr(settings, "AGENT_CHECKPOINT_POOL_SIZE", 4) or 4)
    control_pool = int(getattr(settings, "CONTROL_PLANE_POOL_SIZE", 8) or 8)
    memory_pg = str(getattr(settings, "MEMORY_STORE_BACKEND", "sqlite")).lower() == "postgres"
    checkpoint_pg = str(getattr(settings, "AGENT_CHECKPOINT_BACKEND", "sqlite")).lower() == "postgres"
    # Pointer schema setup/commit hold an advisory-lock connection while their
    # bookkeeping transaction uses another connection. Include these transient
    # peaks separately from the official saver pool, plus maintenance callers.
    pointer_peak = 2 * max(1, int(getattr(settings, "RESOURCE_CHAT_MAX_CONCURRENT", 4))) + 2
    return connection_budget(
        api_replicas=api_replicas,
        api_control_pool=control_pool,
        api_checkpoint_pool=checkpoint_pool,
        api_unpooled=4,
        worker_replicas=worker_replicas,
        worker_control_pool=control_pool,
        worker_checkpoint_pool=checkpoint_pool,
        worker_unpooled=4,
        extra_processes={
            "memory": 1 if memory_pg else 0,
            "migration/ops": 2,
            "checkpoint-pointers": (
                (max(0, int(api_replicas)) + max(0, int(worker_replicas))) * pointer_peak
                if checkpoint_pg else 0
            ),
        },
        reserve=reserve,
    )
