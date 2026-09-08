"""Strict, versioned contracts used by the document planning boundary.

These models describe *what* should be generated and the bounded execution
plan, not evidence text or renderer implementation details.  They are kept
separate from the legacy document-authoring models so old Work Orders can be
read without inventing a plan for them.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.document_authoring.harness.idempotency import canonical_json


_HASH_EXCLUDED_FIELDS = frozenset({
    "accepted_at",
    "confirmed_at",
    "created_at",
    "plan_hash",
    "stale_at",
    "status",
    "updated_at",
})
_FORBIDDEN_MAPPING_KEYS = frozenset({
    "api_key",
    "credential",
    "credentials",
    "content",
    "evidence",
    "evidence_content",
    "file",
    "password",
    "path",
    "prompt",
    "raw_content",
    "secret",
    "storage_ref",
    "template_bytes",
    "token",
})


NonEmptyId = Annotated[str, Field(min_length=1, max_length=256)]
BoundedText = Annotated[str, Field(max_length=8_000)]


class PlanningModel(BaseModel):
    """Common strict configuration for all planning payloads."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


def _unique_strings(values: list[str], *, label: str) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError(f"{label} entries must be non-empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} entries must be unique")
    return normalized


def _reject_forbidden_mappings(value: Any, *, path: str = "payload") -> None:
    """Reject accidental persistence of raw evidence, paths or credentials."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text.casefold() in _FORBIDDEN_MAPPING_KEYS:
                raise ValueError(f"{path} contains forbidden key: {key_text}")
            _reject_forbidden_mappings(child, path=f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden_mappings(child, path=f"{path}[{index}]")


def _model_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _strip_hash_excluded(value: Any, excluded: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_hash_excluded(child, excluded)
            for key, child in value.items()
            if str(key) not in excluded
        }
    if isinstance(value, (list, tuple)):
        return [_strip_hash_excluded(child, excluded) for child in value]
    return value


def planning_content_hash(
    model: Any,
    exclude: set[str] | frozenset[str] | None = None,
) -> str:
    """Return a stable SHA-256 hash for a planning value.

    Lifecycle fields are excluded recursively by default.  Semantic lists
    retain their order; mappings are normalized by :func:`canonical_json`.
    ``canonical_json`` also rejects NaN/Infinity and non-JSON values.
    """

    excluded = _HASH_EXCLUDED_FIELDS | frozenset(exclude or ())
    normalized = _strip_hash_excluded(_model_json(model), excluded)
    encoded = canonical_json(normalized).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class DeliverableSpec(PlanningModel):
    format: Literal["docx", "pdf", "xlsx", "xlsm", "pptx", "markdown"]
    role: Literal["primary", "derivative"]
    required: bool = True
    requested_by: Literal["user", "system"] = "user"


class ArtifactSpec(PlanningModel):
    deliverables: list[DeliverableSpec] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_deliverables(self) -> "ArtifactSpec":
        primary = [item for item in self.deliverables if item.role == "primary"]
        if len(primary) != 1:
            raise ValueError("artifact requires exactly one primary deliverable")
        if not primary[0].required:
            raise ValueError("primary deliverable must be required")
        if not any(item.required for item in self.deliverables):
            raise ValueError("artifact requires at least one required deliverable")
        return self


class ProvidedTemplateSource(PlanningModel):
    mode: Literal["provided_template"] = "provided_template"
    template_version_id: NonEmptyId
    template_schema_id: NonEmptyId
    template_schema_version: NonEmptyId


class SystemRecipeSource(PlanningModel):
    mode: Literal["system_recipe"] = "system_recipe"
    recipe_id: NonEmptyId
    recipe_version: NonEmptyId


class GeneratedStructureSource(PlanningModel):
    mode: Literal["generated_structure"] = "generated_structure"
    constraints_profile_id: NonEmptyId
    constraints_profile_version: NonEmptyId


LayoutSource: TypeAlias = Annotated[
    ProvidedTemplateSource | SystemRecipeSource | GeneratedStructureSource,
    Field(discriminator="mode"),
]


class OutlineUnitSpec(PlanningModel):
    unit_id: NonEmptyId
    kind: Literal["section", "paragraph", "table", "list", "field", "review_item"]
    title: Annotated[str, Field(default="", max_length=512)]
    required: bool = True


class TableRequirement(PlanningModel):
    unit_id: NonEmptyId
    row_scope: Annotated[str, Field(min_length=1, max_length=512)]
    required_columns: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        min_length=1,
        max_length=128,
    )
    row_keys: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list,
        max_length=100_000,
    )

    @model_validator(mode="after")
    def validate_table_contract(self) -> "TableRequirement":
        self.required_columns = _unique_strings(self.required_columns, label="required_columns")
        self.row_keys = _unique_strings(self.row_keys, label="row_keys")
        return self


class SourceScopeSpec(PlanningModel):
    knowledge_bases: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    attachments: list[NonEmptyId] = Field(default_factory=list, max_length=10_000)
    projects: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    structured_inputs: list[NonEmptyId] = Field(default_factory=list, max_length=10_000)
    user_assertion_hashes: list[NonEmptyId] = Field(default_factory=list, max_length=10_000)
    version_policy: Literal["current_published", "latest", "explicit", "frozen"] = "current_published"

    @model_validator(mode="after")
    def normalize_scope(self) -> "SourceScopeSpec":
        for field_name in (
            "knowledge_bases",
            "attachments",
            "projects",
            "structured_inputs",
            "user_assertion_hashes",
        ):
            setattr(self, field_name, _unique_strings(getattr(self, field_name), label=field_name))
        return self


class OutputSpec(PlanningModel):
    output_spec_id: NonEmptyId
    version: int = Field(ge=1)
    status: Literal["draft", "proposed", "accepted", "stale", "blocked"] = "draft"
    purpose: Annotated[str, Field(min_length=1, max_length=8_000)]
    audience: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list,
        max_length=64,
    )
    document_type: Annotated[str, Field(min_length=1, max_length=256)]
    target_identity: dict[NonEmptyId, BoundedText] = Field(default_factory=dict, max_length=32)
    artifact: ArtifactSpec
    layout_source: LayoutSource
    outline: list[OutlineUnitSpec] = Field(min_length=1, max_length=10_000)
    table_requirements: list[TableRequirement] = Field(default_factory=list, max_length=10_000)
    source_scope: SourceScopeSpec = Field(default_factory=SourceScopeSpec)
    language: Annotated[str, Field(min_length=2, max_length=32)] = "en-US"
    style: dict[str, BoundedText] = Field(default_factory=dict, max_length=64)
    missing_data_policy: Literal["mark_tbd", "keep_blank", "block_generation"] = "mark_tbd"
    inference_policy: Literal["forbid", "allow_labeled", "allow_limited"] = "forbid"
    approval_policy_id: NonEmptyId
    accepted_recommendations: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    confirmed_by: NonEmptyId | None = None
    confirmed_at: datetime | None = None
    content_hash: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_deliverables_alias(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or "deliverables" not in data:
            return data
        if "artifact" in data:
            raise ValueError("provide artifact.deliverables or deliverables, not both")
        normalized = dict(data)
        normalized["artifact"] = {"deliverables": normalized.pop("deliverables")}
        return normalized

    @model_validator(mode="after")
    def validate_output_spec(self) -> "OutputSpec":
        self.audience = _unique_strings(self.audience, label="audience") if self.audience else []
        self.accepted_recommendations = _unique_strings(
            self.accepted_recommendations,
            label="accepted_recommendations",
        ) if self.accepted_recommendations else []
        unit_ids = [unit.unit_id for unit in self.outline]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("outline unit IDs must be unique")
        table_units = {unit.unit_id for unit in self.outline if unit.kind == "table"}
        unknown_tables = [item.unit_id for item in self.table_requirements if item.unit_id not in table_units]
        if unknown_tables:
            raise ValueError(f"table requirements reference unknown/non-table units: {unknown_tables}")
        _reject_forbidden_mappings(self.target_identity, path="target_identity")
        _reject_forbidden_mappings(self.style, path="style")
        expected_hash = planning_content_hash(self, exclude={"content_hash"})
        if self.content_hash is not None and self.content_hash != expected_hash:
            raise ValueError("content_hash does not match the canonical OutputSpec content")
        object.__setattr__(self, "content_hash", expected_hash)
        return self


class TemplateContract(PlanningModel):
    kind: Literal["template"] = "template"
    template_version_id: NonEmptyId
    template_schema_id: NonEmptyId
    template_schema_version: NonEmptyId
    bindings: dict[NonEmptyId, list[NonEmptyId]] = Field(default_factory=dict, max_length=10_000)
    table_schemas: dict[NonEmptyId, dict[str, Any]] = Field(default_factory=dict, max_length=10_000)

    @model_validator(mode="after")
    def validate_bindings(self) -> "TemplateContract":
        for unit_id, regions in self.bindings.items():
            self.bindings[unit_id] = _unique_strings(regions, label=f"bindings[{unit_id}]")
        _reject_forbidden_mappings(self.bindings, path="bindings")
        _reject_forbidden_mappings(self.table_schemas, path="table_schemas")
        return self


class StructureContract(PlanningModel):
    kind: Literal["structure"] = "structure"
    structure_profile_id: NonEmptyId
    structure_profile_version: NonEmptyId
    components: list[NonEmptyId] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_components(self) -> "StructureContract":
        self.components = _unique_strings(self.components, label="components")
        return self


LayoutContract: TypeAlias = Annotated[
    TemplateContract | StructureContract,
    Field(discriminator="kind"),
]


class CoverageRequirement(PlanningModel):
    requirement_id: NonEmptyId
    unit_id: NonEmptyId
    kind: Literal["section", "scalar", "paragraph", "table", "cross_unit", "artifact"]
    required: bool = True
    row_keys: list[NonEmptyId] = Field(default_factory=list, max_length=100_000)
    required_columns: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    min_evidence_items: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def validate_coverage_shape(self) -> "CoverageRequirement":
        self.row_keys = _unique_strings(self.row_keys, label="row_keys")
        self.required_columns = _unique_strings(self.required_columns, label="required_columns")
        if self.kind == "table" and self.required and not self.required_columns:
            raise ValueError("required table coverage needs required_columns")
        if self.kind != "table" and (self.row_keys or self.required_columns):
            raise ValueError("row_keys and required_columns are only valid for table coverage")
        return self


class CoverageContract(PlanningModel):
    requirements: list[CoverageRequirement] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def validate_requirements(self) -> "CoverageContract":
        keys = [(item.unit_id, item.kind) for item in self.requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("coverage requirements must be unique per unit and kind")
        return self


class SemanticUnitPlan(PlanningModel):
    unit_id: NonEmptyId
    kind: Literal["section", "paragraph", "table", "list", "field", "review_item"]
    required: bool = True
    output_schema: dict[str, Any] = Field(default_factory=dict, max_length=256)
    source_capabilities: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    reviewer_id: NonEmptyId | None = None
    reviewer_policy_id: NonEmptyId | None = None

    @model_validator(mode="after")
    def validate_unit(self) -> "SemanticUnitPlan":
        self.source_capabilities = _unique_strings(self.source_capabilities, label="source_capabilities")
        _reject_forbidden_mappings(self.output_schema, path="output_schema")
        return self


class UnitTaskSpec(PlanningModel):
    task_id: NonEmptyId
    unit_id: NonEmptyId
    plan_version: int = Field(ge=1)
    dependencies: list[NonEmptyId] = Field(default_factory=list, max_length=10_000)
    barrier: NonEmptyId | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict, max_length=256)
    output_schema: dict[str, Any] = Field(default_factory=dict, max_length=256)
    row_scope: str | None = Field(default=None, max_length=512)
    allowed_sources: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    allowed_retrievers: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    allowed_tools: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    budget: dict[str, int] = Field(default_factory=dict, max_length=32)
    max_attempts: int = Field(default=3, ge=1, le=20)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    action_key: NonEmptyId

    @model_validator(mode="after")
    def validate_task(self) -> "UnitTaskSpec":
        for field_name in ("dependencies", "allowed_sources", "allowed_retrievers", "allowed_tools"):
            values = getattr(self, field_name)
            setattr(self, field_name, _unique_strings(values, label=field_name))
        for field_name in ("input_schema", "output_schema", "budget"):
            _reject_forbidden_mappings(getattr(self, field_name), path=field_name)
        if any(value < 0 for value in self.budget.values()):
            raise ValueError("task budget values must be non-negative")
        return self


class DependencyEdge(PlanningModel):
    upstream_task_id: NonEmptyId
    downstream_task_id: NonEmptyId


class PlanIssue(PlanningModel):
    code: NonEmptyId
    severity: Literal["info", "warning", "error", "critical"] = "warning"
    message: Annotated[str, Field(min_length=1, max_length=4_000)]
    path: str | None = Field(default=None, max_length=1_000)
    blocking: bool | None = None

    @model_validator(mode="after")
    def derive_blocking(self) -> "PlanIssue":
        if self.blocking is None:
            object.__setattr__(self, "blocking", self.severity in {"error", "critical"})
        return self


class DocumentPlan(PlanningModel):
    document_plan_id: NonEmptyId
    version: int = Field(ge=1)
    status: Literal["proposed", "accepted", "stale", "blocked"] = "proposed"
    output_spec_id: NonEmptyId
    output_spec_version: int = Field(ge=1)
    output_spec_hash: NonEmptyId
    output_spec_summary: dict[str, Any] = Field(default_factory=dict, max_length=256)
    source_snapshot_id: NonEmptyId
    source_snapshot_hash: NonEmptyId
    domain_strategy_id: NonEmptyId
    domain_strategy_version: NonEmptyId
    layout_adapter_id: NonEmptyId
    layout_adapter_version: NonEmptyId
    renderer_capability_id: NonEmptyId
    renderer_capability_version: NonEmptyId
    layout_contract: LayoutContract
    semantic_units: list[SemanticUnitPlan] = Field(min_length=1, max_length=100_000)
    dependency_edges: list[DependencyEdge] = Field(default_factory=list, max_length=100_000)
    coverage_contract: CoverageContract
    unit_tasks: list[UnitTaskSpec] = Field(min_length=1, max_length=100_000)
    required_capabilities: list[NonEmptyId] = Field(default_factory=list, max_length=256)
    resolved_capabilities: list[NonEmptyId] = Field(default_factory=list, max_length=256)
    issues: list[PlanIssue] = Field(default_factory=list, max_length=10_000)
    retrieval_specs: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    unit_review_policy: dict[str, Any] = Field(default_factory=dict, max_length=256)
    document_review_policy: dict[str, Any] = Field(default_factory=dict, max_length=256)
    render_spec: dict[str, Any] = Field(default_factory=dict, max_length=256)
    approval_policy: dict[str, Any] = Field(default_factory=dict, max_length=256)
    plan_hash: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_layout_kind(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        layout = data.get("layout_contract")
        if not isinstance(layout, Mapping) or "kind" in layout or "mode" not in layout:
            return data
        normalized = dict(data)
        normalized_layout = dict(layout)
        mode = normalized_layout.pop("mode")
        normalized_layout["kind"] = "template" if mode == "provided_template" else "structure"
        if normalized_layout["kind"] == "structure":
            if "recipe_id" in normalized_layout:
                normalized_layout["structure_profile_id"] = normalized_layout.pop("recipe_id")
            if "recipe_version" in normalized_layout:
                normalized_layout["structure_profile_version"] = normalized_layout.pop("recipe_version")
        normalized["layout_contract"] = normalized_layout
        return normalized

    @model_validator(mode="after")
    def validate_plan(self) -> "DocumentPlan":
        semantic_ids = [unit.unit_id for unit in self.semantic_units]
        if len(semantic_ids) != len(set(semantic_ids)):
            raise ValueError("semantic unit IDs must be unique")
        task_ids = [task.task_id for task in self.unit_tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("unit task IDs must be unique")
        task_units = [task.unit_id for task in self.unit_tasks]
        if len(task_units) != len(set(task_units)):
            raise ValueError("each semantic unit may have only one unit task")
        unknown_task_units = sorted(set(task_units) - set(semantic_ids))
        if unknown_task_units:
            raise ValueError(f"unit tasks reference unknown units: {unknown_task_units}")
        missing_task_units = sorted(set(semantic_ids) - set(task_units))
        if missing_task_units:
            raise ValueError(f"semantic units without tasks: {missing_task_units}")
        task_id_set = set(task_ids)
        for task in self.unit_tasks:
            dangling = sorted(set(task.dependencies) - task_id_set)
            if dangling:
                raise ValueError(f"dependency references unknown task IDs: {dangling}")
            if task.plan_version != self.version:
                raise ValueError("unit task plan_version must match DocumentPlan.version")
        for edge in self.dependency_edges:
            if edge.upstream_task_id == edge.downstream_task_id:
                raise ValueError("dependency edges may not contain self dependencies")
            if edge.upstream_task_id not in task_id_set or edge.downstream_task_id not in task_id_set:
                raise ValueError("dependency edge references an unknown task")
        self.required_capabilities = _unique_strings(self.required_capabilities, label="required_capabilities")
        self.resolved_capabilities = _unique_strings(self.resolved_capabilities, label="resolved_capabilities")
        for field_name in (
            "output_spec_summary",
            "retrieval_specs",
            "unit_review_policy",
            "document_review_policy",
            "render_spec",
            "approval_policy",
        ):
            _reject_forbidden_mappings(getattr(self, field_name), path=field_name)
        expected_hash = planning_content_hash(self, exclude={"plan_hash"})
        if self.plan_hash is not None and self.plan_hash != expected_hash:
            raise ValueError("plan_hash does not match the canonical DocumentPlan content")
        object.__setattr__(self, "plan_hash", expected_hash)
        return self

    @property
    def unresolved_capabilities(self) -> list[str]:
        return sorted(set(self.required_capabilities) - set(self.resolved_capabilities))

    @property
    def has_blocking_issues(self) -> bool:
        return any(bool(issue.blocking) for issue in self.issues)

    @property
    def is_executable(self) -> bool:
        return not self.has_blocking_issues and not self.unresolved_capabilities


class PlanDiff(PlanningModel):
    parent_plan_id: NonEmptyId
    parent_plan_version: int = Field(ge=1)
    child_plan_id: NonEmptyId
    child_plan_version: int = Field(ge=1)
    changed_spec_fields: list[NonEmptyId] = Field(default_factory=list, max_length=10_000)
    added_unit_ids: list[NonEmptyId] = Field(default_factory=list, max_length=100_000)
    removed_unit_ids: list[NonEmptyId] = Field(default_factory=list, max_length=100_000)
    changed_unit_ids: list[NonEmptyId] = Field(default_factory=list, max_length=100_000)
    changed_coverage: list[NonEmptyId] = Field(default_factory=list, max_length=100_000)
    changed_layout: list[NonEmptyId] = Field(default_factory=list, max_length=1_000)
    changed_source_versions: list[NonEmptyId] = Field(default_factory=list, max_length=1_000)
    changed_policy_versions: list[NonEmptyId] = Field(default_factory=list, max_length=1_000)

    @model_validator(mode="after")
    def normalize_diff(self) -> "PlanDiff":
        for field_name in (
            "changed_spec_fields",
            "added_unit_ids",
            "removed_unit_ids",
            "changed_unit_ids",
            "changed_coverage",
            "changed_layout",
            "changed_source_versions",
            "changed_policy_versions",
        ):
            values = _unique_strings(getattr(self, field_name), label=field_name)
            setattr(self, field_name, sorted(values))
        return self
