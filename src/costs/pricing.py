from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PriceVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_version: str = Field(min_length=1, max_length=128)
    endpoint_alias: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=80)
    requested_model: str = Field(min_length=1, max_length=200)
    response_model: str = Field(min_length=1, max_length=200)
    effective_from: str
    effective_to: str | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    input_per_million: Decimal = Field(ge=0)
    cached_input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    algorithm: Literal["inclusive_cache_v1"] = "inclusive_cache_v1"

    @field_validator("input_per_million", "cached_input_per_million", "output_per_million")
    @classmethod
    def finite(cls, value):
        if not value.is_finite():
            raise ValueError("prices must be finite")
        return value

    @field_validator("effective_from", "effective_to")
    @classmethod
    def utc_time(cls, value):
        if value is None:
            return value
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("price effective time requires timezone")
        return dt.astimezone(timezone.utc).isoformat()

    @model_validator(mode="after")
    def interval(self):
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("empty price interval")
        return self


def price(usage: dict, version: PriceVersion | None) -> dict:
    result = {"cost_status": "unknown_usage", "cost_amount": None, "currency": None, "price_version": None, "pricing_algorithm_version": None}
    if usage.get("usage_status") != "reported":
        return result
    reasoning = usage.get("reasoning_tokens")
    if reasoning is not None and (isinstance(reasoning, bool) or not isinstance(reasoning, int) or reasoning < 0 or reasoning > (usage.get("output_tokens") or 0)):
        return result
    if version is None:
        return {**result, "cost_status": "unknown_price"}
    try:
        i, o, cached = (usage.get(k) for k in ("input_tokens", "output_tokens", "cached_input_tokens"))
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (i, o, cached)) or cached > i:
            return result
        if usage.get("cache_write_tokens") not in (None, 0):
            return {**result, "cost_status": "unknown_price"}  # Unsupported billing bucket.
        amount = ((Decimal(i - cached) * version.input_per_million)
                  + Decimal(cached) * version.cached_input_per_million
                  + Decimal(o) * version.output_per_million) / Decimal(1_000_000)
    except (InvalidOperation, TypeError):
        return result
    return {"cost_status": "priced", "cost_amount": str(amount), "currency": version.currency,
            "price_version": version.price_version, "pricing_algorithm_version": version.algorithm}
