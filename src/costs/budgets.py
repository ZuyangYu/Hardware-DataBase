"""Immutable, currency-specific observation budgets; amounts are lower bounds."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BudgetVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget_version: str = Field(min_length=1, max_length=128)
    department_id: int | None = Field(default=None, ge=1)
    purpose: str | None = Field(default=None, min_length=1, max_length=80)
    model_alias: str | None = Field(default=None, min_length=1, max_length=80)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount_limit: Decimal = Field(gt=0)
    effective_from: str
    effective_to: str

    @field_validator("amount_limit")
    @classmethod
    def finite_amount(cls, value):
        if not value.is_finite():
            raise ValueError("budget amount must be finite")
        return value

    @field_validator("effective_from", "effective_to")
    @classmethod
    def utc_time(cls, value):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("budget effective time requires timezone")
        return stamp.astimezone(timezone.utc).isoformat()

    @model_validator(mode="after")
    def interval(self):
        if self.effective_to <= self.effective_from:
            raise ValueError("budget window must be nonempty")
        return self


_STATUSES = []
_GAUGES = {}


def publish_statuses(statuses):
    """Bounded aggregate gauges, no per-policy/user labels or attempt counts."""
    global _STATUSES
    _STATUSES = statuses
    from src.observability.metrics import _enabled, _meter
    from opentelemetry.metrics import Observation

    if not _enabled():
        return
    for name, status in (("hdb.cost.budget.exceeded", "exceeded_known_amount"), ("hdb.cost.budget.unknown", "insufficient_cost_coverage")):
        if name not in _GAUGES:
            def values(_options, status=status):
                return [Observation(sum(item["summary"]["unknown_cost_calls"] > 0 if status == "insufficient_cost_coverage" else item["status"] == status for item in _STATUSES))]
            _GAUGES[name] = _meter().create_observable_gauge(name, callbacks=[values])
