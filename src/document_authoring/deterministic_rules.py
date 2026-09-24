"""Versioned deterministic-rule registry and side-effect-free executor."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from src.document_authoring.models import DeterministicReviewResult, DeterministicRuleSpec


class DeterministicRuleRegistry:
    """Only registered operation names can be invoked by a work order."""

    _OPERATIONS = {
        "exact_match", "set_compare", "range_check", "regex_check", "existence_check",
        "count_compare", "derived_calculation",
    }

    def __init__(self, specs: Iterable[DeterministicRuleSpec] = ()):
        self._specs: dict[str, DeterministicRuleSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: DeterministicRuleSpec) -> None:
        if spec.approved_operation_name not in self._OPERATIONS:
            raise ValueError(f"unapproved deterministic operation: {spec.approved_operation_name}")
        key = self._key(spec.rule_id, spec.rule_version)
        if key in self._specs:
            raise ValueError(f"deterministic rule already registered: {key}")
        self._specs[key] = spec

    def get(self, rule_id: str, rule_version: str | None = None) -> DeterministicRuleSpec:
        if rule_version is not None:
            key = self._key(rule_id, rule_version)
            try:
                return self._specs[key]
            except KeyError as exc:
                raise KeyError(f"deterministic rule not found: {key}") from exc
        candidates = [spec for spec in self._specs.values() if spec.rule_id == rule_id]
        if len(candidates) != 1:
            raise KeyError(f"deterministic rule version must be explicit: {rule_id}")
        return candidates[0]

    @staticmethod
    def _key(rule_id: str, rule_version: str) -> str:
        return f"{rule_id}@{rule_version}"


class DeterministicRuleExecutor:
    """Executes a small allowlist; it never evaluates user code or expressions."""

    def execute(
        self,
        review_item_id: str,
        spec: DeterministicRuleSpec,
        inputs: dict[str, Any],
        evidence_ids: list[str] | None = None,
    ) -> DeterministicReviewResult:
        evidence_ids = evidence_ids or []
        missing = [name for name in spec.input_requirements if inputs.get(name) is None]
        if missing:
            return self._missing(review_item_id, spec, evidence_ids, missing)
        operation = getattr(self, f"_{spec.approved_operation_name}")
        try:
            passed, display = operation(spec, inputs)
        except ValueError as exc:
            return DeterministicReviewResult(
                review_item_id=review_item_id, rule_id=spec.rule_id, status="conflicting",
                evidence_ids=evidence_ids, diagnostics=[str(exc)],
            )
        return DeterministicReviewResult(
            review_item_id=review_item_id,
            rule_id=spec.rule_id,
            status="passed" if passed else "failed",
            display_value=display,
            evidence_ids=evidence_ids,
        )

    @staticmethod
    def _missing(review_item_id: str, spec: DeterministicRuleSpec, evidence_ids: list[str], missing: list[str]) -> DeterministicReviewResult:
        status = {
            "tbd": "requires_human",
            "insufficient_evidence": "insufficient_evidence",
            "block": "retrieval_failed",
        }[spec.missing_behavior]
        return DeterministicReviewResult(
            review_item_id=review_item_id, rule_id=spec.rule_id, status=status,
            evidence_ids=evidence_ids, diagnostics=[f"missing required inputs: {', '.join(missing)}"],
        )

    @staticmethod
    def _norm(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value).strip()).casefold()

    def _exact_match(self, spec: DeterministicRuleSpec, inputs: dict[str, Any]) -> tuple[bool, str]:
        left_name = spec.parameter_bindings.get("actual", spec.input_requirements[0])
        right_name = spec.parameter_bindings.get("expected", spec.input_requirements[1] if len(spec.input_requirements) > 1 else "expected")
        left, right = inputs[left_name], inputs[right_name]
        return self._norm(left) == self._norm(right), f"{left} == {right}"

    def _set_compare(self, spec: DeterministicRuleSpec, inputs: dict[str, Any]) -> tuple[bool, str]:
        actual_name = spec.parameter_bindings.get("actual", spec.input_requirements[0])
        expected_name = spec.parameter_bindings.get("expected", spec.input_requirements[1] if len(spec.input_requirements) > 1 else "expected")
        actual = {self._norm(item) for item in _as_iterable(inputs[actual_name])}
        expected = {self._norm(item) for item in _as_iterable(inputs[expected_name])}
        mode = spec.parameter_bindings.get("mode", "equal")
        passed = expected <= actual if mode == "contains" else actual == expected
        return passed, f"actual={sorted(actual)}, expected={sorted(expected)}"

    def _range_check(self, spec: DeterministicRuleSpec, inputs: dict[str, Any]) -> tuple[bool, str]:
        value_name = spec.parameter_bindings.get("value", spec.input_requirements[0])
        value = float(inputs[value_name])
        minimum = float(spec.parameter_bindings["minimum"])
        maximum = float(spec.parameter_bindings["maximum"])
        tolerance = float((spec.tolerance or {}).get("absolute", 0))
        return minimum - tolerance <= value <= maximum + tolerance, f"{value} in [{minimum}, {maximum}]"

    def _regex_check(self, spec: DeterministicRuleSpec, inputs: dict[str, Any]) -> tuple[bool, str]:
        value_name = spec.parameter_bindings.get("value", spec.input_requirements[0])
        pattern = str(spec.parameter_bindings["pattern"])
        value = str(inputs[value_name])
        return re.fullmatch(pattern, value) is not None, f"{value!r} matches {pattern!r}"

    def _existence_check(self, spec: DeterministicRuleSpec, inputs: dict[str, Any]) -> tuple[bool, str]:
        value_name = spec.parameter_bindings.get("value", spec.input_requirements[0])
        value = inputs[value_name]
        exists = bool(value) if not isinstance(value, (list, tuple, set, dict)) else len(value) > 0
        return exists, "present" if exists else "absent"

    def _count_compare(self, spec: DeterministicRuleSpec, inputs: dict[str, Any]) -> tuple[bool, str]:
        value_name = spec.parameter_bindings.get("values", spec.input_requirements[0])
        count = len(_as_iterable(inputs[value_name]))
        expected = spec.expected_cardinality or {}
        min_count = int(expected.get("min", spec.parameter_bindings.get("min", 0)))
        max_count = int(expected.get("max", spec.parameter_bindings.get("max", math.inf)))
        return min_count <= count <= max_count, f"count={count}, expected=[{min_count}, {max_count}]"

    def _derived_calculation(self, spec: DeterministicRuleSpec, inputs: dict[str, Any]) -> tuple[bool, str]:
        # P2a intentionally supports only sum and difference, selected by a
        # policy-owned identifier; it never parses an arbitrary expression.
        names = spec.parameter_bindings.get("operands", spec.input_requirements)
        values = [float(inputs[name]) for name in names]
        mode = spec.parameter_bindings.get("mode", "sum")
        result = sum(values) if mode == "sum" else values[0] - sum(values[1:])
        expected = float(spec.parameter_bindings["expected"])
        tolerance = float((spec.tolerance or {}).get("absolute", 0))
        return abs(result - expected) <= tolerance, f"derived={result}, expected={expected}"


def _as_iterable(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [part for part in value.split(",") if part.strip()]
    if isinstance(value, Iterable):
        return list(value)
    return [value]
