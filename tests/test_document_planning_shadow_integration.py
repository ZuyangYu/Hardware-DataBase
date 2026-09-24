import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import src.settings as settings
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    TemplateUnitBinding,
)
from src.document_authoring.planning.service import DocumentPlanningService
from src.document_authoring.planning.store import DocumentPlanningStore
from src.document_authoring.service import DocumentGenerationService


def _schema() -> DocumentSchema:
    return DocumentSchema(
        document_schema_id="schema-1",
        version="1",
        document_type="report",
        status="approved",
        fields=[
            DocumentFieldSchema(
                field_id="field-1",
                label="Project title",
                required=True,
                retrieval_policy_id="project-title",
                verification_policy_id="default",
            )
        ],
    )


def _order() -> SimpleNamespace:
    return SimpleNamespace(
        work_order_id="wo-shadow-1",
        tenant_id="tenant-1",
        created_by="user-1",
        task_id="task-shadow-1",
        template_version_id="template-1",
        template_schema_id="template-schema-1",
        template_schema_version="1",
        document_schema_id="schema-1",
        document_schema_version="1",
        target_format="xlsx",
        generation_brief={"purpose": "Generate a report", "confirmed": True},
        input_fingerprint="sha256:legacy-fingerprint",
        status="planned",
        unit_statuses={"field-1": "planned"},
    )


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        source_set_snapshot_id="snapshot-1",
        content_hash="sha256:frozen-snapshot",
        source_names=["private-source-name"],
    )


def _service(tmp_path: Path) -> DocumentGenerationService:
    service = object.__new__(DocumentGenerationService)
    service.planning = DocumentPlanningService(
        store=DocumentPlanningStore(str(tmp_path / "planning.db"))
    )
    return service


def _run(service, order, snapshot):
    return service._run_shadow_planning(
        SimpleNamespace(user_id="user-1"),
        order,
        snapshot=snapshot,
        template=SimpleNamespace(
            template_version_id="template-1",
            template_schema_id="template-schema-1",
            template_schema_version="1",
        ),
        schema=_schema(),
        bindings=[
            TemplateUnitBinding(
                binding_id="binding-1",
                template_schema_id="template-schema-1",
                template_schema_version="1",
                semantic_unit_type="field",
                semantic_unit_id="field-1",
                target_region_ids=["region-1"],
            )
        ],
    )


def test_shadow_flag_off_is_a_noop_and_does_not_change_execution_inputs(tmp_path):
    service = _service(tmp_path)
    order = _order()
    before = copy.deepcopy(vars(order))
    with patch.object(settings, "DOCUMENT_PLANNING_SHADOW_ENABLED", False):
        assert _run(service, order, _snapshot()) is None
    assert vars(order) == before
    assert service.planning.store.list_events(order.task_id) == []
    assert service.planning.store.get_latest_output_spec(order.task_id) is None
    assert service.planning.store.get_latest_plan(order.task_id) is None


def test_shadow_flag_on_only_persists_deterministic_plan_and_is_idempotent(tmp_path):
    service = _service(tmp_path)
    order = _order()
    snapshot = _snapshot()
    before = copy.deepcopy(vars(order))
    with patch.object(settings, "DOCUMENT_PLANNING_SHADOW_ENABLED", True):
        first = _run(service, order, snapshot)
        second = _run(service, order, snapshot)

    assert first is not None
    assert second is not None
    assert first.plan_hash == second.plan_hash
    assert vars(order) == before
    assert service.planning.store.get_latest_output_spec(order.task_id) is not None
    assert service.planning.store.get_latest_plan(order.task_id).plan_hash == first.plan_hash
    events = service.planning.store.list_events(order.task_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "planning_shadow_succeeded"
    assert "private-source-name" not in str(events)
    assert "/tmp" not in str(events)
    assert "evidence" not in str(events).casefold()


def test_shadow_compiler_failure_is_fail_soft_and_emits_sanitized_failure_event(tmp_path):
    service = _service(tmp_path)
    store = service.planning.store
    service.planning = DocumentPlanningService(
        adapter=SimpleNamespace(
            compile=lambda **_: (_ for _ in ()).throw(RuntimeError(
                "private-source-name evidence text /tmp/secret"
            ))
        ),
        store=store,
    )
    order = _order()
    before = copy.deepcopy(vars(order))
    with patch.object(settings, "DOCUMENT_PLANNING_SHADOW_ENABLED", True):
        assert _run(service, order, _snapshot()) is None

    assert vars(order) == before
    assert store.get_latest_output_spec(order.task_id) is None
    assert store.get_latest_plan(order.task_id) is None
    events = store.list_events(order.task_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "planning_shadow_failed"
    serialized = str(events)
    assert "private-source-name" not in serialized
    assert "/tmp/secret" not in serialized
    assert "evidence text" not in serialized


def test_shadow_flag_on_does_not_create_execution_jobs(tmp_path):
    service = _service(tmp_path)
    order = _order()
    execution_jobs = []
    before = list(execution_jobs)
    with patch.object(settings, "DOCUMENT_PLANNING_SHADOW_ENABLED", True):
        _run(service, order, _snapshot())
    assert execution_jobs == before
