"""Deterministic semantic diffs between document plan versions."""

from __future__ import annotations

from typing import Any

from .models import DocumentPlan, PlanDiff, planning_content_hash


def _changed_mapping_keys(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    return sorted({*left, *right} - {
        key for key in left.keys() & right.keys() if planning_content_hash(left[key]) == planning_content_hash(right[key])
    })


def diff_document_plans(parent: DocumentPlan, child: DocumentPlan) -> PlanDiff:
    """Compare plan semantics while ignoring lifecycle status and hashes."""

    if not isinstance(parent, DocumentPlan):
        parent = DocumentPlan.model_validate(parent)
    if not isinstance(child, DocumentPlan):
        child = DocumentPlan.model_validate(child)
    parent_units = {unit.unit_id: unit.model_dump(mode="json") for unit in parent.semantic_units}
    child_units = {unit.unit_id: unit.model_dump(mode="json") for unit in child.semantic_units}
    changed_units = sorted(
        unit_id for unit_id in parent_units.keys() & child_units.keys()
        if planning_content_hash(parent_units[unit_id]) != planning_content_hash(child_units[unit_id])
    )
    parent_coverage = {
        item.unit_id: item.model_dump(mode="json") for item in parent.coverage_contract.requirements
    }
    child_coverage = {
        item.unit_id: item.model_dump(mode="json") for item in child.coverage_contract.requirements
    }
    changed_coverage = sorted(
        unit_id for unit_id in parent_coverage.keys() & child_coverage.keys()
        if planning_content_hash(parent_coverage[unit_id]) != planning_content_hash(child_coverage[unit_id])
    )
    changed_spec_fields = _changed_mapping_keys(parent.output_spec_summary, child.output_spec_summary)
    if parent.output_spec_hash != child.output_spec_hash and not changed_spec_fields:
        changed_spec_fields = ["output_spec"]
    changed_layout = []
    if planning_content_hash(parent.layout_contract) != planning_content_hash(child.layout_contract):
        changed_layout = ["layout_contract"]
    changed_source_versions = []
    if (
        parent.source_snapshot_id != child.source_snapshot_id
        or parent.source_snapshot_hash != child.source_snapshot_hash
    ):
        changed_source_versions = [f"{parent.source_snapshot_id}→{child.source_snapshot_id}"]
    changed_policy_versions: list[str] = []
    for label, left, right in (
        ("domain_strategy", f"{parent.domain_strategy_id}@{parent.domain_strategy_version}", f"{child.domain_strategy_id}@{child.domain_strategy_version}"),
        ("layout_adapter", f"{parent.layout_adapter_id}@{parent.layout_adapter_version}", f"{child.layout_adapter_id}@{child.layout_adapter_version}"),
        ("renderer", f"{parent.renderer_capability_id}@{parent.renderer_capability_version}", f"{child.renderer_capability_id}@{child.renderer_capability_version}"),
    ):
        if left != right:
            changed_policy_versions.append(f"{label}:{left}→{right}")
    return PlanDiff(
        parent_plan_id=parent.document_plan_id,
        parent_plan_version=parent.version,
        child_plan_id=child.document_plan_id,
        child_plan_version=child.version,
        changed_spec_fields=changed_spec_fields,
        added_unit_ids=sorted(set(child_units) - set(parent_units)),
        removed_unit_ids=sorted(set(parent_units) - set(child_units)),
        changed_unit_ids=changed_units,
        changed_coverage=changed_coverage,
        changed_layout=changed_layout,
        changed_source_versions=changed_source_versions,
        changed_policy_versions=sorted(changed_policy_versions),
    )


__all__ = ["diff_document_plans"]
