"""Pure, evidence-led scope decisions for ICD pin tables."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable
from uuid import uuid4

from pydantic import BaseModel, Field


_PIN_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z]{1,12}\d+[A-Za-z0-9_.-]*)\s*-\s*([A-Za-z0-9_.]+)(?![A-Za-z0-9_])"
)


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
                source_names=_unique([mapping.get("source_name", ""), *direct_sources]),
            ))
            continue
        exceptions.append(IcdScopeException(
            exception_id=_stable_scope_id("exception", {
                "kind": "extra_pin_exposure",
                **mapping,
            }),
            kind="extra_pin_exposure",
            **mapping,
            source_names=_unique([mapping.get("source_name", "")]),
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


def supported_connector_refdes(evidences: Iterable[Any]) -> list[str]:
    """Return refdes values directly cited by classified FPT/requirements sources."""
    return _unique(
        match.group(1).upper()
        for evidence in evidences
        if _is_authoritative_support(evidence)
        for match in _PIN_REFERENCE.finditer(str(_value(evidence, "content") or ""))
    )


def build_unknown_connector_scope_decision() -> IcdScopeDecision:
    """Make one actionable stop instead of expanding an unknown scope to an EDF."""
    exception = IcdScopeException(
        exception_id=_stable_scope_id("exception", {"kind": "connector_scope_unknown"}),
        kind="connector_scope_unknown",
        recommended_action="add_connector_constraint",
        user_instruction=(
            "无法确定接插件范围。请在模板字段的检索条件、别名或值约束中补充"
            "接插件位号（例如连接器/位号：X100），然后重新生成。"
        ),
    )
    payload = {
        "auto_items": [],
        "exceptions": [exception.model_dump(mode="json")],
        "frozen_pin_mappings": [],
    }
    return IcdScopeDecision(
        decision_id=_stable_scope_id("decision", payload),
        exceptions=[exception],
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
                "source_name": str(_value(evidence, "source_name") or "").strip(),
            })
    return mappings


def _direct_sources(
    evidences: Iterable[Any], refdes: str, pin_name: str
) -> list[str]:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(refdes)}\s*-\s*{re.escape(pin_name)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    return _unique(
        str(_value(evidence, "source_name") or "")
        for evidence in evidences
        if _is_authoritative_support(evidence)
        and pattern.search(str(_value(evidence, "content") or ""))
    )


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
    return _unique(
        str(_value(evidence, "source_name") or "")
        for evidence in evidences
        if _is_authoritative_support(evidence)
        if re.search(r"预留|裁剪", str(_value(evidence, "content") or ""))
    )


def _is_authoritative_support(evidence: Any) -> bool:
    """Accept only explicitly classified FPT/requirements evidence.

    A direct pin string in a schematic, EDF, or prior ICD is corroboration at
    most; it cannot decide the external-interface scope.  The KB retrieval
    contract preserves source classification in metadata, so absent/unknown
    classification deliberately falls back to the concise exception queue.
    """
    metadata = _value(evidence, "metadata") or {}
    role = " ".join(
        str(value or "")
        for value in (
            _value(evidence, "document_role"),
            metadata.get("document_role"),
            _value(evidence, "source_type"),
            metadata.get("source_type"),
        )
    ).casefold()
    filename = " ".join(
        str(value or "")
        for value in (
            _value(evidence, "source_name"),
            metadata.get("original_file_name"),
            metadata.get("file_name"),
        )
    ).casefold()
    classification = " ".join((role, filename))
    if not classification:
        return False
    rejected = ("schematic", "原理图", "netlist", "edf", "edif", "icd", "design")
    if any(term in classification for term in rejected):
        return False
    accepted = (
        "fpt", "requirement", "requirements", "specification", "spec",
        "需求", "规范", "规格", "hsi",
    )
    return any(term in classification for term in accepted)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


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
