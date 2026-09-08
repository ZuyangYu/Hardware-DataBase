from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.document_authoring.planning.intake import OutputSpecIntakeService
from src.document_authoring.planning.models import DocumentPlan, OutputSpec
from src.document_authoring.planning.service import DocumentPlanningService
from src.document_authoring.planning.store import DocumentPlanningStore


def _complete_spec(*, layout_source: dict | None = None, version: int = 1) -> OutputSpec:
    intake = OutputSpecIntakeService()
    draft = intake.start_draft(
        output_spec_id="spec-proposal",
        version=version,
        purpose="生成评审报告",
        document_type="report",
        layout_source=layout_source or {
            "mode": "generated_structure",
            "constraints_profile_id": "generic-report",
            "constraints_profile_version": "1",
        },
        outline=[{"unit_id": "summary", "kind": "section", "title": "Summary", "required": True}],
        deliverables=[{"format": "markdown", "role": "primary", "required": True}],
        missing_data_policy="mark_tbd",
        inference_policy="forbid",
        approval_policy_id="default-document-v1",
    )
    return intake.to_output_spec(draft).model_copy(update={"status": "proposed"})


def test_planning_service_persists_one_proposal_on_retry(tmp_path: Path):
    store = DocumentPlanningStore(str(tmp_path / "planning.db"))
    service = DocumentPlanningService(store=store)
    spec = _complete_spec()
    plan = DocumentPlan.model_validate({
        "document_plan_id": "plan:spec-proposal",
        "version": 1,
        "output_spec_id": spec.output_spec_id,
        "output_spec_version": spec.version,
        "output_spec_hash": spec.content_hash,
        "source_snapshot_id": "snapshot-1",
        "source_snapshot_hash": "sha256:snapshot-1",
        "domain_strategy_id": "generic",
        "domain_strategy_version": "1",
        "layout_adapter_id": "generated_structure",
        "layout_adapter_version": "1",
        "renderer_capability_id": "markdown",
        "renderer_capability_version": "1",
        "layout_contract": {
            "kind": "structure",
            "structure_profile_id": "generic-report",
            "structure_profile_version": "1",
        },
        "semantic_units": [{"unit_id": "summary", "kind": "section"}],
        "coverage_contract": {"requirements": [{"requirement_id": "summary", "unit_id": "summary", "kind": "section"}]},
        "unit_tasks": [{
            "task_id": "unit-task:summary", "unit_id": "summary", "plan_version": 1,
            "action_key": "document-plan:summary:v1",
        }],
    })
    first = service.persist_proposal(
        output_spec=spec,
        plan=plan,
        tenant_id="tenant-a",
        user_id="user-a",
        task_id="task-a",
        idempotency_key="proposal-1",
    )
    replay = service.persist_proposal(
        output_spec=spec,
        plan=plan,
        tenant_id="tenant-a",
        user_id="user-a",
        task_id="task-a",
        idempotency_key="proposal-1",
    )
    assert first == replay == plan
    assert len(store.list_events("task-a")) == 1


def test_planning_service_rejects_invalid_plan_before_persistence(tmp_path: Path):
    store = DocumentPlanningStore(str(tmp_path / "planning.db"))
    service = DocumentPlanningService(store=store)
    spec = _complete_spec()
    plan = SimpleNamespace(
        model_dump=lambda **_: {"document_plan_id": "plan-x"},
        document_plan_id="plan-x",
        version=1,
    )
    with pytest.raises(ValueError, match="DocumentPlan|model"):
        service.persist_proposal(
            output_spec=spec,
            plan=plan,
            tenant_id="tenant-a",
            user_id="user-a",
            task_id="task-a",
        )
