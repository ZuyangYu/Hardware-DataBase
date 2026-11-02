"""Runtime Contract v1 tests for executable Skills."""

from __future__ import annotations

import pytest

from src.sandbox.runtime_contract import (
    RUNTIME_CONTRACT_VERSION,
    RuntimeContractError,
    parse_skill_result,
    validate_skill_event,
)
from src.sandbox.models import SandboxTaskSpec
from src.sandbox.task_store import SandboxTaskStore


def test_needs_user_input_result_is_structured_without_final_artifacts():
    result = parse_skill_result(
        {
            "runtime_contract_version": "v1",
            "result_status": "needs_user_input",
            "issues": [{"code": "missing_pin_map", "message": "Provide the pin map"}],
            "required_inputs": ["pin_map.csv"],
        }
    )

    assert result.runtime_contract_version == RUNTIME_CONTRACT_VERSION
    assert result.result_status == "needs_user_input"
    assert result.required_inputs == ("pin_map.csv",)
    assert result.artifact_ids == ()


def test_result_rejects_unknown_status_and_non_object_issues():
    with pytest.raises(RuntimeContractError, match="result_status"):
        parse_skill_result({"result_status": "waiting"})

    with pytest.raises(RuntimeContractError, match="issues"):
        parse_skill_result({"result_status": "failed", "issues": ["secret"]})


def test_step_event_requires_matching_task_and_monotonic_sequence():
    event = validate_skill_event(
        {
            "runtime_contract_version": "v1",
            "event_id": "evt-1",
            "seq": 2,
            "task_id": "task-123",
            "attempt": 1,
            "step_id": "build_spec",
            "event": "step_completed",
            "status": "ok",
            "progress": 0.5,
            "artifact_ids": ["art-1234567890abcdef"],
        },
        task_id="task-123",
        attempt=1,
        previous_seq=1,
    )

    assert event.seq == 2
    assert event.step_id == "build_spec"
    assert event.artifact_ids == ("art-1234567890abcdef",)

    with pytest.raises(RuntimeContractError, match="sequence"):
        validate_skill_event(
            {
                "runtime_contract_version": "v1",
                "event_id": "evt-2",
                "seq": 2,
                "task_id": "task-123",
                "attempt": 1,
                "step_id": "build_spec",
                "event": "step_progress",
            },
            task_id="task-123",
            attempt=1,
            previous_seq=2,
        )


def test_task_ledger_persists_contract_lineage_and_latest_runtime_event(tmp_path, monkeypatch):
    import src.settings as settings

    monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_BACKEND", "runner_api")
    monkeypatch.setattr(settings, "SANDBOX_ALLOW_DYNAMIC_INSTALL", False)
    monkeypatch.setattr(settings, "SANDBOX_DEFAULT_NETWORK", "none")
    monkeypatch.setattr(settings, "SANDBOX_AUTHORIZED_CAPABILITIES", ("sandbox.execute",))
    store = SandboxTaskStore(str(tmp_path / "tasks.db"), validate_on_create=False)
    task = store.create(
        SandboxTaskSpec(
            idempotency_key="runtime-contract-task",
            owner_id="user-1",
            department_id="dept-1",
            bundle_ref="bundle/diagram",
            bundle_digest="a" * 64,
            profile_ref="profile/dev-l1",
            policy_snapshot_hash="policy-1",
            required_capabilities=("sandbox.execute",),
            skill_ref="skill/diagram",
            skill_content_digest="b" * 64,
            skill_version="v3",
            runtime_contract_version="v1",
            parent_task_id="task-parent",
            resume_from="art-1234567890abcdef",
        )
    )

    updated = store.append_runtime_event(
        task.task_id,
        {
            "runtime_contract_version": "v1",
            "event_id": "evt-runtime-1",
            "seq": 1,
            "task_id": task.task_id,
            "attempt": 0,
            "step_id": "build_spec",
            "event": "step_progress",
            "progress": 0.25,
            "message": "spec started",
        },
    )

    assert updated.latest_step_id == "build_spec"
    assert updated.latest_event_seq == 1
    assert updated.to_dict()["skill_version"] == "v3"
    assert updated.to_dict()["parent_task_id"] == "task-parent"
    event_record = store.runtime_events(task.task_id)[0]
    assert event_record["step_id"] == "build_spec"
    assert event_record["runtime_contract_version"] == "v1"
    assert event_record["skill_content_digest"] == "b" * 64
    assert event_record["fencing_token"] == task.fencing_token
    lifecycle_event = store.events(task.task_id)[-1]
    assert lifecycle_event["metadata"]["skill_content_digest"] == "b" * 64
    assert lifecycle_event["metadata"]["runtime_contract_version"] == "v1"

    with pytest.raises(RuntimeContractError, match="sequence"):
        store.append_runtime_event(
            task.task_id,
            {
                "runtime_contract_version": "v1",
                "event_id": "evt-runtime-duplicate",
                "seq": 1,
                "task_id": task.task_id,
                "attempt": 0,
                "step_id": "build_spec",
                "event": "step_progress",
            },
        )


def test_event_ids_are_scoped_per_task(tmp_path, monkeypatch):
    import src.settings as settings

    monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_BACKEND", "runner_api")
    monkeypatch.setattr(settings, "SANDBOX_ALLOW_DYNAMIC_INSTALL", False)
    monkeypatch.setattr(settings, "SANDBOX_DEFAULT_NETWORK", "none")
    monkeypatch.setattr(settings, "SANDBOX_AUTHORIZED_CAPABILITIES", ("sandbox.execute",))
    store = SandboxTaskStore(str(tmp_path / "tasks.db"), validate_on_create=False)
    tasks = [
        store.create(
            SandboxTaskSpec(
                idempotency_key=f"same-event-{index}",
                owner_id="user-1",
                department_id="dept-1",
                bundle_ref="bundle/diagram",
                bundle_digest="a" * 64,
                profile_ref="profile/dev-l1",
                policy_snapshot_hash="policy-1",
                required_capabilities=("sandbox.execute",),
            )
        )
        for index in range(2)
    ]
    for task in tasks:
        store.append_runtime_event(
            task.task_id,
            {
                "runtime_contract_version": "v1",
                "event_id": "evt-1",
                "seq": 1,
                "task_id": task.task_id,
                "attempt": 0,
                "step_id": "step",
                "event": "step_started",
            },
        )
    assert store.runtime_events(tasks[0].task_id)[0]["event_id"] == "evt-1"
    assert store.runtime_events(tasks[1].task_id)[0]["event_id"] == "evt-1"


def test_task_ledger_keeps_business_result_separate_from_runner_status(tmp_path, monkeypatch):
    import src.settings as settings

    monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_BACKEND", "runner_api")
    monkeypatch.setattr(settings, "SANDBOX_ALLOW_DYNAMIC_INSTALL", False)
    monkeypatch.setattr(settings, "SANDBOX_DEFAULT_NETWORK", "none")
    monkeypatch.setattr(settings, "SANDBOX_AUTHORIZED_CAPABILITIES", ("sandbox.execute",))
    store = SandboxTaskStore(str(tmp_path / "tasks.db"), validate_on_create=False)
    task = store.create(
        SandboxTaskSpec(
            idempotency_key="runtime-result-task",
            owner_id="user-1",
            department_id="dept-1",
            bundle_ref="bundle/diagram",
            bundle_digest="a" * 64,
            profile_ref="profile/dev-l1",
            policy_snapshot_hash="policy-1",
            required_capabilities=("sandbox.execute",),
        )
    )

    updated = store.record_result(
        task.task_id,
        {
            "runtime_contract_version": "v1",
            "result_status": "validation_failed",
            "issues": [{"code": "missing_interface"}],
            "artifact_ids": ["art-1234567890abcdef"],
        },
    )

    assert updated.status == "admitted"
    assert updated.result_status == "validation_failed"
    assert updated.to_dict()["result_status"] == "validation_failed"

    with pytest.raises(RuntimeContractError, match="task_id"):
        validate_skill_event(
            {
                "runtime_contract_version": "v1",
                "event_id": "evt-3",
                "seq": 3,
                "task_id": "task-other",
                "attempt": 1,
                "step_id": "build_spec",
                "event": "step_progress",
            },
            task_id="task-123",
            attempt=1,
            previous_seq=2,
        )
