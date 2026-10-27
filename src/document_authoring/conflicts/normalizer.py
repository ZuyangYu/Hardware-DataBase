"""Normalize engineering claim values before comparing source declarations."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import NormalizedClaim


_NUMERIC_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-zΩμµ%]+)?\s*$")
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_RANGE_RE = re.compile(rf"^\s*({_NUMBER})\s*(?:~|～|–|—|至|to|-)\s*({_NUMBER})\s*([A-Za-zΩμµ%°]+)?\s*$", re.I)
_LIMIT_RE = re.compile(rf"^\s*(<=|>=|<|>|≤|≥)\s*({_NUMBER})\s*([A-Za-zΩμµ%°]+)?\s*$")
_UNIT_ALIASES = {
    "v": ("voltage", Decimal("1")),
    "mv": ("voltage", Decimal("0.001")),
    "kv": ("voltage", Decimal("1000")),
    "a": ("current", Decimal("1")),
    "ma": ("current", Decimal("0.001")),
    "ua": ("current", Decimal("0.000001")),
    "μa": ("current", Decimal("0.000001")),
    "µa": ("current", Decimal("0.000001")),
    "hz": ("frequency", Decimal("1")),
    "khz": ("frequency", Decimal("1000")),
    "mhz": ("frequency", Decimal("1000000")),
    "ohm": ("resistance", Decimal("1")),
    "ω": ("resistance", Decimal("1")),
    "kohm": ("resistance", Decimal("1000")),
    "%": ("ratio", Decimal("1")),
    "°c": ("temperature", Decimal("1")),
    "degc": ("temperature", Decimal("1")),
    "deg": ("temperature", Decimal("1")),
}


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _key(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def _numeric(value: Any, unit: str) -> tuple[str, str] | None:
    text = _text(value)
    ranged = _RANGE_RE.match(text)
    limited = _LIMIT_RE.match(text)
    if ranged or limited:
        match = ranged or limited
        alias = _UNIT_ALIASES.get(_text(match.group(3) or unit).casefold())
        if alias is None:
            return None
        dimension, multiplier = alias
        if ranged:
            lower, upper = (Decimal(match.group(i)) * multiplier for i in (1, 2))
            if lower > upper:
                return None
            return dimension, f"[{format(lower.normalize(), 'f')},{format(upper.normalize(), 'f')}]"
        operator = match.group(1).replace("≤", "<=").replace("≥", ">=")
        number = Decimal(match.group(2)) * multiplier
        return dimension, operator + format(number.normalize(), "f")
    match = _NUMERIC_RE.match(_text(value))
    if not match:
        return None
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return None
    raw_unit = _text(match.group(2) or unit).casefold()
    if not raw_unit:
        return None
    alias = _UNIT_ALIASES.get(raw_unit)
    if alias is None:
        return None
    dimension, multiplier = alias
    canonical = (number * multiplier).normalize()
    return dimension, format(canonical, "f")


def normalize_claim(
    *,
    claim_id: str,
    field_key: str,
    subject: str,
    predicate: str,
    value: Any,
    unit: str = "",
    condition: str = "",
    scope_key: str = "",
    source_kb_id: int,
    source_role: str,
    authoritative: bool = False,
    approval_status: str = "",
    evidence_id: str | None = None,
    evidence_ids: tuple[str, ...] | list[str] = (),
    source_content_hash: str = "",
    source_version_id: str = "",
    locator: dict[str, Any] | None = None,
) -> NormalizedClaim:
    """Create a canonical claim without treating model text as authority."""

    display = _text(value)
    numeric = _numeric(value, unit)
    if numeric is None:
        normalized_value = _key(display)
        normalized_unit = _key(unit)
    else:
        normalized_unit, normalized_value = numeric
    ids = [str(item).strip() for item in evidence_ids if str(item).strip()]
    if evidence_id and str(evidence_id).strip():
        ids.append(str(evidence_id).strip())
    return NormalizedClaim(
        claim_id=_text(claim_id),
        field_key=_key(field_key),
        subject=_key(subject),
        predicate=_key(predicate),
        value=value,
        display_value=display,
        normalized_value=normalized_value or "∅",
        normalized_unit=normalized_unit,
        condition=_key(condition),
        scope_key=_key(scope_key),
        source_kb_id=int(source_kb_id),
        source_role=source_role,
        authoritative=bool(authoritative),
        approval_status=_key(approval_status),
        evidence_ids=tuple(dict.fromkeys(ids)),
        source_content_hash=str(source_content_hash or ""),
        source_version_id=str(source_version_id or ""),
        locator=dict(locator or {}),
    )


__all__ = ["normalize_claim"]
