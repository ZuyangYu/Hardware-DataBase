"""Deterministic semantic diffs between document plan versions."""

from __future__ import annotations

from typing import Any

from .models import AffectedSubgraph, DocumentPlan, PlanDiff, planning_content_hash


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
    if (
        planning_content_hash(parent.layout_contract) != planning_content_hash(child.layout_contract)
        or planning_content_hash(parent.render_spec) != planning_content_hash(child.render_spec)
    ):
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
    for label, left, right in (
        ("approval_policy", parent.approval_policy, child.approval_policy),
        ("unit_review_policy", parent.unit_review_policy, child.unit_review_policy),
        ("document_review_policy", parent.document_review_policy, child.document_review_policy),
    ):
        if planning_content_hash(left) != planning_content_hash(right):
            changed_policy_versions.append(label)
    parent_task_units = {task.task_id: task.unit_id for task in parent.unit_tasks}
    child_task_units = {task.task_id: task.unit_id for task in child.unit_tasks}
    parent_dependencies = {
        task.task_id: sorted(task.dependencies)
        for task in parent.unit_tasks
    }
    child_dependencies = {
        task.task_id: sorted(task.dependencies)
        for task in child.unit_tasks
    }
    dependency_changed_units = sorted(
        child_task_units[task_id]
        for task_id in child_task_units.keys() & parent_task_units.keys()
        if parent_dependencies.get(task_id, []) != child_dependencies.get(task_id, [])
    )
    parent_edges = {
        (edge.upstream_task_id, edge.downstream_task_id)
        for edge in parent.dependency_edges
    }
    child_edges_declared = {
        (edge.upstream_task_id, edge.downstream_task_id)
        for edge in child.dependency_edges
    }
    edge_changed_units = sorted({
        child_task_units[downstream]
        for _upstream, downstream in (child_edges_declared ^ parent_edges)
        if downstream in child_task_units
    })
    dependency_changed_units = sorted(set(dependency_changed_units) | set(edge_changed_units))
    changed_units = sorted(set(changed_units) | set(dependency_changed_units))

    directly_affected = set(changed_units) | set(changed_coverage) | set(
        set(child_units) - set(parent_units)
    )
    reason_codes: set[str] = set()
    if changed_spec_fields:
        reason_codes.add("spec_change")
        directly_affected.update(child_units)
    if changed_layout:
        reason_codes.add("layout_change")
        directly_affected.update(child_units)
    if changed_source_versions:
        reason_codes.add("source_change")
        directly_affected.update(child_units)
    if changed_policy_versions:
        reason_codes.add("policy_change")
        directly_affected.update(child_units)
    if dependency_changed_units:
        reason_codes.add("dependency_change")
    if changed_coverage:
        reason_codes.add("coverage_change")
    if changed_units:
        reason_codes.add("unit_change")
    if set(child_units) - set(parent_units):
        reason_codes.add("unit_added")
    if set(parent_units) - set(child_units):
        reason_codes.add("unit_removed")

    child_edges = {
        (edge.upstream_task_id, edge.downstream_task_id)
        for edge in child.dependency_edges
    }
    for task in child.unit_tasks:
        for dependency in task.dependencies:
            child_edges.add((dependency, task.task_id))
    task_by_unit = {task.unit_id: task.task_id for task in child.unit_tasks}
    affected_task_ids = {
        task_by_unit[unit_id]
        for unit_id in directly_affected
        if unit_id in task_by_unit
    }
    changed = True
    while changed:
        changed = False
        for upstream, downstream in child_edges:
            if upstream in affected_task_ids and downstream not in affected_task_ids:
                affected_task_ids.add(downstream)
                changed = True
    affected_unit_ids = {
        child_task_units[task_id]
        for task_id in affected_task_ids
        if task_id in child_task_units
    }
    affected_unit_ids.update(
        unit_id for unit_id in directly_affected if unit_id in child_units
    )
    common_units = set(parent_units) & set(child_units)
    reused_unit_ids = common_units - affected_unit_ids
    if not affected_task_ids and affected_unit_ids:
        # A semantic unit without a corresponding child task is a malformed
        # scope and must not be silently considered reusable.
        reason_codes.add("unbound_unit")
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
        directly_affected_unit_ids=sorted(directly_affected),
        affected_unit_ids=sorted(affected_unit_ids),
        affected_task_ids=sorted(affected_task_ids),
        dependency_closure=sorted(affected_task_ids),
        reused_unit_ids=sorted(reused_unit_ids),
        scope_reason_codes=sorted(reason_codes),
    )


def compile_affected_subgraph(
    parent: DocumentPlan,
    child: DocumentPlan,
    diff: PlanDiff | None = None,
) -> AffectedSubgraph:
    """Compile the exact child-task closure allowed for a revision."""

    candidate_diff = diff or diff_document_plans(parent, child)
    return AffectedSubgraph(
        child_plan_id=child.document_plan_id,
        child_plan_version=child.version,
        directly_affected_unit_ids=candidate_diff.directly_affected_unit_ids,
        affected_unit_ids=candidate_diff.affected_unit_ids,
        affected_task_ids=candidate_diff.affected_task_ids,
        reused_unit_ids=candidate_diff.reused_unit_ids,
        dependency_closure=candidate_diff.dependency_closure,
        reason_codes=candidate_diff.scope_reason_codes,
    )


__all__ = ["compile_affected_subgraph", "diff_document_plans"]
