from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from src.document_authoring.planning.models import (
    CoverageContract,
    CoverageRequirement,
    DocumentPlan,
    OutputSpec,
    TableRequirement,
    planning_content_hash,
)


def _output_payload() -> dict:
    return {
        "output_spec_id": "spec-001",
        "version": 1,
        "status": "proposed",
        "purpose": "接口评审",
        "audience": ["hardware", "software"],
        "document_type": "icd",
        "target_identity": {"project": "ADAS", "product": "ECU", "release": "R1"},
        "artifact": {
            "deliverables": [
                {"format": "xlsx", "role": "primary", "required": True, "requested_by": "user"},
                {"format": "pdf", "role": "derivative", "required": False, "requested_by": "system"},
            ]
        },
        "layout_source": {
            "mode": "provided_template",
            "template_version_id": "template-001",
            "template_schema_id": "schema-001",
            "template_schema_version": "1",
        },
        "outline": [
            {"unit_id": "cover", "kind": "section", "title": "Cover", "required": True},
            {"unit_id": "pins", "kind": "table", "title": "Pin Definition", "required": True},
        ],
        "table_requirements": [
            {
                "unit_id": "pins",
                "row_scope": "all_selected_pins",
                "required_columns": ["connector", "pin", "signal"],
                "row_keys": ["J1:1", "J1:2"],
            }
        ],
        "source_scope": {
            "knowledge_bases": ["ADAS"],
            "attachments": ["attachment-001"],
            "projects": ["project-001"],
            "user_assertion_hashes": ["sha256:assertion"],
            "version_policy": "current_published",
        },
        "language": "zh-CN",
        "style": {"tone": "engineering", "detail": "review_ready"},
        "missing_data_policy": "mark_tbd",
        "inference_policy": "forbid",
        "approval_policy_id": "formal-engineering-v1",
        "accepted_recommendations": ["recommended-language-zh"],
    }


def _plan_payload(**overrides) -> dict:
    payload = {
        "document_plan_id": "plan-001",
        "version": 1,
        "status": "proposed",
        "output_spec_id": "spec-001",
        "output_spec_version": 1,
        "output_spec_hash": OutputSpec(**_output_payload()).content_hash,
        "source_snapshot_id": "source-snapshot-001",
        "source_snapshot_hash": "sha256:source-001",
        "domain_strategy_id": "legacy_document_schema",
        "domain_strategy_version": "1",
        "layout_adapter_id": "provided_template",
        "layout_adapter_version": "1",
        "renderer_capability_id": "xlsx",
        "renderer_capability_version": "1",
        "layout_contract": {
            "kind": "template",
            "template_version_id": "template-001",
            "template_schema_id": "schema-001",
            "template_schema_version": "1",
            "bindings": {"cover": ["A1"], "pins": ["A10"]},
        },
        "semantic_units": [
            {
                "unit_id": "cover",
                "kind": "section",
                "required": True,
                "output_schema": {"type": "object"},
                "source_capabilities": ["kb.read@1"],
                "reviewer_id": "deterministic@1",
            },
            {
                "unit_id": "pins",
                "kind": "table",
                "required": True,
                "output_schema": {"type": "table", "columns": ["connector", "pin", "signal"]},
                "source_capabilities": ["kb.read@1"],
                "reviewer_id": "table@1",
            },
        ],
        "dependency_edges": [],
        "coverage_contract": {
            "requirements": [
                {"requirement_id": "cover", "unit_id": "cover", "kind": "section", "required": True},
                {
                    "requirement_id": "pins",
                    "unit_id": "pins",
                    "kind": "table",
                    "required": True,
                    "row_keys": ["J1:1", "J1:2"],
                    "required_columns": ["connector", "pin", "signal"],
                },
            ]
        },
        "unit_tasks": [
            {
                "task_id": "task-cover",
                "unit_id": "cover",
                "plan_version": 1,
                "dependencies": [],
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "allowed_sources": ["kb.read@1"],
                "allowed_retrievers": ["default@1"],
                "allowed_tools": ["retrieve"],
                "max_attempts": 2,
                "timeout_seconds": 60,
                "action_key": "document-plan:plan-001:cover",
            },
            {
                "task_id": "task-pins",
                "unit_id": "pins",
                "plan_version": 1,
                "dependencies": [],
                "input_schema": {"type": "table"},
                "output_schema": {"type": "table"},
                "row_scope": "all_selected_pins",
                "allowed_sources": ["kb.read@1"],
                "allowed_retrievers": ["default@1"],
                "allowed_tools": ["retrieve"],
                "max_attempts": 2,
                "timeout_seconds": 60,
                "action_key": "document-plan:plan-001:pins",
            },
        ],
        "required_capabilities": ["kb.read@1", "xlsx@1"],
        "resolved_capabilities": ["kb.read@1", "xlsx@1"],
        "issues": [],
        "retrieval_specs": [{"retriever_id": "default@1", "scope": "source-snapshot-001"}],
        "unit_review_policy": {"policy_id": "unit-default@1"},
        "document_review_policy": {"policy_id": "document-default@1"},
        "render_spec": {"format": "xlsx"},
        "approval_policy": {"policy_id": "formal-engineering-v1"},
    }
    payload.update(overrides)
    return payload


def test_valid_output_spec_and_plan_round_trip_with_stable_hashes():
    spec = OutputSpec(**_output_payload())
    plan = DocumentPlan(**_plan_payload())

    assert spec.content_hash.startswith("sha256:")
    assert plan.plan_hash.startswith("sha256:")
    assert plan.is_executable is True
    assert OutputSpec.model_validate(spec.model_dump(mode="json")) == spec
    assert DocumentPlan.model_validate(plan.model_dump(mode="json")) == plan


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value["artifact"]["deliverables"].append(
            {"format": "docx", "role": "primary", "required": True, "requested_by": "user"}
        ),
        lambda value: value["artifact"]["deliverables"].__setitem__(0, {
            "format": "xlsx", "role": "derivative", "required": True, "requested_by": "user"
        }),
    ],
)
def test_output_spec_requires_exactly_one_primary_and_one_required_deliverable(mutator):
    payload = _output_payload()
    mutator(payload)
    with pytest.raises(ValidationError, match="primary|required"):
        OutputSpec(**payload)


def test_layout_source_is_discriminated_and_rejects_ambiguous_or_incomplete_modes():
    with pytest.raises(ValidationError):
        OutputSpec(**{**_output_payload(), "layout_source": {
            "template_version_id": "template-001",
            "template_schema_id": "schema-001",
            "template_schema_version": "1",
        }})

    with pytest.raises(ValidationError, match="template_schema"):
        OutputSpec(**{**_output_payload(), "layout_source": {
            "mode": "provided_template",
            "template_version_id": "template-001",
        }})

    with pytest.raises(ValidationError, match="template"):
        OutputSpec(**{**_output_payload(), "layout_source": {
            "mode": "generated_structure",
            "constraints_profile_id": "profile-1",
            "constraints_profile_version": "1",
            "template_version_id": "must-not-be-here",
        }})


def test_duplicate_units_and_invalid_dependency_edges_are_rejected():
    duplicate = _output_payload()
    duplicate["outline"].append({"unit_id": "cover", "kind": "paragraph", "required": False})
    with pytest.raises(ValidationError, match="unique"):
        OutputSpec(**duplicate)

    dangling = _plan_payload(dependency_edges=[{"upstream_task_id": "missing", "downstream_task_id": "task-cover"}])
    with pytest.raises(ValidationError, match="dependency"):
        DocumentPlan(**dangling)

    self_edge = _plan_payload(dependency_edges=[{"upstream_task_id": "task-cover", "downstream_task_id": "task-cover"}])
    with pytest.raises(ValidationError, match="self"):
        DocumentPlan(**self_edge)


def test_free_form_sensitive_fields_and_mismatched_caller_hash_are_rejected():
    with pytest.raises(ValidationError):
        OutputSpec(**{**_output_payload(), "source_scope": {
            **_output_payload()["source_scope"], "raw_content": "secret source text"
        }})

    with pytest.raises(ValidationError, match="content_hash"):
        OutputSpec(**{**_output_payload(), "content_hash": "sha256:caller-value"})

    with pytest.raises(ValidationError, match="plan_hash"):
        DocumentPlan(**_plan_payload(plan_hash="sha256:caller-value"))


def test_hash_is_order_independent_but_changes_for_semantic_inputs():
    payload = _output_payload()
    reordered = deepcopy(payload)
    reordered["target_identity"] = {"release": "R1", "product": "ECU", "project": "ADAS"}
    reordered["style"] = {"detail": "review_ready", "tone": "engineering"}
    assert planning_content_hash(payload) == planning_content_hash(reordered)

    for path, changed in [
        (("source_scope", "version_policy"), "explicit"),
        (("table_requirements", 0, "required_columns"), ["connector", "pin", "direction"]),
    ]:
        variant = deepcopy(payload)
        target = variant
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = changed
        assert planning_content_hash(payload) != planning_content_hash(variant)

    base_plan = DocumentPlan(**_plan_payload())
    changed_strategy = DocumentPlan(**_plan_payload(domain_strategy_version="2"))
    assert base_plan.plan_hash != changed_strategy.plan_hash


def test_plan_is_not_executable_with_blocking_issue_or_unresolved_capability():
    blocked = DocumentPlan(**_plan_payload(issues=[
        {"code": "row_scope_unresolved", "severity": "error", "message": "rows unavailable"}
    ]))
    assert blocked.is_executable is False
    assert blocked.has_blocking_issues is True

    unresolved = DocumentPlan(**_plan_payload(resolved_capabilities=["kb.read@1"]))
    assert unresolved.is_executable is False
    assert unresolved.unresolved_capabilities == ["xlsx@1"]


def test_table_requirements_and_coverage_keep_row_and_column_contracts_typed():
    requirement = TableRequirement(
        unit_id="pins",
        row_scope="all_selected_pins",
        required_columns=["connector", "pin"],
        row_keys=["J1:1"],
    )
    coverage = CoverageContract(requirements=[CoverageRequirement(
        requirement_id="pins",
        unit_id="pins",
        kind="table",
        required=True,
        row_keys=requirement.row_keys,
        required_columns=requirement.required_columns,
    )])
    assert coverage.requirements[0].required_columns == ["connector", "pin"]


def test_canonical_hash_rejects_non_json_and_non_finite_values():
    with pytest.raises(ValueError, match="NaN|Infinity"):
        planning_content_hash({"score": float("nan")})
    with pytest.raises(ValueError, match="NaN|Infinity"):
        planning_content_hash({"score": float("inf")})
    with pytest.raises(ValueError, match="JSON"):
        planning_content_hash({"value": object()})
