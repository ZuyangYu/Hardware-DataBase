"""Deterministic connector-pin function resolution.

Connector function cells are particularly prone to cross-row RAG contamination.
This module keeps the scope narrow: X/J reference designators with a real net
are resolved from an exact manual hit first, then from an explicit net-name
rule.  Unknown nets deliberately remain unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from src.circuit.models import ComponentInstance


_CONNECTOR_RE = re.compile(r"^[XJ]\d+[A-Z0-9_]*$", re.IGNORECASE)
_PIN_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<ref>[XJ]\d+[A-Z0-9_]*)\s*(?:[-_./:]|pin\s*)\s*&?(?P<pin>[A-Za-z0-9_.]+)",
    re.IGNORECASE,
)
_FUNCTION_HINT_RE = re.compile(
    r"(?:pin\s*(?:description|function|definition|purpose)|功能(?:描述|定义|说明)|用途|输入|输出|signal|供电|接地)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PinFunctionResolution:
    function: str | None
    source: str
    evidence_ids: list[str]


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.casefold() in {"none", "null", "nan"} else text


def _pin_name(value: Any) -> str:
    return _clean(value).lstrip("&")


def select_connected_connector_pins(
    connectors: Iterable[ComponentInstance],
) -> list[dict[str, str]]:
    """Return only connected pins belonging to X/J connector instances."""

    selected: list[dict[str, str]] = []
    for connector in connectors:
        refdes = _clean(connector.refdes).upper()
        if not _CONNECTOR_RE.fullmatch(refdes):
            continue
        part_number = _clean(connector.part_number or connector.value)
        for pin in connector.pins:
            pin_name = _pin_name(pin.name)
            net_name = _clean(pin.net)
            if not pin_name or not net_name:
                continue
            selected.append({
                "refdes": refdes,
                "pin_name": pin_name,
                "net_name": net_name,
                "part_number": part_number,
            })
    return selected


def infer_pin_function_from_net(net_name: str | None) -> str | None:
    """Map common net naming conventions to a conservative function label."""

    net = _clean(net_name).upper().replace(" ", "")
    if not net:
        return None
    if net in {"NC", "N/C", "NO_CONNECT"}:
        return "未连接（NC，规则推断）"
    if re.search(r"(?:CAN\d*[_-]?[HL])$", net):
        return "CAN 总线差分信号（规则推断）"
    if re.search(r"(?:ETH|B_D_ETH)[_\d]*(?:100|1000)?[_-]?[PN]$", net):
        return "以太网高速差分信号（规则推断）"
    if re.search(r"MIPI.*(?:DATA|CLK).*[_-]?[PN]$", net):
        return "MIPI 摄像头高速差分数据信号（规则推断）"
    if re.search(r"(?:^|[_-])(?:SDA|SCL)(?:[_-].*)?$", net):
        return "I²C 串行总线信号（规则推断）"
    if "ERRORFLAG" in net or "ERR_FLAG" in net:
        return "故障状态指示信号（规则推断）"
    if "WKUP" in net or "WAKE" in net:
        return "唤醒控制信号（规则推断）"
    if re.search(r"(?:^|[_-])(?:GND|PGND|AGND|DGND)(?:[_-].*)?$", net):
        return "地/回流连接（规则推断）"
    if re.search(r"(?:^|[_-])(?:VCC|VDD|VSS|VBAT|VIN|VOUT|3V3|1V8|1V1)(?:[_A-Z0-9-]*)$", net):
        return "电源供电连接（规则推断）"
    if re.fullmatch(r"\d{7}", net):
        return "信号线（规则推断）"
    return None


def _is_manual_evidence(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") or {}
    role = " ".join(str(metadata.get(key) or "") for key in ("source_role", "source_group", "evidence_kind"))
    source = str(item.get("source_name") or "")
    text = f"{role} {source}".casefold()
    return any(term in text for term in ("datasheet", "manual", "handbook", "手册", "规格书", "数据手册"))


def _contains_exact_pin(text: str, refdes: str, pin_name: str) -> bool:
    ref = re.escape(refdes)
    pin = re.escape(pin_name.lstrip("&"))
    patterns = (
        rf"(?<![A-Za-z0-9_]){ref}\s*[-_./:]\s*&?{pin}(?![A-Za-z0-9_])",
        rf"(?<![A-Za-z0-9_]){ref}\s+pin\s*&?{pin}(?![A-Za-z0-9_])",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _manual_sentence(text: str, refdes: str, pin_name: str) -> str | None:
    if not _contains_exact_pin(text, refdes, pin_name):
        return None
    chunks = [part.strip(" \t\r\n-•") for part in re.split(r"(?<=[。！？.!?;；])|\n+", text) if part.strip()]
    candidates = [chunk for chunk in chunks if _contains_exact_pin(chunk, refdes, pin_name)]
    if not candidates:
        return None
    candidates.sort(key=lambda chunk: (0 if _FUNCTION_HINT_RE.search(chunk) else 1, len(chunk)))
    value = candidates[0]
    # Remove a leading locator while preserving the manual's wording.
    value = re.sub(
        rf"^.*?{re.escape(refdes)}\s*(?:[-_./:]\s*&?|pin\s*&?){re.escape(pin_name)}(?:\s*\([^)]*\))?\s*[:：,-]?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    return value or candidates[0]


def resolve_pin_function(
    *,
    refdes: str,
    pin_name: str,
    net_name: str | None,
    evidence: Iterable[dict[str, Any]] = (),
    part_number: str | None = None,
) -> PinFunctionResolution:
    """Resolve one X/J pin, preferring exact same-device manual evidence."""

    ref = _clean(refdes).upper()
    pin = _pin_name(pin_name)
    if not _CONNECTOR_RE.fullmatch(ref) or not pin or not _clean(net_name):
        return PinFunctionResolution(None, "unresolved", [])
    items = list(evidence)
    manual_candidates = [item for item in items if _is_manual_evidence(item)]
    if part_number:
        part = _clean(part_number)
        scoped = [item for item in manual_candidates if part.casefold() in str(item.get("content") or "").casefold() or part.casefold() in str(item.get("source_name") or "").casefold()]
        if scoped:
            manual_candidates = scoped
    for item in manual_candidates:
        text = _clean(item.get("content"))
        sentence = _manual_sentence(text, ref, pin)
        if sentence:
            evidence_id = _clean(item.get("id"))
            return PinFunctionResolution(sentence, "datasheet", [evidence_id] if evidence_id else [])
    inferred = infer_pin_function_from_net(net_name)
    ids = [_clean(item.get("id")) for item in items if _clean(item.get("id"))]
    return PinFunctionResolution(inferred, "rule" if inferred else "unresolved", ids[:1] if inferred else [])
