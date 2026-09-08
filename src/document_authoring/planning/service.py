"""Pure legacy-template planning compiler and its persistence adapter."""

from __future__ import annotations

from typing import Any, Iterable

from src.document_authoring.contract_registry import CAPABILITIES
from src.document_authoring.models import DocumentSchema, TemplateUnitBinding
from src.document_authoring.template_analysis import TemplateAnalysis

from .models import (
    CoverageContract,
    CoverageRequirement,
    DocumentPlan,
    OutputSpec,
    PlanIssue,
    SemanticUnitPlan,
    TemplateContract,
    UnitTaskSpec,
    StructureContract,
)
from .registry import CapabilityRegistries, build_builtin_registries
from .recipes import (
    RecipeRegistry,
    StructureProfileRegistry,
    build_builtin_recipe_registry,
    build_builtin_structure_profile_registry,
)
from .store import DocumentPlanningStore


class LegacyTemplatePlanningAdapter:
    """Compile existing schema/analysis facts into a deterministic shadow plan."""

    def __init__(
        self,
        *,
        registries: CapabilityRegistries | None = None,
        domain_strategy_id: str = "legacy_document_schema",
        domain_strategy_version: str = "1",
    ):
        self.registries = registries or build_builtin_registries()
        self.domain_strategy_id = domain_strategy_id
        self.domain_strategy_version = domain_strategy_version

    def compile(
        self,
        *,
        output_spec: OutputSpec,
        document_schema: DocumentSchema,
        template_analysis: TemplateAnalysis | None = None,
        bindings: Iterable[TemplateUnitBinding] | None = None,
        source_snapshot_id: str = "legacy-source-snapshot",
        source_snapshot_hash: str = "sha256:legacy-source-snapshot",
    ) -> DocumentPlan:
        spec = output_spec if isinstance(output_spec, OutputSpec) else OutputSpec.model_validate(output_spec)
        schema = document_schema if isinstance(document_schema, DocumentSchema) else DocumentSchema.model_validate(document_schema)
        issues: list[PlanIssue] = []
        binding_by_unit = self._binding_map(bindings or [], schema, issues)
        analysis_by_unit = {
            suggestion.semantic_unit_id: suggestion
            for suggestion in (template_analysis.suggestions if template_analysis else [])
        }

        semantic_units: list[SemanticUnitPlan] = []
        coverage_requirements: list[CoverageRequirement] = []
        unit_tasks: list[UnitTaskSpec] = []
        required_capabilities: list[str] = []
        resolved_capabilities: list[str] = []

        for field in schema.fields:
            kind = "table" if field.value_type == "table" else "field"
            semantic_units.append(SemanticUnitPlan(
                unit_id=field.field_id,
                kind=kind,
                required=field.required,
                output_schema={"type": field.value_type, "required": field.required},
                source_capabilities=[f"{capability}@1" for capability in field.required_capabilities],
                reviewer_policy_id=field.verification_policy_id,
            ))
            if kind == "table":
                table_requirement = next(
                    (item for item in spec.table_requirements if item.unit_id == field.field_id),
                    None,
                )
                columns = list((field.table_columns or {}).values())
                if table_requirement is not None:
                    columns = table_requirement.required_columns
                if not columns:
                    issues.append(PlanIssue(
                        code="table_columns_unresolved",
                        severity="error",
                        message=f"table columns are unresolved for unit {field.field_id}",
                        path=f"table_requirements.{field.field_id}",
                    ))
                    columns = ["unresolved"]
                    required_coverage = False
                else:
                    required_coverage = field.required
                row_keys = table_requirement.row_keys if table_requirement else []
                row_identity_fields = table_requirement.row_identity_fields if table_requirement else []
                row_key_schema = table_requirement.row_key_schema if table_requirement else {}
                row_order = table_requirement.row_order if table_requirement else "declared"
                duplicate_policy = table_requirement.duplicate_policy if table_requirement else "reject"
                if field.required and not row_keys:
                    issues.append(PlanIssue(
                        code="row_scope_unresolved",
                        severity="error",
                        message=f"row scope is unresolved for table unit {field.field_id}",
                        path=f"table_requirements.{field.field_id}.row_scope",
                    ))
                coverage_requirements.append(CoverageRequirement(
                    requirement_id=field.field_id,
                    unit_id=field.field_id,
                    kind="table",
                    required=required_coverage,
                    row_keys=row_keys,
                    required_columns=columns,
                    row_identity_fields=row_identity_fields,
                    row_key_schema=row_key_schema,
                    row_order=row_order,
                    duplicate_policy=duplicate_policy,
                ))
            else:
                coverage_requirements.append(CoverageRequirement(
                    requirement_id=field.field_id,
                    unit_id=field.field_id,
                    kind="scalar" if field.value_type != "text" else "paragraph",
                    required=field.required,
                    min_evidence_items=1 if field.required else 0,
                ))
            self._append_capabilities(
                field.required_capabilities,
                required_capabilities,
                resolved_capabilities,
                issues,
                path=f"fields.{field.field_id}.required_capabilities",
            )
            unit_tasks.append(self._unit_task(
                unit_id=field.field_id,
                plan_version=spec.version,
                output_schema={"type": field.value_type},
                source_capabilities=field.required_capabilities,
                retrieval_policy_id=field.retrieval_policy_id,
            ))

        for review in schema.review_items:
            semantic_units.append(SemanticUnitPlan(
                unit_id=review.review_item_id,
                kind="review_item",
                required=True,
                output_schema={"type": "review", "severity": review.severity},
                source_capabilities=[f"{capability}@1" for capability in review.required_capabilities],
                reviewer_policy_id=review.pass_policy_id,
            ))
            coverage_requirements.append(CoverageRequirement(
                requirement_id=review.review_item_id,
                unit_id=review.review_item_id,
                kind="scalar",
                required=True,
                min_evidence_items=1,
            ))
            self._append_capabilities(
                review.required_capabilities,
                required_capabilities,
                resolved_capabilities,
                issues,
                path=f"review_items.{review.review_item_id}.required_capabilities",
            )
            unit_tasks.append(self._unit_task(
                unit_id=review.review_item_id,
                plan_version=spec.version,
                output_schema={"type": "review"},
                source_capabilities=review.required_capabilities,
                retrieval_policy_id=review.retrieval_rule_id,
            ))

        layout_contract, layout_adapter_id, layout_adapter_version = self._layout_contract(
            spec,
            binding_by_unit,
            analysis_by_unit,
            issues,
        )
        layout_result = self.registries.layout_adapters.resolve(layout_adapter_id, layout_adapter_version)
        if isinstance(layout_result, PlanIssue):
            issues.append(layout_result)

        primary = next(item for item in spec.artifact.deliverables if item.role == "primary")
        renderer_result = self.registries.renderers.resolve(primary.format, "1")
        renderer_capability_version = "1"
        if isinstance(renderer_result, PlanIssue):
            issues.append(renderer_result)
        else:
            renderer_capability_version = renderer_result.version
            renderer_ref = f"{primary.format}@{renderer_result.version}"
            required_capabilities.append(renderer_ref)
            resolved_capabilities.append(renderer_ref)

        strategy_result = self.registries.domain_strategies.resolve(
            self.domain_strategy_id,
            self.domain_strategy_version,
        )
        if isinstance(strategy_result, PlanIssue):
            issues.append(strategy_result)

        # A binding is a physical allowlist edge, never a source of cell text.
        for unit in semantic_units:
            if unit.unit_id not in binding_by_unit and unit.unit_id not in analysis_by_unit:
                issues.append(PlanIssue(
                    code="binding_missing",
                    severity="error",
                    message=f"no physical binding was found for unit {unit.unit_id}",
                    path=f"semantic_units.{unit.unit_id}",
                ))

        output_summary = {
            "purpose": spec.purpose,
            "document_type": spec.document_type,
            "target_identity": dict(spec.target_identity),
            "layout_mode": spec.layout_source.mode,
            "primary_format": primary.format,
            "outline_unit_ids": [unit.unit_id for unit in spec.outline],
            "table_unit_ids": [item.unit_id for item in spec.table_requirements],
            "missing_data_policy": spec.missing_data_policy,
            "inference_policy": spec.inference_policy,
            "approval_policy_id": spec.approval_policy_id,
        }
        return DocumentPlan(
            document_plan_id=f"plan:{spec.output_spec_id}",
            version=spec.version,
            status="blocked" if any(issue.blocking for issue in issues) else "proposed",
            output_spec_id=spec.output_spec_id,
            output_spec_version=spec.version,
            output_spec_hash=spec.content_hash,
            output_spec_summary=output_summary,
            source_snapshot_id=source_snapshot_id,
            source_snapshot_hash=source_snapshot_hash,
            domain_strategy_id=self.domain_strategy_id,
            domain_strategy_version=self.domain_strategy_version,
            layout_adapter_id=layout_adapter_id,
            layout_adapter_version=layout_adapter_version,
            renderer_capability_id=primary.format,
            renderer_capability_version=renderer_capability_version,
            layout_contract=layout_contract,
            semantic_units=semantic_units,
            dependency_edges=[],
            coverage_contract=CoverageContract(requirements=coverage_requirements),
            unit_tasks=unit_tasks,
            required_capabilities=self._unique(required_capabilities),
            resolved_capabilities=self._unique(resolved_capabilities),
            issues=issues,
            retrieval_specs=self._retrieval_specs(schema),
            unit_review_policy={"policy_id": "unit-default@1"},
            document_review_policy={"policy_id": "document-default@1"},
            render_spec={"format": primary.format, "deliverables": [item.format for item in spec.artifact.deliverables]},
            approval_policy={"policy_id": spec.approval_policy_id},
        )

    def _layout_contract(
        self,
        spec: OutputSpec,
        binding_by_unit: dict[str, TemplateUnitBinding],
        analysis_by_unit: dict[str, Any],
        issues: list[PlanIssue],
    ) -> tuple[TemplateContract | StructureContract, str, str]:
        source = spec.layout_source
        if source.mode == "provided_template":
            bindings: dict[str, list[str]] = {}
            table_schemas: dict[str, dict[str, Any]] = {}
            for unit_id, binding in binding_by_unit.items():
                bindings[unit_id] = list(binding.target_region_ids)
                if binding.table_schema is not None:
                    table_schemas[unit_id] = binding.table_schema.model_dump(mode="json")
            for unit_id, suggestion in analysis_by_unit.items():
                bindings.setdefault(unit_id, list(suggestion.target_unit_ids))
            return TemplateContract(
                template_version_id=source.template_version_id,
                template_schema_id=source.template_schema_id,
                template_schema_version=source.template_schema_version,
                bindings=bindings,
                table_schemas=table_schemas,
            ), "provided_template", "1"
        if source.mode == "system_recipe":
            issues.append(PlanIssue(
                code="layout_capability_unavailable",
                severity="error",
                message="system recipe layout execution is deferred to Phase 3",
                path="layout_source",
            ))
            return StructureContract(
                structure_profile_id=source.recipe_id,
                structure_profile_version=source.recipe_version,
            ), "system_recipe", "1"
        issues.append(PlanIssue(
            code="layout_capability_unavailable",
            severity="error",
            message="generated structure layout execution is deferred to Phase 3",
            path="layout_source",
        ))
        return StructureContract(
            structure_profile_id=source.constraints_profile_id,
            structure_profile_version=source.constraints_profile_version,
        ), "generated_structure", "1"

    @staticmethod
    def _binding_map(
        bindings: Iterable[TemplateUnitBinding],
        schema: DocumentSchema,
        issues: list[PlanIssue],
    ) -> dict[str, TemplateUnitBinding]:
        unit_ids = {field.field_id for field in schema.fields} | {item.review_item_id for item in schema.review_items}
        result: dict[str, TemplateUnitBinding] = {}
        for binding in bindings:
            if binding.semantic_unit_id not in unit_ids:
                issues.append(PlanIssue(
                    code="binding_unknown_unit",
                    severity="error",
                    message=f"binding references unknown unit {binding.semantic_unit_id}",
                    path="bindings",
                ))
                continue
            if binding.semantic_unit_id in result:
                issues.append(PlanIssue(
                    code="binding_duplicate_unit",
                    severity="error",
                    message=f"multiple bindings reference unit {binding.semantic_unit_id}",
                    path="bindings",
                ))
                continue
            result[binding.semantic_unit_id] = binding
        return result

    @staticmethod
    def _unit_task(
        *,
        unit_id: str,
        plan_version: int,
        output_schema: dict[str, Any],
        source_capabilities: list[str],
        retrieval_policy_id: str,
    ) -> UnitTaskSpec:
        capability_refs = [f"{capability}@1" for capability in source_capabilities]
        return UnitTaskSpec(
            task_id=f"unit-task:{unit_id}",
            unit_id=unit_id,
            plan_version=plan_version,
            input_schema={"type": "frozen_source_scope"},
            output_schema=output_schema,
            allowed_sources=["frozen_source_scope"],
            allowed_retrievers=[f"{retrieval_policy_id}@1"],
            allowed_tools=capability_refs,
            max_attempts=3,
            timeout_seconds=300,
            action_key=f"document-plan:unit:{unit_id}:v{plan_version}",
        )

    @staticmethod
    def _append_capabilities(
        capabilities: list[str],
        required: list[str],
        resolved: list[str],
        issues: list[PlanIssue],
        *,
        path: str,
    ) -> None:
        for capability in capabilities:
            raw = str(capability).strip()
            if not raw:
                continue
            if "@" in raw:
                identifier, version = raw.rsplit("@", 1)
            else:
                identifier, version = raw, "1"
            reference = f"{identifier}@{version}"
            required.append(reference)
            if identifier not in CAPABILITIES:
                issues.append(PlanIssue(
                    code="capability_missing",
                    severity="error",
                    message=f"required capability {reference} is not registered",
                    path=path,
                ))
            else:
                resolved.append(reference)

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value) for value in values if str(value).strip()))

    @staticmethod
    def _retrieval_specs(schema: DocumentSchema) -> list[dict[str, str]]:
        specs: list[dict[str, str]] = []
        for field in schema.fields:
            specs.append({"unit_id": field.field_id, "retrieval_policy_id": field.retrieval_policy_id})
        for review in schema.review_items:
            specs.append({"unit_id": review.review_item_id, "retrieval_policy_id": review.retrieval_rule_id})
        return specs


class TemplateFreePlanningAdapter:
    """Compile an OutputSpec into a controlled recipe-backed structure plan.

    This adapter never accepts template coordinates or model-generated package
    instructions.  It only turns the user-owned outline/table contract into
    semantic units and resolves exact server-owned recipe/capability versions.
    """

    def __init__(
        self,
        *,
        registries: CapabilityRegistries | None = None,
        recipe_registry: RecipeRegistry | None = None,
        profile_registry: StructureProfileRegistry | None = None,
        domain_strategy_id: str = "generic_report",
        domain_strategy_version: str = "1",
    ) -> None:
        self.registries = registries or build_builtin_registries()
        self.recipe_registry = recipe_registry or build_builtin_recipe_registry()
        self.profile_registry = profile_registry or build_builtin_structure_profile_registry()
        self.domain_strategy_id = domain_strategy_id
        self.domain_strategy_version = domain_strategy_version

    @staticmethod
    def _append_structure_profile_issues(
        profile: Any,
        *,
        document_type: str,
        output_format: str,
        issues: list[PlanIssue],
    ) -> None:
        if document_type not in profile.supported_document_types:
            issues.append(PlanIssue(
                code="structure_profile_document_type_unsupported",
                severity="error",
                message=f"structure profile {profile.profile_id}@{profile.version} does not support {document_type}",
                path="document_type",
            ))
        if output_format not in profile.supported_formats:
            issues.append(PlanIssue(
                code="structure_profile_format_unsupported",
                severity="error",
                message=f"structure profile {profile.profile_id}@{profile.version} does not support {output_format}",
                path="artifact.deliverables",
            ))
        if not profile.ready_for_generated_structure:
            failed = ", ".join(
                name for name, status in profile.readiness_statuses.items() if status != "passed"
            ) or ("status=" + str(profile.status))
            issues.append(PlanIssue(
                code="structure_profile_gates_incomplete",
                severity="error",
                message=f"structure profile {profile.profile_id}@{profile.version} is not ready: {failed}",
                path="layout_source",
            ))

    def compile(
        self,
        *,
        output_spec: OutputSpec,
        source_snapshot_id: str,
        source_snapshot_hash: str,
    ) -> DocumentPlan:
        spec = output_spec if isinstance(output_spec, OutputSpec) else OutputSpec.model_validate(output_spec)
        issues: list[PlanIssue] = []
        primary = next(item for item in spec.artifact.deliverables if item.role == "primary")
        layout_source = spec.layout_source
        if layout_source.mode == "system_recipe":
            recipe_id = layout_source.recipe_id
            recipe_version = layout_source.recipe_version
            adapter_id = "system_recipe"
        elif layout_source.mode == "generated_structure":
            profile_id = layout_source.constraints_profile_id
            profile_version = layout_source.constraints_profile_version
            profile = self.profile_registry.lookup(profile_id, profile_version)
            if profile is None:
                issues.append(PlanIssue(
                    code="structure_profile_missing",
                    severity="error",
                    message=f"structure profile {profile_id}@{profile_version} is not registered",
                    path="layout_source",
                ))
                recipe_id = profile_id
                recipe_version = profile_version
            else:
                recipe_id = profile.recipe_id
                recipe_version = profile.recipe_version
                self._append_structure_profile_issues(
                    profile,
                    document_type=spec.document_type,
                    output_format=primary.format,
                    issues=issues,
                )
            adapter_id = "generated_structure"
        else:
            raise ValueError("template-free planning requires a template-free layout source")

        recipe = self.recipe_registry.lookup(recipe_id, recipe_version)
        if recipe is None:
            issues.append(PlanIssue(
                code="recipe_missing",
                severity="error",
                message=f"system recipe {recipe_id}@{recipe_version} is not registered",
                path="layout_source",
            ))
        elif recipe.status != "available":
            issues.append(PlanIssue(
                code="recipe_unavailable",
                severity="error",
                message=f"system recipe {recipe_id}@{recipe_version} is unavailable",
                path="layout_source",
            ))
        elif primary.format not in recipe.supported_formats:
            issues.append(PlanIssue(
                code="recipe_format_unsupported",
                severity="error",
                message=f"recipe {recipe_id}@{recipe_version} does not support {primary.format}",
                path="artifact.deliverables",
            ))

        if layout_source.mode == "system_recipe":
            profile = self.profile_registry.lookup(
                recipe.constraints_profile_id, recipe.constraints_profile_version,
            ) if recipe is not None else None
            if profile is None:
                issues.append(PlanIssue(
                    code="structure_profile_missing",
                    severity="error",
                    message=(
                        f"structure profile {getattr(recipe, 'constraints_profile_id', recipe_id)}@"
                        f"{getattr(recipe, 'constraints_profile_version', recipe_version)} is not registered"
                    ),
                    path="layout_source",
                ))
            elif spec.document_type not in profile.supported_document_types or primary.format not in profile.supported_formats:
                self._append_structure_profile_issues(
                    profile,
                    document_type=spec.document_type,
                    output_format=primary.format,
                    issues=issues,
                )

        adapter_result = self.registries.layout_adapters.resolve(adapter_id, "1")
        if isinstance(adapter_result, PlanIssue):
            issues.append(adapter_result)
        renderer_result = self.registries.renderers.resolve(primary.format, "1")
        if isinstance(renderer_result, PlanIssue):
            issues.append(renderer_result)
        strategy_result = self.registries.domain_strategies.resolve(
            self.domain_strategy_id, self.domain_strategy_version,
        )
        if isinstance(strategy_result, PlanIssue):
            issues.append(strategy_result)
        else:
            if spec.document_type not in strategy_result.supported_document_types:
                issues.append(PlanIssue(
                    code="strategy_document_type_unsupported",
                    severity="error",
                    message=(
                        f"domain strategy {self.domain_strategy_id}@{self.domain_strategy_version} "
                        f"does not support {spec.document_type}"
                    ),
                    path="document_type",
                ))
            implementation = self.registries.domain_strategies.implementation(
                self.domain_strategy_id, self.domain_strategy_version,
            )
            if implementation is None:
                issues.append(PlanIssue(
                    code="strategy_implementation_missing",
                    severity="error",
                    message=(
                        f"domain strategy {self.domain_strategy_id}@{self.domain_strategy_version} "
                        "has no registered implementation"
                    ),
                    path="domain_strategy",
                ))
            else:
                domain_requirements = implementation.identify_requirements(spec, None)
                issues.extend(domain_requirements.issues)

        structure_components = list(recipe.component_ids) if recipe is not None else []
        semantic_units: list[SemanticUnitPlan] = []
        coverage_requirements: list[CoverageRequirement] = []
        unit_tasks: list[UnitTaskSpec] = []
        table_by_unit = {item.unit_id: item for item in spec.table_requirements}
        required_capabilities = [
            f"{adapter_id}@1",
            f"{primary.format}@1",
            f"{self.domain_strategy_id}@{self.domain_strategy_version}",
        ]
        resolved_capabilities = [
            f"{adapter_id}@1",
            f"{primary.format}@{renderer_result.version}" if not isinstance(renderer_result, PlanIssue) else "",
            f"{self.domain_strategy_id}@{self.domain_strategy_version}" if not isinstance(strategy_result, PlanIssue) else "",
        ]
        for unit in spec.outline:
            semantic_units.append(SemanticUnitPlan(
                unit_id=unit.unit_id,
                kind=unit.kind,
                required=unit.required,
                output_schema={"type": "table" if unit.kind == "table" else "text", "required": unit.required},
            ))
            table = table_by_unit.get(unit.unit_id)
            if unit.kind == "table":
                if table is None:
                    issues.append(PlanIssue(
                        code="table_requirement_missing",
                        severity="error" if unit.required else "warning",
                        message=f"table requirement is missing for unit {unit.unit_id}",
                        path=f"outline.{unit.unit_id}",
                    ))
                    coverage_requirements.append(CoverageRequirement(
                        requirement_id=unit.unit_id,
                        unit_id=unit.unit_id,
                        kind="table",
                        required=unit.required,
                        required_columns=["unresolved"],
                    ))
                else:
                    coverage_requirements.append(CoverageRequirement(
                        requirement_id=unit.unit_id,
                        unit_id=unit.unit_id,
                        kind="table",
                        required=unit.required,
                        row_keys=list(table.row_keys),
                        required_columns=list(table.required_columns),
                        row_identity_fields=list(table.row_identity_fields),
                        row_key_schema=dict(table.row_key_schema),
                        row_order=table.row_order,
                        duplicate_policy=table.duplicate_policy,
                    ))
            else:
                coverage_kind = "scalar" if unit.kind in {"field", "review_item"} else unit.kind
                coverage_requirements.append(CoverageRequirement(
                    requirement_id=unit.unit_id,
                    unit_id=unit.unit_id,
                    kind=coverage_kind,
                    required=unit.required,
                    min_evidence_items=1 if unit.required else 0,
                ))
            output_schema = {"type": "table" if unit.kind == "table" else "text"}
            if table is not None:
                output_schema.update({
                    "row_keys": list(table.row_keys),
                    "required_columns": list(table.required_columns),
                    "row_order": table.row_order,
                    "duplicate_policy": table.duplicate_policy,
                })
            unit_tasks.append(UnitTaskSpec(
                task_id=f"unit-task:{unit.unit_id}",
                unit_id=unit.unit_id,
                plan_version=spec.version,
                input_schema={"type": "frozen_source_scope"},
                output_schema=output_schema,
                row_scope=table.row_scope if table is not None else None,
                allowed_sources=["frozen_source_scope"],
                allowed_retrievers=["generic_report@1"],
                max_attempts=3,
                timeout_seconds=300,
                action_key=f"document-plan:unit:{unit.unit_id}:v{spec.version}",
            ))

        output_summary = {
            "purpose": spec.purpose,
            "document_type": spec.document_type,
            "target_identity": dict(spec.target_identity),
            "layout_mode": layout_source.mode,
            "recipe_id": recipe_id,
            "recipe_version": recipe_version,
            "primary_format": primary.format,
            "outline_unit_ids": [unit.unit_id for unit in spec.outline],
            "table_unit_ids": [item.unit_id for item in spec.table_requirements],
            "missing_data_policy": spec.missing_data_policy,
            "inference_policy": spec.inference_policy,
            "approval_policy_id": spec.approval_policy_id,
        }
        return DocumentPlan(
            document_plan_id=f"plan:{spec.output_spec_id}",
            version=spec.version,
            status="blocked" if any(issue.blocking for issue in issues) else "proposed",
            output_spec_id=spec.output_spec_id,
            output_spec_version=spec.version,
            output_spec_hash=spec.content_hash,
            output_spec_summary=output_summary,
            source_snapshot_id=source_snapshot_id,
            source_snapshot_hash=source_snapshot_hash,
            domain_strategy_id=self.domain_strategy_id,
            domain_strategy_version=self.domain_strategy_version,
            layout_adapter_id=adapter_id,
            layout_adapter_version="1",
            renderer_capability_id=primary.format,
            renderer_capability_version=(renderer_result.version if not isinstance(renderer_result, PlanIssue) else "1"),
            layout_contract=StructureContract(
                structure_profile_id=recipe_id,
                structure_profile_version=recipe_version,
                components=structure_components,
            ),
            semantic_units=semantic_units,
            dependency_edges=[],
            coverage_contract=CoverageContract(requirements=coverage_requirements),
            unit_tasks=unit_tasks,
            required_capabilities=list(dict.fromkeys(required_capabilities)),
            resolved_capabilities=list(dict.fromkeys(value for value in resolved_capabilities if value)),
            issues=issues,
            retrieval_specs=[{"unit_id": unit.unit_id, "retrieval_policy_id": "generic_report"} for unit in spec.outline],
            unit_review_policy={"policy_id": "unit-default@1"},
            document_review_policy={"policy_id": "document-default@1"},
            render_spec={
                "format": primary.format,
                "deliverables": [item.format for item in spec.artifact.deliverables],
                "template_free": True,
                "recipe_id": recipe_id,
                "recipe_version": recipe_version,
            },
            approval_policy={"policy_id": spec.approval_policy_id},
        )


class DocumentPlanningService:
    """Thin coordinator around the pure compiler and optional persistence."""

    def __init__(
        self,
        *,
        adapter: LegacyTemplatePlanningAdapter | None = None,
        template_free_adapter: TemplateFreePlanningAdapter | None = None,
        store: DocumentPlanningStore | None = None,
    ):
        self.adapter = adapter or LegacyTemplatePlanningAdapter()
        self.template_free_adapter = template_free_adapter or TemplateFreePlanningAdapter()
        self.store = store

    def compile(self, **kwargs: Any) -> DocumentPlan:
        output_spec = kwargs.get("output_spec")
        layout_source = getattr(output_spec, "layout_source", None)
        layout_mode = getattr(layout_source, "mode", None)
        if layout_mode in {"system_recipe", "generated_structure"}:
            # Template-free plans have a separate compiler and never receive
            # template analysis/binding arguments from legacy callers.
            return self.template_free_adapter.compile(
                output_spec=output_spec,
                source_snapshot_id=kwargs["source_snapshot_id"],
                source_snapshot_hash=kwargs["source_snapshot_hash"],
            )
        return self.adapter.compile(**kwargs)

    def compile_template_free(self, **kwargs: Any) -> DocumentPlan:
        return self.template_free_adapter.compile(**kwargs)

    def persist_proposal(
        self,
        *,
        output_spec: OutputSpec,
        plan: DocumentPlan,
        tenant_id: str,
        user_id: str,
        task_id: str,
        idempotency_key: str | None = None,
    ) -> DocumentPlan:
        """Persist one immutable, hash-bound proposal and its audit event.

        Both rows are owner-scoped and idempotent.  A retry with the same
        semantic versions returns the existing rows; a caller that attempts
        to reuse an identity with different content is rejected by the store.
        """

        spec = output_spec if isinstance(output_spec, OutputSpec) else OutputSpec.model_validate(output_spec)
        candidate = plan if isinstance(plan, DocumentPlan) else DocumentPlan.model_validate(plan)
        if candidate.output_spec_id != spec.output_spec_id or candidate.output_spec_version != spec.version:
            raise ValueError("document plan is not bound to the proposed OutputSpec")
        if candidate.output_spec_hash != spec.content_hash:
            raise ValueError("document plan OutputSpec hash does not match the proposed spec")
        if self.store is None:
            return candidate
        persisted_spec = self.store.create_output_spec(
            spec,
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
        )
        persisted_plan = self.store.create_plan(
            candidate,
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
        )
        event_key = str(idempotency_key or f"plan-proposal:{candidate.document_plan_id}:v{candidate.version}").strip()
        self.store.append_event(
            task_id=task_id,
            event_type="document_plan_proposed",
            idempotency_key=event_key,
            payload={
                "document_plan_id": persisted_plan.document_plan_id,
                "document_plan_version": persisted_plan.version,
                "output_spec_id": persisted_spec.output_spec_id,
                "output_spec_version": persisted_spec.version,
                "plan_hash": persisted_plan.plan_hash,
            },
        )
        return persisted_plan

    def propose_shadow(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        output_spec: OutputSpec,
        **compile_kwargs: Any,
    ) -> DocumentPlan:
        plan = self.compile(output_spec=output_spec, **compile_kwargs)
        if self.store is not None:
            self.store.create_output_spec(
                output_spec,
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
            )
            self.store.create_plan(
                plan,
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
            )
        return plan


__all__ = [
    "DocumentPlanningService",
    "LegacyTemplatePlanningAdapter",
    "TemplateFreePlanningAdapter",
]
