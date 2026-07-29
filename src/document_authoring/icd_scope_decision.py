"""Pure, evidence-led scope decisions for ICD pin tables."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable
from uuid import uuid4

from pydantic import BaseModel, Field


class IcdScopeItem(BaseModel):
    """A pin mapping safe to include without a user decision."""

    refdes: str
    pin_name: str
    net_name: str
    source_names: list[str] = Field(default_factory=list)
    reason: str = "direct_circuit_and_supporting_reference"


class IcdScopeException(BaseModel):
    """A scope question that needs the user's batch resolution."""

    exception_id: str = Field(default_factory=lambda: uuid4().hex)
    kind: str
    refdes: str | None = None
    pin_name: str | None = None
    net_name: str | None = None
    source_names: list[str] = Field(default_factory=list)
    recommended_action: str
    user_instruction: str


class IcdScopeDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: uuid4().hex)
    auto_items: list[IcdScopeItem] = Field(default_factory=list)
    exceptions: list[IcdScopeException] = Field(default_factory=list)
    frozen_pin_mappings: list[dict[str, str | None]] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        circuit_evidences: Iterable[Any],
        supporting_evidences: Iterable[Any],
    ) -> "IcdScopeDecision":
        return build_icd_scope_decision(circuit_evidences, supporting_evidences)


def build_icd_scope_decision(
    circuit_evidences: Iterable[Any],
    supporting_evidences: Iterable[Any],
) -> IcdScopeDecision:
    """Freeze every EDF pin and auto-adopt only directly supported mappings."""

    mappings = _pin_mappings(circuit_evidences)
    supporting = list(supporting_evidences)
    auto_items: list[IcdScopeItem] = []
    exceptions: list[IcdScopeException] = []

    for mapping in mappings:
        direct_sources = _direct_sources(
            supporting, mapping["refdes"], mapping["pin_name"]
        )
        if direct_sources:
            auto_items.append(IcdScopeItem(
                **mapping,
                source_names=direct_sources,
            ))
            continue
        exceptions.append(IcdScopeException(
            exception_id=_stable_scope_id("exception", {
                "kind": "extra_pin_exposure",
                **mapping,
            }),
            kind="extra_pin_exposure",
            **mapping,
            recommended_action="mark_pending",
            user_instruction="确认该脚是否需要在对外 ICD 中暴露。",
        ))

    if _has_unsupported_reservation(supporting, mappings):
        exceptions.append(IcdScopeException(
            exception_id=_stable_scope_id("exception", {
                "kind": "unsupported_reservation",
                "source_names": _reservation_sources(supporting),
            }),
            kind="unsupported_reservation",
            recommended_action="mark_pending",
            user_instruction="请提供该预留或裁剪结论对应的直接管脚来源。",
            source_names=_reservation_sources(supporting),
        ))

    decision_payload = {
        "auto_items": [item.model_dump(mode="json") for item in auto_items],
        "exceptions": [exception.model_dump(mode="json") for exception in exceptions],
        "frozen_pin_mappings": mappings,
    }
    return IcdScopeDecision(
        decision_id=_stable_scope_id("decision", decision_payload),
        auto_items=auto_items,
        exceptions=exceptions,
        frozen_pin_mappings=mappings,
    )


def _pin_mappings(circuit_evidences: Iterable[Any]) -> list[dict[str, str]]:
    mappings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for evidence in circuit_evidences:
        metadata = _value(evidence, "metadata") or {}
        locator = _value(evidence, "locator") or {}
        evidence_refdes = str(locator.get("entity_id") or "").strip()
        for raw_mapping in metadata.get("pin_mappings") or []:
            refdes = str(raw_mapping.get("refdes") or evidence_refdes).strip()
            pin_name = str(raw_mapping.get("pin_name") or raw_mapping.get("raw_pin_name") or "").strip()
            if not (refdes and pin_name):
                continue
            key = (refdes.casefold(), pin_name.casefold())
            if key in seen:
                continue
            seen.add(key)
            net_name = str(raw_mapping.get("net_name") or "").strip() or "NC"
            mappings.append({
                "refdes": refdes,
                "pin_name": pin_name,
                "net_name": net_name,
            })
    return mappings


def _direct_sources(
    evidences: Iterable[Any], refdes: str, pin_name: str
) -> list[str]:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(refdes)}\s*-\s*{re.escape(pin_name)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    return [
        str(_value(evidence, "source_name") or "")
        for evidence in evidences
        if pattern.search(str(_value(evidence, "content") or ""))
    ]


def _has_unsupported_reservation(
    evidences: Iterable[Any], mappings: Iterable[dict[str, str]]
) -> bool:
    reservation_evidences = [
        evidence
        for evidence in evidences
        if re.search(r"预留|裁剪", str(_value(evidence, "content") or ""))
    ]
    if not reservation_evidences:
        return False
    return not any(
        _direct_sources(reservation_evidences, mapping["refdes"], mapping["pin_name"])
        for mapping in mappings
    )


def _reservation_sources(evidences: Iterable[Any]) -> list[str]:
    return [
        str(_value(evidence, "source_name") or "")
        for evidence in evidences
        if re.search(r"预留|裁剪", str(_value(evidence, "content") or ""))
    ]


def effective_frozen_pin_mappings(review: Any | None) -> list[dict[str, str | None]]:
    """Return frozen mappings after applying persisted user exclusions.

    The review itself remains immutable evidence of the user's choices; this
    projection is the only mapping list that generation consumers should use.
    """
    decision = _value(review, "decision")
    mappings = _value(decision, "frozen_pin_mappings") or []
    exceptions = {
        str(_value(exception, "exception_id") or ""): exception
        for exception in (_value(decision, "exceptions") or [])
    }
    excluded_keys = {
        _scope_mapping_key(exception)
        for resolution in (_value(review, "resolutions") or [])
        if str(_value(resolution, "action") or "").strip().casefold() == "exclude"
        if (exception := exceptions.get(str(_value(resolution, "exception_id") or "")))
        and _scope_mapping_key(exception) is not None
    }
    return [
        dict(mapping)
        for mapping in mappings
        if isinstance(mapping, dict)
        and _scope_mapping_key(mapping) not in excluded_keys
    ]


def _scope_mapping_key(item: Any) -> tuple[str, str] | None:
    refdes = str(_value(item, "refdes") or "").strip()
    pin_name = str(_value(item, "pin_name") or "").strip()
    if not (refdes and pin_name):
        return None
    return refdes.casefold(), pin_name.casefold()


def _stable_scope_id(kind: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"icd-scope-{kind}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _value(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)
