"""Resolve semantic blocks to server-owned template binding identifiers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.document_authoring.models import (
    DocxFill,
    DocxFillPlan,
    TemplateUnitBinding,
    WorkbookFill,
    WorkbookFillPlan,
    WorkbookRegionSchema,
    WorkbookTableFill,
    WorkbookTableRowFill,
)
from src.document_authoring.models import TemplateVersion
from src.document_authoring.planning.models import TemplateContract

from .document_model import DocumentModel, ParagraphBlock, SectionBlock, TypedTableBlock


class RenderBinding(BaseModel):
    """One semantic-to-registered-region binding.

    Physical sheet/cell/range coordinates are intentionally absent.  The
    renderer receives those only by resolving the immutable server-owned
    region/table registration referenced by ``region_id``.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    unit_id: str
    region_id: str
    row_key: str | None = None
    column_id: str | None = None
    table_region_id: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "RenderBinding":
        self.unit_id = self.unit_id.strip()
        self.region_id = self.region_id.strip()
        if not self.unit_id or not self.region_id:
            raise ValueError("render bindings require unit_id and region_id")
        if self.row_key is not None:
            self.row_key = self.row_key.strip() or None
        if self.column_id is not None:
            self.column_id = self.column_id.strip() or None
        if self.table_region_id is not None:
            self.table_region_id = self.table_region_id.strip() or None
        if self.table_region_id and not self.row_key:
            raise ValueError("table render bindings require row_key")
        if self.table_region_id and not self.column_id:
            raise ValueError("table render bindings require column_id")
        return self


class RenderBindingSet(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    template_version_id: str
    template_schema_id: str
    template_schema_version: str
    plan_id: str = ""
    plan_version: int | None = None
    plan_hash: str = ""
    bindings: list[RenderBinding] = Field(default_factory=list)
    binding_hash: str = ""

    @model_validator(mode="after")
    def normalize_and_hash(self) -> "RenderBindingSet":
        ids = [
            (binding.unit_id, binding.row_key, binding.column_id, binding.region_id)
            for binding in self.bindings
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("render bindings must be unique")
        from src.document_authoring.models import content_hash

        expected = content_hash(self.model_dump(mode="json", exclude={"binding_hash"}))
        if self.binding_hash and self.binding_hash != expected:
            raise ValueError("render binding hash does not match contents")
        self.binding_hash = expected
        return self


class RenderBindingResolver:
    """Resolve only bindings registered for the frozen template contract."""

    def resolve(
        self,
        template_contract: TemplateContract | Mapping[str, Any],
        document_model: DocumentModel | Mapping[str, Any],
        registered_bindings: Sequence[TemplateUnitBinding] | Mapping[str, TemplateUnitBinding],
        *,
        regions: Sequence[WorkbookRegionSchema] | Mapping[str, Any] | None = None,
    ) -> RenderBindingSet:
        contract = (
            template_contract if isinstance(template_contract, TemplateContract)
            else TemplateContract.model_validate(template_contract)
        )
        model = (
            document_model if isinstance(document_model, DocumentModel)
            else DocumentModel.model_validate(document_model)
        )
        binding_map = _binding_map(registered_bindings)
        region_map = _region_map(regions)
        resolved: list[RenderBinding] = []
        for block in model.blocks:
            unit_id = block.unit_id
            binding = binding_map.get(unit_id)
            expected_targets = list(contract.bindings.get(unit_id, []))
            if binding is None:
                raise PermissionError(f"no registered render binding for unit: {unit_id}")
            if (
                binding.template_schema_id != contract.template_schema_id
                or binding.template_schema_version != contract.template_schema_version
            ):
                raise PermissionError(f"render binding schema is not frozen for unit: {unit_id}")
            if expected_targets and list(binding.target_region_ids) != expected_targets:
                raise PermissionError(f"render binding is outside the frozen contract: {unit_id}")
            if isinstance(block, TypedTableBlock):
                table = binding.table_schema
                if table is None:
                    raise PermissionError(f"table unit has no registered table binding: {unit_id}")
                if binding.target_region_ids != [table.table_region_id]:
                    raise PermissionError(f"table binding does not reference its registered table region: {unit_id}")
                expected_keys = list(block.expected_row_keys)
                for row in block.rows:
                    for column in block.columns:
                        if column not in row.cells:
                            continue
                        resolved.append(RenderBinding(
                            unit_id=unit_id,
                            region_id=table.table_region_id,
                            table_region_id=table.table_region_id,
                            row_key=row.row_key,
                            column_id=column,
                        ))
                if expected_keys and [row.row_key for row in block.rows] != expected_keys:
                    raise PermissionError(f"table rows are not in the frozen row-key order: {unit_id}")
                continue
            if len(binding.target_region_ids) != 1:
                raise PermissionError(f"scalar render binding must have one target: {unit_id}")
            region_id = binding.target_region_ids[0]
            region = region_map.get(region_id)
            if region is not None:
                role = str(_value(region, "role", ""))
                write_policy = str(_value(region, "write_policy", ""))
                if write_policy not in {"deterministic_only", "validated_draft"} or role in {
                    "formula", "human_input", "human_approval", "locked_template", "legacy_example",
                }:
                    raise PermissionError(f"registered region is not renderer-writable: {region_id}")
            resolved.append(RenderBinding(unit_id=unit_id, region_id=region_id))
        return RenderBindingSet(
            template_version_id=contract.template_version_id,
            template_schema_id=contract.template_schema_id,
            template_schema_version=contract.template_schema_version,
            plan_id=model.plan_id,
            plan_version=model.plan_version,
            plan_hash=model.plan_hash,
            bindings=resolved,
        )


class LegacyFillPlanAdapter:
    """Compatibility adapter for callers that still consume ``FillPlan``."""

    @staticmethod
    def to_fill_plan(
        document_model: DocumentModel,
        *,
        template_version_id: str,
        output_format: str,
        bindings: Sequence[TemplateUnitBinding] | Mapping[str, TemplateUnitBinding],
    ) -> WorkbookFillPlan | DocxFillPlan:
        binding_map = _binding_map(bindings)
        scalar_fills: list[WorkbookFill | DocxFill] = []
        table_fills: list[WorkbookTableFill] = []
        for block in document_model.blocks:
            binding = binding_map.get(block.unit_id)
            if binding is None:
                raise PermissionError(f"legacy adapter has no binding for unit: {block.unit_id}")
            if isinstance(block, TypedTableBlock):
                if output_format == "docx":
                    raise PermissionError(
                        "DOCX legacy adapter cannot render a typed workbook table"
                    )
                if binding.table_schema is None:
                    raise PermissionError(f"legacy adapter cannot scalarize table unit: {block.unit_id}")
                table_fills.append(WorkbookTableFill(
                    table_region_id=binding.table_schema.table_region_id,
                    semantic_unit_id=block.unit_id,
                    rows=[WorkbookTableRowFill(
                        row_key=row.row_key,
                        cells=dict(row.cells),
                        evidence_ids=list(row.evidence_ids),
                        cell_evidence_ids={
                            column: list(ids) for column, ids in row.cell_evidence_ids.items()
                        },
                    ) for row in block.rows],
                ))
                continue
            content = _block_content(block)
            for region_id in binding.target_region_ids:
                if output_format == "docx":
                    scalar_fills.append(DocxFill(
                        region_id=region_id, value=content, semantic_unit_id=block.unit_id,
                    ))
                else:
                    scalar_fills.append(WorkbookFill(
                        region_id=region_id, value=content, semantic_unit_id=block.unit_id,
                    ))
        if output_format == "docx":
            return DocxFillPlan(template_version_id=template_version_id, fills=scalar_fills)
        return WorkbookFillPlan(
            template_version_id=template_version_id,
            fills=scalar_fills,
            table_fills=table_fills,
        )


def legacy_fill_plan_from_model(
    document_model: DocumentModel,
    template: TemplateVersion,
    bindings: Sequence[TemplateUnitBinding] | Mapping[str, TemplateUnitBinding],
) -> WorkbookFillPlan | DocxFillPlan:
    return LegacyFillPlanAdapter.to_fill_plan(
        document_model,
        template_version_id=template.template_version_id,
        output_format=template.format,
        bindings=bindings,
    )


def _binding_map(value: Sequence[TemplateUnitBinding] | Mapping[str, TemplateUnitBinding]) -> dict[str, TemplateUnitBinding]:
    if isinstance(value, Mapping):
        return {
            str(key): item if isinstance(item, TemplateUnitBinding)
            else TemplateUnitBinding.model_validate(item)
            for key, item in value.items()
        }
    result: dict[str, TemplateUnitBinding] = {}
    for binding in value:
        normalized = binding if isinstance(binding, TemplateUnitBinding) else TemplateUnitBinding.model_validate(binding)
        result[normalized.semantic_unit_id] = normalized
    return result


def _region_map(value: Sequence[WorkbookRegionSchema] | Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {str(_value(region, "region_id", "")): region for region in value}


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _block_content(block: Any) -> str:
    if isinstance(block, ParagraphBlock):
        return block.content
    if isinstance(block, SectionBlock):
        return block.content or block.title
    return ""
