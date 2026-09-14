from __future__ import annotations

from tests.test_document_shadow_planner import (
    _analysis,
    _bindings,
    _output_spec,
    _schema,
)
from src.document_authoring.planning.diff import diff_document_plans
from src.document_authoring.planning.registry import build_builtin_registries
from src.document_authoring.planning.service import LegacyTemplatePlanningAdapter
from src.document_authoring.planning.models import OutputSpec


def _compile(**changes):
    output = _output_spec()
    if changes.get("purpose"):
        output = OutputSpec(**{
            **{key: value for key, value in output.model_dump(mode="json").items() if key != "content_hash"},
            "purpose": changes["purpose"],
        })
    if changes.get("required_columns"):
        payload = {
            key: value for key, value in output.model_dump(mode="json").items()
            if key != "content_hash"
        }
        payload["table_requirements"][0]["required_columns"] = changes["required_columns"]
        output = OutputSpec(**payload)
    return LegacyTemplatePlanningAdapter(registries=build_builtin_registries()).compile(
        output_spec=output,
        document_schema=_schema(),
        template_analysis=_analysis(),
        bindings=_bindings(),
        source_snapshot_id=changes.get("source_snapshot_id", "snapshot-1"),
        source_snapshot_hash=changes.get("source_snapshot_hash", "snapshot-hash"),
    )


def test_plan_diff_reports_semantic_changes_in_stable_order():
    parent = _compile()
    child = _compile(purpose="a new purpose", required_columns=["signal", "connector"])
    diff = diff_document_plans(parent, child)
    assert diff.parent_plan_id == parent.document_plan_id
    assert diff.child_plan_version == child.version
    assert "purpose" in diff.changed_spec_fields
    assert diff.changed_coverage == ["pins"]
    assert diff.changed_unit_ids == []


def test_plan_diff_reports_layout_and_source_changes_without_source_content():
    parent = _compile()
    child = _compile(source_snapshot_id="snapshot-2", source_snapshot_hash="snapshot-hash-2")
    diff = diff_document_plans(parent, child)
    assert diff.changed_source_versions == ["snapshot-1→snapshot-2"]
    assert diff.changed_layout == []
