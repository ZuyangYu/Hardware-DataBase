"""Optional P1 field-level requirements and evidence verification.

Rules here describe *what must be present* (object, attribute, conditions,
source locator). They deliberately contain no expected numeric answers from the
acceptance cases. Verification returns supported/contradicted/unknown with the
actual evidence locator and excerpt so an unsupported field can never be marked
covered by a model suggestion.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence


RULE_VERSION = "field-rules-v1"

SUPPORTED = "supported"
CONTRADICTED = "contradicted"
UNKNOWN = "unknown"

_RANGE_RE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*(?:V|v|伏)?\s*(?:-|~|～|—|–|至|到)\s*(?P<high>\d+(?:\.\d+)?)\s*(?:V|v|伏)"
)
_CURRENT_RE = re.compile(r"(?P<threshold>\d+(?:\.\d+)?)\s*(?P<unit>uA|µA|μA|mA|A)", re.IGNORECASE)
_RESISTANCE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>kΩ|kohm|k|Ω|ohm|R)\b", re.IGNORECASE)
_CAPACITANCE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>nF|uF|µF|μF|pF|F)\b", re.IGNORECASE)
_RAIL_RE = re.compile(r"\bVCC[A-Z0-9_]+\b", re.IGNORECASE)
_PIN_RE = re.compile(r"(?:第\s*(\d{1,3})\s*(?:脚|引脚)|(?:pin|管脚)\s*[:：#]?\s*(\d{1,3}))", re.IGNORECASE)


@dataclass(frozen=True)
class FieldRequirement:
    requirement_id: str
    field_type: str
    rule_version: str
    target_id: str
    kb_id: int
    kb_name: str
    record_id: int
    question: str
    object_scope: str = "unspecified"
    attributes: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    pin_values: tuple[str, ...] = ()
    expected_count: int | None = None


@dataclass
class FieldVerification:
    requirement_id: str
    status: str = UNKNOWN
    evidence_ref: str = ""
    locator: dict = field(default_factory=dict)
    excerpt: str = ""
    facts: dict = field(default_factory=dict)
    reason: str = ""


@dataclass
class FieldCoveragePlan:
    requirements: tuple[FieldRequirement, ...]
    verifications: dict[str, FieldVerification] = field(default_factory=dict)
    revision: int = 0

    def __post_init__(self) -> None:
        for requirement in self.requirements:
            self.verifications.setdefault(requirement.requirement_id, FieldVerification(requirement.requirement_id))

    coverage_mode = "explicit_fields"

    def evaluate(self, evidence) -> list[FieldVerification]:
        metadata = dict(getattr(evidence, "metadata", None) or {})
        locator = dict(getattr(evidence, "locator", None) or {})
        content = str(getattr(evidence, "content", "") or "")
        evidence_ref = str(getattr(evidence, "id", "") or "")
        results = []
        for requirement in self.requirements:
            if int(metadata.get("kb_id") or 0) not in (0, requirement.kb_id):
                continue
            if int(metadata.get("local_record_id") or 0) not in (0, requirement.record_id):
                continue
            if requirement.pin_values and not _pin_matches(requirement, locator, content):
                continue
            verification = verify_requirement(requirement, evidence)
            if verification.status == UNKNOWN and not verification.reason:
                continue
            results.append(verification)
        return results

    def apply(self, results: Iterable[FieldVerification]) -> None:
        for result in results:
            current = self.verifications.get(result.requirement_id)
            if current is None:
                continue
            priority = {SUPPORTED: 3, CONTRADICTED: 2, UNKNOWN: 1}
            if priority.get(result.status, 0) >= priority.get(current.status, 0):
                self.verifications[result.requirement_id] = result
        self.revision += 1

    def coverage_snapshot(self) -> dict:
        counts = {SUPPORTED: 0, CONTRADICTED: 0, UNKNOWN: 0}
        for verification in self.verifications.values():
            counts[verification.status] = counts.get(verification.status, 0) + 1
        return {
            "coverage_mode": self.coverage_mode,
            "rule_version": RULE_VERSION,
            "required": len(self.requirements),
            "states": counts,
            "complete": bool(self.requirements) and counts[SUPPORTED] == len(self.requirements),
            "requirements": [
                {
                    "requirement_id": verification.requirement_id,
                    "field_type": requirement.field_type,
                    "target_id": requirement.target_id,
                    "object_scope": requirement.object_scope,
                    "attributes": list(requirement.attributes),
                    "conditions": list(requirement.conditions),
                    "status": verification.status,
                    "evidence_ref": verification.evidence_ref,
                    "locator": verification.locator,
                    "excerpt": verification.excerpt[:500],
                    "facts": verification.facts,
                    "reason": verification.reason,
                }
                for requirement, verification in (
                    (self._requirement(item.requirement_id), item)
                    for item in self.verifications.values()
                )
            ],
        }

    def _requirement(self, requirement_id: str) -> FieldRequirement:
        return next(item for item in self.requirements if item.requirement_id == requirement_id)


def extract_requirements(question: str, targets: Sequence) -> tuple[FieldRequirement, ...]:
    """Extract supported field obligations without guessing values."""
    requirements: list[FieldRequirement] = []
    for target in targets:
        target_question = str(getattr(target, "question", "") or question or "")
        text = _norm(target_question)
        base = dict(
            target_id=target.target_id, kb_id=target.kb_id, kb_name=target.kb_name,
            record_id=target.record_id, question=target_question,
        )
        index = 0
        if re.search(r"静态电流|休眠电流|quiescent\s+current", text):
            index += 1
            requirements.append(FieldRequirement(
                requirement_id=f"{target.target_id}:current:{index}", field_type="quiescent_current",
                rule_version=RULE_VERSION, object_scope="quiescent",
                conditions=_conditions_near(target_question, r"静态电流|休眠电流|quiescent\s+current", 80),
                **base,
            ))
        if re.search(r"供电|工作电压|电压范围|kl30|蓄电池", text):
            object_scope = _voltage_scope(text)
            index += 1
            requirements.append(FieldRequirement(
                requirement_id=f"{target.target_id}:voltage:{index}", field_type="voltage_range",
                rule_version=RULE_VERSION, object_scope=object_scope,
                conditions=_conditions_near(target_question, r"供电|工作电压|电压范围|kl30|蓄电池", 28),
                **base,
            ))
        if re.search(r"终端电阻|termination\s+resistor", text):
            index += 1
            requirements.append(FieldRequirement(
                requirement_id=f"{target.target_id}:resistance:{index}", field_type="termination_resistance",
                rule_version=RULE_VERSION, object_scope="terminal_resistor", attributes=("resistance",), **base,
            ))
        if re.search(r"电容|capacitor", text):
            index += 1
            requirements.append(FieldRequirement(
                requirement_id=f"{target.target_id}:capacitance:{index}", field_type="capacitance",
                rule_version=RULE_VERSION, object_scope="terminal_capacitor", attributes=("capacitance",), **base,
            ))
        if re.search(r"lp87702|电源网络|输入网络|输出电源|vcc3v3|vcc1v", text):
            output_match = re.search(r"([一二三四五六七八九十\d]+)\s*(?:个|路)?\s*输出", text)
            output_count = _chinese_number(output_match.group(1)) if output_match else None
            index += 1
            requirements.append(FieldRequirement(
                requirement_id=f"{target.target_id}:power_network:{index}", field_type="power_network",
                rule_version=RULE_VERSION, object_scope="lp87702",
                attributes=("input_rail", "output_rails") if "输出" in text else ("input_rail",),
                expected_count=output_count, **base,
            ))
        pins = [first or second for first, second in _PIN_RE.findall(target_question)]
        if pins or re.search(r"管脚定义|引脚定义|pin\s+definition|功能描述", text):
            index += 1
            attributes = []
            if re.search(r"定义|definition", text):
                attributes.append("definition")
            if re.search(r"功能|function", text):
                attributes.append("function")
            if re.search(r"电压|voltage|供电", text):
                attributes.append("voltage")
            requirements.append(FieldRequirement(
                requirement_id=f"{target.target_id}:pin:{index}", field_type="pin_definition",
                rule_version=RULE_VERSION, object_scope="pin",
                attributes=tuple(attributes or ("definition",)), pin_values=tuple(dict.fromkeys(pins)), **base,
            ))
    return tuple(requirements)


def verify_requirement(requirement: FieldRequirement, evidence) -> FieldVerification:
    content = str(getattr(evidence, "content", "") or "")
    locator = dict(getattr(evidence, "locator", None) or {})
    evidence_ref = str(getattr(evidence, "id", "") or "")
    text = _norm(content)
    base = dict(requirement_id=requirement.requirement_id, evidence_ref=evidence_ref,
                locator=locator, excerpt=content)
    verifier = {
        "voltage_range": _verify_voltage,
        "quiescent_current": _verify_current,
        "termination_resistance": _verify_resistance,
        "capacitance": _verify_capacitance,
        "power_network": _verify_power_network,
        "pin_definition": _verify_pin,
    }.get(requirement.field_type)
    if verifier is None:
        return FieldVerification(**base, status=UNKNOWN, reason="unsupported_field_type")
    return verifier(requirement, text, content, base)


def _verify_voltage(requirement, text, content, base):
    ranges = [{"low": float(match.group("low")), "high": float(match.group("high")),
               "unit": "V"} for match in _RANGE_RE.finditer(content)]
    if not ranges:
        return FieldVerification(**base, status=UNKNOWN, reason="no_voltage_range")
    scope = requirement.object_scope
    can_only = bool(re.search(r"can\s*通信|cAN通信|仅\s*can", text, re.IGNORECASE))
    all_modules = bool(re.search(r"所有模块|全部模块|全功能|整机.*正常", text))
    if scope == "all_modules" and can_only and not all_modules:
        return FieldVerification(**base, status=CONTRADICTED, facts={"ranges": ranges},
                                 reason="scope_is_can_communication_not_all_modules")
    if scope == "all_modules" and not all_modules:
        return FieldVerification(**base, status=UNKNOWN, facts={"ranges": ranges},
                                 reason="missing_all_modules_scope")
    if scope == "can_communication" and not can_only:
        return FieldVerification(**base, status=UNKNOWN, facts={"ranges": ranges},
                                 reason="missing_can_scope")
    missing = [condition for condition in requirement.conditions if not _condition_in_text(condition, text)]
    if missing:
        return FieldVerification(**base, status=UNKNOWN, facts={"ranges": ranges},
                                 reason="missing_conditions:" + ",".join(missing))
    return FieldVerification(**base, status=SUPPORTED, facts={"ranges": ranges})


def _verify_current(requirement, text, content, base):
    match = _CURRENT_RE.search(content)
    if not match:
        return FieldVerification(**base, status=UNKNOWN, reason="no_current_value")
    missing = [condition for condition in requirement.conditions if not _condition_in_text(condition, text)]
    if missing:
        return FieldVerification(**base, status=UNKNOWN,
                                 facts={"value": match.group("threshold"), "unit": match.group("unit")},
                                 reason="missing_conditions:" + ",".join(missing))
    return FieldVerification(**base, status=SUPPORTED,
                             facts={"value": match.group("threshold"), "unit": match.group("unit")})


def _verify_resistance(requirement, text, content, base):
    values = [{"value": m.group("value"), "unit": m.group("unit")} for m in _RESISTANCE_RE.finditer(content)]
    if not values:
        return FieldVerification(**base, status=UNKNOWN, reason="no_resistance_value")
    if not re.search(r"终端电阻|termination", text):
        return FieldVerification(**base, status=UNKNOWN, facts={"values": values},
                                 reason="missing_resistance_context")
    return FieldVerification(**base, status=SUPPORTED, facts={"values": values})


def _verify_capacitance(requirement, text, content, base):
    values = [{"value": m.group("value"), "unit": m.group("unit")} for m in _CAPACITANCE_RE.finditer(content)]
    if not values:
        return FieldVerification(**base, status=UNKNOWN, reason="no_capacitance_value")
    if not re.search(r"电容|capacitor", text):
        return FieldVerification(**base, status=UNKNOWN, facts={"values": values},
                                 reason="missing_capacitance_context")
    return FieldVerification(**base, status=SUPPORTED, facts={"values": values})


def _verify_power_network(requirement, text, content, base):
    if "lp87702" not in text:
        return FieldVerification(**base, status=UNKNOWN, reason="missing_lp87702_context")
    rails = list(dict.fromkeys(match.group(0).upper() for match in _RAIL_RE.finditer(content)))
    if not rails:
        return FieldVerification(**base, status=UNKNOWN, reason="no_power_rail")
    input_rail = None
    for rail in rails:
        if re.search(rf"{re.escape(rail)}[^,;。\n]{{0,12}}(?:输入|input)|(?:输入|input)[^,;。\n]{{0,12}}{re.escape(rail)}",
                     content, re.IGNORECASE):
            input_rail = rail
            break
    output_rails = [rail for rail in rails if rail != input_rail]
    facts = {"rails": rails, "input_rail": input_rail, "output_rails": output_rails}
    if "input_rail" in requirement.attributes and input_rail is None:
        return FieldVerification(**base, status=UNKNOWN, facts=facts, reason="missing_input_rail_context")
    if requirement.expected_count is not None and len(output_rails) < requirement.expected_count:
        return FieldVerification(**base, status=UNKNOWN, facts=facts,
                                 reason=f"missing_output_rails:{len(output_rails)}/{requirement.expected_count}")
    return FieldVerification(**base, status=SUPPORTED, facts=facts)


def _verify_pin(requirement, text, content, base):
    if requirement.pin_values and not any(
        re.search(rf"(?:第\s*{re.escape(pin)}\s*脚|pin\s*(?:no\.?|number)?\s*[:：#]?\s*{re.escape(pin)}(?!\d))",
                  text, re.IGNORECASE)
        for pin in requirement.pin_values
    ):
        return FieldVerification(**base, status=UNKNOWN, reason="pin_number_not_present")
    facts = {}
    if "definition" in requirement.attributes:
        match = re.search(r"(?:管脚定义|pin\s*definition|定义)\s*[:：]?\s*([^;；|]+)", content, re.IGNORECASE)
        if not match:
            return FieldVerification(**base, status=UNKNOWN, reason="missing_pin_definition")
        facts["definition"] = match.group(1).strip()
    if "function" in requirement.attributes:
        match = re.search(r"(?:功能描述|功能|function)\s*[:：]?\s*([^;；|]+)", content, re.IGNORECASE)
        if not match:
            return FieldVerification(**base, status=UNKNOWN, reason="missing_pin_function")
        facts["function"] = match.group(1).strip()
    if "voltage" in requirement.attributes:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|~|～|—|–|至)\s*(\d+(?:\.\d+)?)\s*V", content, re.IGNORECASE)
        if not match and not re.search(r"\d+(?:\.\d+)?\s*V", content, re.IGNORECASE):
            return FieldVerification(**base, status=UNKNOWN, reason="missing_pin_voltage")
        facts["voltage"] = match.group(0) if match else re.search(r"\d+(?:\.\d+)?\s*V", content, re.IGNORECASE).group(0)
    return FieldVerification(**base, status=SUPPORTED, facts=facts)


def _pin_matches(requirement: FieldRequirement, locator: dict, content: str) -> bool:
    if not requirement.pin_values:
        return True
    locator_pin = str(locator.get("pin_value") or "")
    if locator_pin and locator_pin in requirement.pin_values:
        return True
    return any(
        re.search(rf"(?:第\s*{re.escape(pin)}\s*脚|pin\s*(?:no\.?|number)?\s*[:：#]?\s*{re.escape(pin)}(?!\d))",
                  content, re.IGNORECASE)
        for pin in requirement.pin_values
    )


def _voltage_scope(text: str) -> str:
    if re.search(r"can\s*通信|cAN通信|仅\s*can", text, re.IGNORECASE) and not re.search(r"所有模块|全部模块", text):
        return "can_communication"
    if re.search(r"所有模块|全部模块|整机|全功能", text):
        return "all_modules"
    if re.search(r"蓄电池|控制器.*供电", text):
        return "controller_supply"
    if re.search(r"adas.*正常|正常工作电压", text):
        return "normal_operation"
    return "unspecified"


def _conditions(text: str) -> tuple[str, ...]:
    conditions = []
    temp = re.search(r"(\d+(?:\.\d+)?)\s*(?:deg|℃|°C|度)", text, re.IGNORECASE)
    if temp:
        conditions.append(f"temp:{temp.group(1)}")
    test_voltage = re.search(r"(?:暂定|测试电压|测试条件)[^。；;\n]{0,20}?(\d+(?:\.\d+)?)\s*V", text, re.IGNORECASE)
    if test_voltage:
        conditions.append(f"test_voltage:{test_voltage.group(1)}")
    return tuple(dict.fromkeys(conditions))


def _conditions_near(text: str, anchor_pattern: str, window: int) -> tuple[str, ...]:
    anchor = re.search(anchor_pattern, text, re.IGNORECASE)
    if not anchor:
        return ()
    start = max(0, anchor.start() - window)
    end = min(len(text), anchor.end() + window)
    return _conditions(text[start:end])


def _condition_in_text(condition: str, text: str) -> bool:
    kind, _, value = condition.partition(":")
    if kind == "temp":
        return bool(re.search(rf"{re.escape(value)}\s*(?:deg|℃|°C|度)", text, re.IGNORECASE))
    if kind == "test_voltage":
        return bool(re.search(rf"{re.escape(value)}\s*V", text, re.IGNORECASE))
    return value.casefold() in text


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).casefold()


def _chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    mapping = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return mapping.get(value)
