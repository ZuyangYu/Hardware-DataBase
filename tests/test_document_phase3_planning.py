from __future__ import annotations

import pytest

from src.document_authoring.planning.intake import OutputSpecIntakeService
from src.document_authoring.planning.models import OutputSpec
from src.document_authoring.planning.recipes import build_builtin_recipe_registry
from src.document_authoring.planning.service import TemplateFreePlanningAdapter


def _spec(*, mode: str = "system_recipe", fmt: str = "docx") -> OutputSpec:
    intake = OutputSpecIntakeService()
    if mode == "system_recipe":
        layout = {"mode": mode, "recipe_id": "generic-report", "recipe_version": "1"}
    else:
        layout = {
            "mode": mode,
            "constraints_profile_id": "generic-report",
            "constraints_profile_version": "1",
        }
    draft = intake.start_draft(
        output_spec_id="spec:phase3",
        version=1,
        purpose="生成受控无模板报告",
        document_type="generic_report",
        layout_source=layout,
        outline=[
            {"unit_id": "overview", "kind": "section", "title": "Overview", "required": True},
            {"unit_id": "summary", "kind": "paragraph", "required": True},
            {"unit_id": "signals", "kind": "table", "title": "Signals", "required": True},
        ],
        table_requirements=[{
            "unit_id": "signals",
            "row_scope": "selected signals",
            "required_columns": ["signal", "value"],
            "row_keys": ["J1:1", "J1:2"],
            "row_order": "declared",
        }],
        deliverables=[{"format": fmt, "role": "primary", "required": True}],
        missing_data_policy="mark_tbd",
        inference_policy="forbid",
        approval_policy_id="default-document-v1",
    )
    return intake.to_output_spec(draft).model_copy(update={"status": "proposed"})


def test_template_free_planner_compiles_an_executable_system_recipe_plan():
    spec = _spec()
    plan = TemplateFreePlanningAdapter(
        recipe_registry=build_builtin_recipe_registry(),
    ).compile(
        output_spec=spec,
        source_snapshot_id="snapshot:phase3",
        source_snapshot_hash="sha256:snapshot-phase3",
    )

    assert plan.status == "proposed"
    assert plan.layout_adapter_id == "system_recipe"
    assert plan.layout_contract.kind == "structure"
    assert plan.layout_contract.components
    assert plan.render_spec["recipe_id"] == "generic-report"
    assert plan.renderer_capability_id == "docx"
    table = next(item for item in plan.coverage_contract.requirements if item.unit_id == "signals")
    assert table.row_keys == ["J1:1", "J1:2"]
    assert not plan.has_blocking_issues


def test_template_free_planner_supports_generated_structure_only_for_known_profile():
    spec = _spec(mode="generated_structure", fmt="pdf")
    plan = TemplateFreePlanningAdapter(
        recipe_registry=build_builtin_recipe_registry(),
    ).compile(
        output_spec=spec,
        source_snapshot_id="snapshot:phase3",
        source_snapshot_hash="sha256:snapshot-phase3",
    )

    assert plan.status == "proposed"
    assert plan.layout_adapter_id == "generated_structure"
    assert plan.render_spec["recipe_id"] == "generic-report"
    assert plan.renderer_capability_id == "pdf"


def test_template_free_planner_fails_closed_for_unknown_recipe():
    spec = _spec()
    data = spec.model_dump(mode="json")
    data["layout_source"] = {
        "mode": "system_recipe",
        "recipe_id": "unknown",
        "recipe_version": "1",
    }
    data.pop("content_hash", None)
    unknown = OutputSpec.model_validate(data)

    plan = TemplateFreePlanningAdapter(
        recipe_registry=build_builtin_recipe_registry(),
    ).compile(
        output_spec=unknown,
        source_snapshot_id="snapshot:phase3",
        source_snapshot_hash="sha256:snapshot-phase3",
    )
    assert plan.status == "blocked"
    assert any(issue.code == "recipe_missing" for issue in plan.issues)
