"""Registry-bound semantic strategies for document planning.

Strategies own domain semantics only.  They return planning contracts and
structured issues; they never receive template bytes and never render a file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from .models import (
    CoverageContract,
    DependencyEdge,
    NonEmptyId,
    OutputSpec,
    PlanIssue,
    PlanningModel,
    SemanticUnitPlan,
    UnitTaskSpec,
)


class DomainRequirements(PlanningModel):
    """Deterministic semantic requirements emitted by a domain strategy."""

    strategy_id: NonEmptyId
    strategy_version: NonEmptyId
    required_unit_ids: list[NonEmptyId] = Field(default_factory=list, max_length=100_000)
    required_table_columns: dict[NonEmptyId, list[NonEmptyId]] = Field(default_factory=dict, max_length=100_000)
    issues: list[PlanIssue] = Field(default_factory=list, max_length=10_000)


class DocumentDomainStrategy:
    """Small semantic strategy interface used by the planner registry."""

    strategy_id = ""
    version = "1"
    supported_document_types: tuple[str, ...] = ()

    def identify_requirements(self, output_spec: Any, source_catalog: Any = None) -> DomainRequirements:
        spec = _as_spec(output_spec)
        issues: list[PlanIssue] = []
        if spec.document_type not in self.supported_document_types:
            issues.append(PlanIssue(
                code="strategy_document_type_unsupported",
                severity="error",
                message=f"{self.strategy_id}@{self.version} does not support {spec.document_type}",
                path="document_type",
            ))
        required_units = [unit.unit_id for unit in spec.outline if unit.required]
        table_columns = {
            item.unit_id: list(item.required_columns)
            for item in getattr(spec, "table_requirements", [])
        }
        self._add_domain_requirements(spec, table_columns, issues)
        return DomainRequirements(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            required_unit_ids=required_units,
            required_table_columns=table_columns,
            issues=issues,
        )

    def compile_semantic_units(self, output_spec: Any, layout_contract: Any = None) -> list[SemanticUnitPlan]:
        spec = _as_spec(output_spec)
        return [
            SemanticUnitPlan(
                unit_id=unit.unit_id,
                kind=unit.kind,
                required=unit.required,
                output_schema={"type": "table" if unit.kind == "table" else "text", "required": unit.required},
            )
            for unit in spec.outline
        ]

    def build_dependency_edges(self, semantic_units: Sequence[SemanticUnitPlan]) -> list[DependencyEdge]:
        """Return only explicit semantic dependencies; no hidden fan-out."""

        return []

    def normalize_candidate(self, task: UnitTaskSpec | Mapping[str, Any], candidate: Any) -> dict[str, Any]:
        """Keep candidate normalization typed and free of raw source content."""

        normalized_task = task if isinstance(task, UnitTaskSpec) else UnitTaskSpec.model_validate(task)
        if not isinstance(candidate, Mapping):
            raise ValueError("domain candidates must be structured objects")
        return {"unit_id": normalized_task.unit_id, "candidate": dict(candidate)}

    def review_unit(self, candidate: Any, evidence: Sequence[Any] | None = None) -> list[PlanIssue]:
        if not isinstance(candidate, Mapping):
            return [PlanIssue(
                code="domain_candidate_not_object",
                severity="error",
                message="domain candidate must be a structured object",
                path="candidate",
            )]
        return []

    def review_document(self, document_model: Any, coverage: CoverageContract | Any) -> list[PlanIssue]:
        return []

    def _add_domain_requirements(
        self,
        spec: OutputSpec,
        table_columns: dict[str, list[str]],
        issues: list[PlanIssue],
    ) -> None:
        """Hook for domain-specific column/outline rules."""


class GenericReportStrategy(DocumentDomainStrategy):
    strategy_id = "generic_report"
    supported_document_types = ("generic", "generic_report", "report")


class IcdStrategy(DocumentDomainStrategy):
    strategy_id = "icd"
    supported_document_types = ("icd",)
    required_columns = ("connector", "pin", "signal", "direction", "description")

    def _add_domain_requirements(self, spec, table_columns, issues):
        for unit in spec.outline:
            if unit.kind != "table":
                continue
            missing = sorted(set(self.required_columns) - set(table_columns.get(unit.unit_id, [])))
            if missing:
                issues.append(PlanIssue(
                    code="domain_required_columns_missing",
                    severity="error" if unit.required else "warning",
                    message=f"ICD table {unit.unit_id} is missing required columns: {missing}",
                    path=f"table_requirements.{unit.unit_id}.required_columns",
                ))


class FptStrategy(DocumentDomainStrategy):
    strategy_id = "fpt"
    supported_document_types = ("fpt",)
    required_columns = ("test_case", "signal", "expected", "actual", "status")

    def _add_domain_requirements(self, spec, table_columns, issues):
        for unit in spec.outline:
            if unit.kind != "table":
                continue
            missing = sorted(set(self.required_columns) - set(table_columns.get(unit.unit_id, [])))
            if missing:
                issues.append(PlanIssue(
                    code="domain_required_columns_missing",
                    severity="error" if unit.required else "warning",
                    message=f"FPT table {unit.unit_id} is missing required columns: {missing}",
                    path=f"table_requirements.{unit.unit_id}.required_columns",
                ))


class RequirementsStrategy(DocumentDomainStrategy):
    strategy_id = "requirements"
    supported_document_types = ("requirements", "requirements_spec", "specification")
    required_columns = ("requirement_id", "description", "verification_method", "status")

    def _add_domain_requirements(self, spec, table_columns, issues):
        for unit in spec.outline:
            if unit.kind != "table":
                continue
            missing = sorted(set(self.required_columns) - set(table_columns.get(unit.unit_id, [])))
            if missing:
                issues.append(PlanIssue(
                    code="domain_required_columns_missing",
                    severity="error" if unit.required else "warning",
                    message=f"requirements table {unit.unit_id} is missing required columns: {missing}",
                    path=f"table_requirements.{unit.unit_id}.required_columns",
                ))


def build_builtin_domain_strategies() -> dict[str, DocumentDomainStrategy]:
    """Build the reviewed strategy implementations used by the registry."""

    strategies = [GenericReportStrategy(), IcdStrategy(), FptStrategy(), RequirementsStrategy()]
    return {strategy.strategy_id: strategy for strategy in strategies}


def _as_spec(value: Any) -> OutputSpec:
    if isinstance(value, OutputSpec) or (
        hasattr(value, "document_type") and hasattr(value, "outline")
    ):
        return value
    return OutputSpec.model_validate(value)


__all__ = [
    "DocumentDomainStrategy",
    "DomainRequirements",
    "FptStrategy",
    "GenericReportStrategy",
    "IcdStrategy",
    "RequirementsStrategy",
    "build_builtin_domain_strategies",
]
