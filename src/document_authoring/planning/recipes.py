"""Server-owned contracts for controlled template-free authoring.

The planner may select a recipe, but neither a model nor a client can define a
new component, coordinate, package part, or style.  A recipe is a small,
versioned allowlist of semantic block kinds plus resource limits.  Rendering
code consumes the immutable binding produced here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, model_validator

from src.document_authoring.document_model import DocumentModel

from .models import DocumentPlan, NonEmptyId, PlanningModel, StructureContract, planning_content_hash


BlockKind = Literal["section", "paragraph", "list", "table", "cross_reference"]


class StructureConstraintsProfile(PlanningModel):
    """Resource and component limits for one generated-structure profile."""

    profile_id: NonEmptyId
    version: NonEmptyId
    allowed_component_ids: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    supported_formats: list[NonEmptyId] = Field(default_factory=list, max_length=16)
    max_components: int = Field(default=256, ge=1, le=10_000)
    max_text_chars: int = Field(default=200_000, ge=1, le=10_000_000)
    max_table_rows: int = Field(default=10_000, ge=1, le=100_000)
    max_table_columns: int = Field(default=100, ge=1, le=1_000)
    max_artifact_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=250 * 1024 * 1024)
    max_nesting_depth: int = Field(default=8, ge=1, le=32)

    @model_validator(mode="after")
    def normalize(self) -> "StructureConstraintsProfile":
        self.allowed_component_ids = _unique(self.allowed_component_ids, "allowed_component_ids")
        self.supported_formats = _unique(self.supported_formats, "supported_formats")
        return self


class RecipeComponent(PlanningModel):
    """One server-owned semantic component available to a recipe."""

    component_id: NonEmptyId
    block_kinds: list[BlockKind] = Field(min_length=1, max_length=8)
    supported_formats: list[NonEmptyId] = Field(min_length=1, max_length=16)
    max_instances: int = Field(default=10_000, ge=1, le=100_000)

    @model_validator(mode="after")
    def normalize(self) -> "RecipeComponent":
        self.supported_formats = _unique(self.supported_formats, "component supported_formats")
        if len(set(self.block_kinds)) != len(self.block_kinds):
            raise ValueError("recipe component block_kinds must be unique")
        return self


class SystemRecipe(PlanningModel):
    """Immutable, versioned description of a safe template-free layout."""

    recipe_id: NonEmptyId
    version: NonEmptyId
    supported_document_types: list[NonEmptyId] = Field(min_length=1, max_length=64)
    supported_formats: list[NonEmptyId] = Field(min_length=1, max_length=16)
    constraints_profile_id: NonEmptyId
    constraints_profile_version: NonEmptyId
    components: list[RecipeComponent] = Field(min_length=1, max_length=128)
    status: Literal["available", "unavailable", "disabled"] = "available"
    unavailable_reason: str | None = Field(default=None, max_length=1_000)
    recipe_hash: str | None = None

    @model_validator(mode="after")
    def validate_recipe(self) -> "SystemRecipe":
        self.supported_document_types = _unique(
            self.supported_document_types, "supported_document_types",
        )
        self.supported_formats = _unique(self.supported_formats, "supported_formats")
        component_ids = [component.component_id for component in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("recipe component IDs must be unique")
        supported = set(self.supported_formats)
        if any(set(component.supported_formats) - supported for component in self.components):
            raise ValueError("recipe component format is not supported by the recipe")
        if self.status != "available" and not self.unavailable_reason:
            raise ValueError("unavailable recipes require unavailable_reason")
        expected = planning_content_hash(self, exclude={"recipe_hash"})
        if self.recipe_hash is not None and self.recipe_hash != expected:
            raise ValueError("recipe_hash does not match recipe contents")
        object.__setattr__(self, "recipe_hash", expected)
        return self

    @property
    def component_ids(self) -> list[str]:
        return [component.component_id for component in self.components]

    def component_for_kind(self, block_kind: str, output_format: str) -> RecipeComponent | None:
        normalized_format = str(output_format or "").strip().lower().lstrip(".")
        for component in self.components:
            if block_kind in component.block_kinds and normalized_format in component.supported_formats:
                return component
        return None


class RecipeRegistry:
    """Exact-version registry; there is deliberately no version fallback."""

    def __init__(self) -> None:
        self._recipes: dict[tuple[str, str], SystemRecipe] = {}

    def register(self, recipe: SystemRecipe) -> SystemRecipe:
        key = (recipe.recipe_id, recipe.version)
        if key in self._recipes:
            raise ValueError(f"duplicate system recipe registration: {recipe.recipe_id}@{recipe.version}")
        self._recipes[key] = recipe
        return recipe

    def lookup(self, recipe_id: str, version: str) -> SystemRecipe | None:
        return self._recipes.get((str(recipe_id).strip(), str(version).strip()))

    get = lookup

    def resolve(self, recipe_id: str, version: str) -> SystemRecipe:
        recipe = self.lookup(recipe_id, version)
        if recipe is None:
            raise ValueError(f"system recipe {recipe_id}@{version} is not registered")
        if recipe.status != "available":
            raise ValueError(
                f"system recipe {recipe_id}@{version} is unavailable: "
                f"{recipe.unavailable_reason or recipe.status}"
            )
        return recipe

    def list(self) -> list[SystemRecipe]:
        return [self._recipes[key] for key in sorted(self._recipes)]


class StructureRenderBinding(PlanningModel):
    """A logical semantic unit to recipe component binding."""

    unit_id: NonEmptyId
    block_id: NonEmptyId
    block_kind: BlockKind
    component_id: NonEmptyId
    ordinal: int = Field(ge=0)


class StructureBindingSet(PlanningModel):
    """Hash-bound logical bindings; it contains no physical file coordinates."""

    plan_id: NonEmptyId
    plan_version: int = Field(ge=1)
    plan_hash: NonEmptyId
    recipe_id: NonEmptyId
    recipe_version: NonEmptyId
    recipe_hash: NonEmptyId
    profile_id: NonEmptyId
    profile_version: NonEmptyId
    output_format: NonEmptyId
    bindings: list[StructureRenderBinding] = Field(min_length=1, max_length=10_000)
    binding_hash: str | None = None

    @model_validator(mode="after")
    def hash_bindings(self) -> "StructureBindingSet":
        unit_ids = [binding.unit_id for binding in self.bindings]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("structure bindings must contain one binding per unit")
        expected = planning_content_hash(self, exclude={"binding_hash"})
        if self.binding_hash is not None and self.binding_hash != expected:
            raise ValueError("structure binding hash does not match contents")
        object.__setattr__(self, "binding_hash", expected)
        return self


class StructureBindingCompiler:
    """Validate a plan/model pair and bind blocks to registered components."""

    _SAFE_HINT_KEYS = frozenset({"level", "emphasis", "orientation", "keep_with_next"})

    def __init__(self, registry: RecipeRegistry | None = None) -> None:
        self.registry = registry or build_builtin_recipe_registry()

    def compile(
        self,
        plan: DocumentPlan | Mapping[str, Any],
        document_model: DocumentModel | Mapping[str, Any],
        recipe: SystemRecipe | None = None,
    ) -> StructureBindingSet:
        candidate_plan = plan if isinstance(plan, DocumentPlan) else DocumentPlan.model_validate(plan)
        model = (
            document_model
            if isinstance(document_model, DocumentModel)
            else DocumentModel.model_validate(document_model)
        )
        if candidate_plan.status != "accepted":
            raise ValueError("template-free structure binding requires an accepted DocumentPlan")
        contract = candidate_plan.layout_contract
        if not isinstance(contract, StructureContract):
            raise ValueError("template-free structure binding requires a StructureContract")
        if model.plan_id and model.plan_id != candidate_plan.document_plan_id:
            raise ValueError("document model plan identity does not match the accepted plan")
        if model.plan_hash and model.plan_hash != candidate_plan.plan_hash:
            raise ValueError("document model plan hash does not match the accepted plan")

        output_format = str(
            (candidate_plan.render_spec or {}).get("format")
            or candidate_plan.renderer_capability_id
        ).strip().lower().lstrip(".")
        recipe_id = str(
            (candidate_plan.render_spec or {}).get("recipe_id")
            or contract.structure_profile_id
        ).strip()
        recipe_version = str(
            (candidate_plan.render_spec or {}).get("recipe_version")
            or contract.structure_profile_version
        ).strip()
        selected = recipe or self.registry.resolve(recipe_id, recipe_version)
        if selected.recipe_id != recipe_id or selected.version != recipe_version:
            raise ValueError("supplied recipe is not the exact recipe selected by the plan")
        if output_format not in selected.supported_formats:
            raise ValueError(f"recipe does not support output format: {output_format}")

        selected_components = set(contract.components)
        unknown_selected = selected_components - set(selected.component_ids)
        if unknown_selected:
            raise ValueError(f"structure contract references unknown component(s): {sorted(unknown_selected)}")

        plan_units = {str(unit.unit_id): unit for unit in candidate_plan.semantic_units}
        bindings: list[StructureRenderBinding] = []
        total_text = 0
        component_counts: dict[str, int] = {}
        if len(model.blocks) > selected_profile(selected).max_components:
            raise ValueError("structure exceeds the recipe component limit")
        for ordinal, block in enumerate(model.blocks):
            hints = getattr(block, "layout_hints", {}) or {}
            unsafe = set(hints) - self._SAFE_HINT_KEYS
            if unsafe:
                raise ValueError(f"layout hint is not allowlisted: {sorted(unsafe)}")
            unit_id = str(block.unit_id).strip()
            plan_unit = plan_units.get(unit_id)
            if plan_unit is None:
                raise ValueError(f"document model block is outside the accepted plan: {unit_id}")
            component = selected.component_for_kind(block.kind, output_format)
            if component is None:
                raise ValueError(f"recipe has no component for block kind: {block.kind}")
            if selected_components and component.component_id not in selected_components:
                raise ValueError(f"component is outside the frozen structure contract: {component.component_id}")
            component_counts[component.component_id] = component_counts.get(component.component_id, 0) + 1
            if component_counts[component.component_id] > component.max_instances:
                raise ValueError(f"component instance limit exceeded: {component.component_id}")
            text_size, table_rows, table_columns = _block_size(block)
            total_text += text_size
            profile = selected_profile(selected)
            if total_text > profile.max_text_chars:
                raise ValueError("structure exceeds the recipe text limit")
            if table_rows > profile.max_table_rows:
                raise ValueError("structure exceeds the recipe table row limit")
            if table_columns > profile.max_table_columns:
                raise ValueError("structure exceeds the recipe table column limit")
            bindings.append(StructureRenderBinding(
                unit_id=unit_id,
                block_id=str(block.block_id),
                block_kind=block.kind,
                component_id=component.component_id,
                ordinal=ordinal,
            ))

        if not bindings:
            raise ValueError("template-free structure requires at least one renderable block")
        profile = selected_profile(selected)
        return StructureBindingSet(
            plan_id=candidate_plan.document_plan_id,
            plan_version=candidate_plan.version,
            plan_hash=candidate_plan.plan_hash or "",
            recipe_id=selected.recipe_id,
            recipe_version=selected.version,
            recipe_hash=selected.recipe_hash or "",
            profile_id=profile.profile_id,
            profile_version=profile.version,
            output_format=output_format,
            bindings=bindings,
        )


def selected_profile(recipe: SystemRecipe) -> StructureConstraintsProfile:
    """Return the built-in profile represented by a recipe.

    Profiles are embedded in the immutable recipe for now.  Keeping this
    helper as a boundary lets Phase 3B replace it with an exact-version profile
    registry without changing renderers.
    """

    limits = {
        "generic-report": dict(max_components=256, max_text_chars=200_000, max_table_rows=10_000, max_table_columns=32),
        "structured-table": dict(max_components=128, max_text_chars=100_000, max_table_rows=10_000, max_table_columns=100),
    }.get(recipe.recipe_id, {})
    return StructureConstraintsProfile(
        profile_id=recipe.constraints_profile_id,
        version=recipe.constraints_profile_version,
        allowed_component_ids=recipe.component_ids,
        supported_formats=recipe.supported_formats,
        **limits,
    )


def build_builtin_recipe_registry() -> RecipeRegistry:
    """Register only the reviewed Phase 3 recipes."""

    registry = RecipeRegistry()
    report_components = [
        RecipeComponent(component_id="report.section", block_kinds=["section"], supported_formats=["docx", "pdf"]),
        RecipeComponent(component_id="report.paragraph", block_kinds=["paragraph"], supported_formats=["docx", "pdf"]),
        RecipeComponent(component_id="report.list", block_kinds=["list"], supported_formats=["docx", "pdf"]),
        RecipeComponent(component_id="report.table", block_kinds=["table"], supported_formats=["docx", "pdf"]),
        RecipeComponent(component_id="report.cross_reference", block_kinds=["cross_reference"], supported_formats=["docx", "pdf"]),
    ]
    registry.register(SystemRecipe(
        recipe_id="generic-report",
        version="1",
        supported_document_types=["generic", "generic_report", "report"],
        supported_formats=["docx", "pdf"],
        constraints_profile_id="generic-report",
        constraints_profile_version="1",
        components=report_components,
    ))
    table_components = [
        RecipeComponent(component_id="table.title", block_kinds=["section"], supported_formats=["xlsx"]),
        RecipeComponent(component_id="table.paragraph", block_kinds=["paragraph"], supported_formats=["xlsx"]),
        RecipeComponent(component_id="table.list", block_kinds=["list"], supported_formats=["xlsx"]),
        RecipeComponent(component_id="table.data", block_kinds=["table"], supported_formats=["xlsx"]),
    ]
    registry.register(SystemRecipe(
        recipe_id="structured-table",
        version="1",
        supported_document_types=["structured_table", "generic", "icd"],
        supported_formats=["xlsx"],
        constraints_profile_id="structured-table",
        constraints_profile_version="1",
        components=table_components,
    ))
    return registry


def _unique(values: list[str], label: str) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError(f"{label} entries must be non-empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} entries must be unique")
    return normalized


def _block_size(block: Any) -> tuple[int, int, int]:
    if block.kind == "table":
        text = sum(len(str(value)) for row in block.rows for value in row.cells.values())
        return text, len(block.rows), len(block.columns)
    if block.kind == "list":
        return sum(len(str(value)) for value in block.items), 0, 0
    if block.kind == "section":
        return len(str(block.title or "")) + len(str(block.content or "")), 0, 0
    if block.kind == "paragraph":
        return len(block.content), 0, 0
    if block.kind == "cross_reference":
        return sum(len(str(value)) for value in block.references), 0, 0
    return 0, 0, 0


__all__ = [
    "RecipeComponent",
    "RecipeRegistry",
    "StructureBindingCompiler",
    "StructureBindingSet",
    "StructureConstraintsProfile",
    "StructureRenderBinding",
    "SystemRecipe",
    "build_builtin_recipe_registry",
]
