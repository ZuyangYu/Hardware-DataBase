"""Bounded, deterministic extraction of a controlled hardware field catalogue.

Text is data, never authority. Only explicit declarations are extracted; every
claim carries an exact quote span and server-owned provenance. Unrecognised
properties remain unverified rather than being guessed by a language model.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from .models import NormalizedClaim
from .normalizer import normalize_claim

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_VALUE = rf"(?:[<>≤≥]=?\s*)?{_NUMBER}(?:\s*(?:~|～|–|—|至|to|-)\s*{_NUMBER})?\s*(?:mV|kV|V|mA|uA|μA|µA|A|MHz|kHz|Hz|℃|°C|degC?|%)"
# Keep locally named designators/rails; never infer an object from nearby text
# or carry one forward from another declaration.
_OBJECT = r"(?:[A-Z]{1,4}\d+[A-Z0-9_]*|(?:VCC|VDD|VSS|VIN|VOUT|VBAT|GND)(?:_[A-Z0-9]+)*)"
# The phrase must name a controlled property, not merely mention a number.
_DECLARATION = re.compile(
    rf"(?:(?<![A-Z0-9_])(?P<object>{_OBJECT})\s*(?:的\s*)?)?"
    rf"(?P<label>(?:(?:控制器|ECU|CAN|输入|输出|input|output)\s*)?"
    rf"(?:工作电压(?:范围)?|电压范围|输入电压|输出电压|额定电压|标称电压|电压|静态电流|休眠电流|最大电流|额定电流|工作温度(?:范围)?|时钟频率|频率|"
    rf"(?:operating|input|output|nominal)\s+voltage(?:\s+range)?|quiescent\s+current|operating\s+temperature(?:\s+range)?))"
    rf"\s*[:：=为是]*\s*(?P<limit>小于|不大于|大于|不小于)?\s*(?P<value>{_VALUE})", re.I,
)
_SENTENCE = re.compile(r"[^。；;\n]+[。；;\n]?")


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


class GroundedClaimExtractor:
    version = "hardware-fields-v2"

    def __init__(self, *, max_claims_per_evidence: int = 32, max_characters: int = 64_000):
        self.max_claims = max(1, min(int(max_claims_per_evidence), 128))
        self.max_characters = max(1, int(max_characters))

    def extract(self, evidences: list[Any]) -> list[NormalizedClaim]:
        claims: dict[str, NormalizedClaim] = {}
        for evidence in evidences:
            metadata = dict(_get(evidence, "metadata", {}) or {})
            source = metadata.get("knowledge_source") or {}
            identity = str(_get(evidence, "id", "") or "")
            raw_kb_id = source.get("kb_id") or metadata.get("kb_id")
            # Evidence metadata crosses JSON/API boundaries where integer
            # identities are commonly serialized as strings.  Normalize that
            # representation at the extractor boundary while rejecting
            # ambiguous values such as booleans or decimal strings.
            if isinstance(raw_kb_id, bool):
                kb_id = None
            elif isinstance(raw_kb_id, int):
                kb_id = raw_kb_id
            elif isinstance(raw_kb_id, str) and re.fullmatch(r"[1-9]\d*", raw_kb_id.strip()):
                kb_id = int(raw_kb_id.strip())
            else:
                kb_id = None
            role = source.get("role") or metadata.get("source_role")
            if not identity or not isinstance(kb_id, int) or kb_id <= 0 or role not in {"primary", "peer", "reference", "standard"}:
                continue
            text = str(_get(evidence, "content", "") or "")[:self.max_characters]
            count = 0
            for sentence in _SENTENCE.finditer(text):
                quote = sentence.group()
                for match in _DECLARATION.finditer(quote):
                    label = match["label"].casefold()
                    value = match["value"]
                    if match["limit"]:
                        value = {"小于": "<", "不大于": "<=", "大于": ">", "不小于": ">="}[match["limit"]] + value
                    if "电压" in label or "voltage" in label:
                        field = "nominal_voltage" if any(word in label for word in ("额定", "标称", "nominal")) else "operating_voltage"
                        subject = "power.output" if any(word in label for word in ("输出", "output")) else "power.input"
                    elif "电流" in label or "current" in label:
                        field = "quiescent_current" if any(word in label for word in ("静态", "休眠", "quiescent")) else "rated_current"
                        subject = "controller"
                    elif "温度" in label or "temperature" in label:
                        field, subject = "operating_temperature", "controller"
                    else:
                        field, subject = "clock_frequency", "controller"
                    explicit_object = match["object"] or ""
                    if explicit_object:
                        subject = f"{explicit_object}.{subject}"
                    # Separate normal operation, communication and derating.
                    conditions = []
                    if "CAN" in quote.upper():
                        conditions.append("can_communication")
                    elif "线性" in quote or "降额" in quote:
                        conditions.append("derating")
                    if explicit_object.upper() == "KL15" or "IGN" in quote.upper():
                        conditions.append("ignition")
                    for token in re.findall(r"@\s*[-+]?\d+(?:\.\d+)?\s*(?:℃|°C|degC?|V)", quote, re.I):
                        conditions.append(token.casefold().replace(" ", ""))
                    # A project/version must be explicitly shared by trusted
                    # metadata before facts from different KBs can disagree.
                    scope = str(metadata.get("effective_scope") or metadata.get("project_id") or f"kb:{kb_id}")
                    revision = str(metadata.get("revision") or "")
                    if revision:
                        scope += f":revision:{revision}"
                    start, end = sentence.start(), sentence.end()
                    key = f"{identity}|{start}|{match.start()}|{field}|{value}|{scope}"
                    claim_id = "grounded-" + sha256(key.encode()).hexdigest()[:32]
                    locator = dict(_get(evidence, "locator", {}) or metadata.get("locator") or {})
                    locator["quote_span"] = {"start": start, "end": end, "text": quote}
                    locator["extractor_version"] = self.version
                    claims[claim_id] = normalize_claim(
                        claim_id=claim_id, field_key=field, subject=subject, predicate=field,
                        value=value, condition=";".join(conditions), scope_key=scope,
                        source_kb_id=kb_id, source_role=role,
                        approval_status=str(metadata.get("approval_status") or ""),
                        evidence_id=identity,
                        source_content_hash=str(metadata.get("source_content_hash") or ""),
                        source_version_id=str(metadata.get("source_version_id") or ""), locator=locator,
                    )
                    count += 1
                    if count >= self.max_claims:
                        break
                if count >= self.max_claims:
                    break
        return list(claims.values())
