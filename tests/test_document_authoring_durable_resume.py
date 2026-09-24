"""Durable approval/resume boundary tests."""

from __future__ import annotations

from types import SimpleNamespace

from src.core.app_pipeline import AppPipeline
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.pipelines.document_rag.schemas import RequestContext


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="user-a",
        session_id="chat-a",
        tenant_id="tenant-a",
        metadata={"resource_department_id": 7},
        kb_permissions={"7:hardware": "write"},
    )


class _DecisionService:
    def __init__(self):
        self.run = SimpleNamespace(work_order_id="work-order-a")
        self.order = SimpleNamespace(
            work_order_id="work-order-a",
            scope_type="knowledge_base",
            knowledge_base_name="hardware",
        )
        self.calls = []

        self.store = SimpleNamespace(
            get_harness_run=lambda _run_id: self.run,
            get_work_order=lambda _work_order_id: self.order,
        )

    def require_work_order_capability(self, *_args):
        return None

    def resolve_agent_human_decision(self, _ctx, harness_run_id, **kwargs):
        self.calls.append((harness_run_id, kwargs))
        return {
            "decision": "approve",
            "decision_key": "decision-key-a",
            "event_id": "human-event-a",
            "harness_run": {"harness_run_id": harness_run_id, "status": "retrying"},
        }


def test_human_approval_enqueues_same_run_and_replays_same_job(tmp_path):
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = _DecisionService()
    pipeline.document_job_store = DocumentAuthoringJobStore(str(tmp_path / "jobs.db"))

    first = pipeline.resolve_knowledge_base_harness_human_decision(
        _ctx(),
        "harness-run-a",
        pending_event_id="pending-a",
        proposal_hash="proposal-a",
        decision="approve",
    )
    replay = pipeline.resolve_knowledge_base_harness_human_decision(
        _ctx(),
        "harness-run-a",
        pending_event_id="pending-a",
        proposal_hash="proposal-a",
        decision="approve",
    )

    assert first["job_id"] == replay["job_id"]
    assert first["job_status"] == "queued"
    assert first["next_actions"] == ["poll_status"]
    assert pipeline.document_generation.calls[0][1]["retrieve"] is None
    job = pipeline.document_job_store.get(first["job_id"])
    assert job is not None
    assert job.operation == "resume_work_order"
    assert job.payload == {
        "harness_run_id": "harness-run-a",
        "knowledge_base_name": "hardware",
        "work_order_id": "work-order-a",
    }


def test_reject_decision_does_not_create_resume_job(tmp_path):
    pipeline = object.__new__(AppPipeline)
    service = _DecisionService()
    pipeline.document_generation = service
    pipeline.document_job_store = DocumentAuthoringJobStore(str(tmp_path / "jobs.db"))

    def reject(_ctx, _run_id, **kwargs):
        service.calls.append((_run_id, kwargs))
        return {
            "decision": "reject",
            "decision_key": "decision-key-reject",
            "event_id": "human-event-reject",
            "harness_run": {"status": "failed"},
        }

    service.resolve_agent_human_decision = reject
    result = pipeline.resolve_knowledge_base_harness_human_decision(
        _ctx(),
        "harness-run-a",
        pending_event_id="pending-a",
        proposal_hash="proposal-a",
        decision="reject",
    )

    assert result["decision"] == "reject"
    assert "job_id" not in result
    assert pipeline.document_job_store.list_pending() == []
