from __future__ import annotations

import pytest

from src.document_authoring.planning.intake import OutputSpecIntakeService
from src.document_authoring.planning.models import OutputSpec
from src.document_authoring.planning import recipes as recipes_module
from src.document_authoring.planning.recipes import StructureBindingCompiler, build_builtin_recipe_registry
from src.document_authoring.planning.service import TemplateFreePlanningAdapter
from src.document_authoring.document_model import DocumentModel, ParagraphBlock, SectionBlock


def _profile_registry():
    registry_factory = getattr(recipes_module, "build_builtin_structure_profile_registry", None)
    assert registry_factory is not None, "Phase 3B structure profile registry is not implemented"
    return registry_factory()


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


def test_planner_summary_keeps_explicit_additional_requirements():
    spec = _spec().model_copy(update={
        "additional_requirements": [{
            "field": "generation_basis",
            "value": "actual_function",
            "meaning": "use_actual_function",
        }],
    })
    plan = TemplateFreePlanningAdapter(
        recipe_registry=build_builtin_recipe_registry(),
    ).compile(
        output_spec=spec,
        source_snapshot_id="snapshot:phase3-extra",
        source_snapshot_hash="sha256:snapshot-phase3-extra",
    )

    assert plan.output_spec_summary["additional_requirements"] == [{
        "field": "generation_basis",
        "value": "actual_function",
        "meaning": "use_actual_function",
    }]


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


def test_generated_structure_requires_all_server_profile_readiness_gates():
    spec = _spec(mode="generated_structure", fmt="pdf")
    profiles = _profile_registry()
    profile = profiles.lookup("generic-report", "1")
    assert profile is not None
    profile.coverage_gate_status = "failed"

    plan = TemplateFreePlanningAdapter(
        recipe_registry=build_builtin_recipe_registry(),
        profile_registry=profiles,
    ).compile(
        output_spec=spec,
        source_snapshot_id="snapshot:phase3-gates",
        source_snapshot_hash="sha256:snapshot-phase3-gates",
    )

    assert plan.status == "blocked"
    assert any(issue.code == "structure_profile_gates_incomplete" for issue in plan.issues)


def test_generated_structure_rejects_profile_document_type_mismatch():
    spec = _spec(mode="generated_structure", fmt="pdf").model_copy(
        update={"document_type": "requirements"},
    )
    plan = TemplateFreePlanningAdapter(
        recipe_registry=build_builtin_recipe_registry(),
        profile_registry=_profile_registry(),
    ).compile(
        output_spec=spec,
        source_snapshot_id="snapshot:phase3-type",
        source_snapshot_hash="sha256:snapshot-phase3-type",
    )

    assert plan.status == "blocked"
    assert any(issue.code == "structure_profile_document_type_unsupported" for issue in plan.issues)


def test_structure_binding_enforces_server_owned_generated_order_and_depth():
    spec = _spec(mode="generated_structure", fmt="pdf")
    plan = TemplateFreePlanningAdapter(
        recipe_registry=build_builtin_recipe_registry(),
        profile_registry=_profile_registry(),
    ).compile(
        output_spec=spec,
        source_snapshot_id="snapshot:phase3-order",
        source_snapshot_hash="sha256:snapshot-phase3-order",
    ).model_copy(update={"status": "accepted"})
    model = DocumentModel(
        document_id="document:phase3-order",
        plan_id=plan.document_plan_id,
        plan_version=plan.version,
        plan_hash=plan.plan_hash,
        blocks=[
            SectionBlock(unit_id="overview", block_id="block:overview", title="Overview"),
            ParagraphBlock(unit_id="summary", block_id="block:summary", content="Summary"),
        ],
    )
    compiler = StructureBindingCompiler(
        build_builtin_recipe_registry(),
        profile_registry=_profile_registry(),
    )
    model.blocks[1].layout_hints = {"level": 99}

    with pytest.raises(ValueError, match="nesting depth"):
        compiler.compile(plan, model)

    model.blocks[1].layout_hints = {}
    model.blocks.reverse()
    with pytest.raises(ValueError, match="ordering"):
        compiler.compile(plan, model)
