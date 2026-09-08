from __future__ import annotations

import sqlite3
from unittest.mock import Mock

import pytest

from src.core.app_pipeline import AppPipeline
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.models import content_hash
from src.document_authoring.reviews import DocumentReviewStore
from src.document_authoring.tasks import DocumentTaskStore
from src.pipelines.document_rag.schemas import RequestContext


def _count_rows(db_path, table_name: str) -> int:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


def _create_review(store: DocumentReviewStore, **overrides):
    values = {
        "task_id": "task-1",
        "work_order_id": "work-order-1",
        "artifact_id": "artifact-1",
        "review_kind": "field_mapping",
        "status": "pending",
        "subject_hash": "subject-hash-1",
        "source_snapshot_hash": "source-snapshot-hash-1",
        "schema_hash": "schema-hash-1",
        "metadata": {"field_id": "board.revision", "confidence": 0.56},
        "client_request_id": "create-review-1",
    }
    values.update(overrides)
    return store.create(**values)


def test_create_persists_and_rehydrates_task_bound_review(tmp_path):
    db_path = tmp_path / "reviews.db"
    store = DocumentReviewStore(db_path)

    created = _create_review(store)
    rehydrated = DocumentReviewStore(db_path).get(created.review_id)

    assert rehydrated is not None
    assert rehydrated.review_id == created.review_id
    assert rehydrated.task_id == "task-1"
    assert rehydrated.work_order_id == "work-order-1"
    assert rehydrated.artifact_id == "artifact-1"
    assert rehydrated.review_kind == "field_mapping"
    assert rehydrated.status == "pending"
    assert rehydrated.subject_hash == "subject-hash-1"
    assert rehydrated.source_snapshot_hash == "source-snapshot-hash-1"
    assert rehydrated.schema_hash == "schema-hash-1"
    assert rehydrated.metadata == {"field_id": "board.revision", "confidence": 0.56}
    assert rehydrated.client_request_id == "create-review-1"
    assert rehydrated.created_at == created.created_at
    assert rehydrated.updated_at == created.updated_at


def test_create_reuses_same_client_request_and_rejects_payload_conflict(tmp_path):
    db_path = tmp_path / "reviews.db"
    store = DocumentReviewStore(db_path)

    first = _create_review(store)
    retry = _create_review(store, review_id="a-different-id")

    assert retry.review_id == first.review_id
    assert _count_rows(db_path, "document_reviews") == 1

    with pytest.raises(ValueError, match="idempotency key conflicts"):
        _create_review(store, subject_hash="a-different-subject")

    assert _count_rows(db_path, "document_reviews") == 1


def test_create_retry_after_decision_reuses_the_existing_review(tmp_path):
    db_path = tmp_path / "reviews.db"
    store = DocumentReviewStore(db_path)
    review = _create_review(store)

    decided = store.submit_decision(
        review.review_id,
        subject_hash="subject-hash-1",
        decision="approved",
        status="approved",
        client_request_id="decision-after-create",
    )
    retry = _create_review(store, review_id="new-review-id")

    assert retry == decided
    assert _count_rows(db_path, "document_reviews") == 1


def test_list_for_task_returns_only_reviews_bound_to_that_task(tmp_path):
    store = DocumentReviewStore(tmp_path / "reviews.db")
    first = _create_review(store)
    second = _create_review(
        store,
        review_id="review-2",
        client_request_id="create-review-2",
        review_kind="protected_field",
    )
    other_task = _create_review(
        store,
        task_id="task-2",
        review_id="review-3",
        client_request_id="create-review-3",
    )

    reviews = store.list_for_task("task-1")

    assert [review.review_id for review in reviews] == [first.review_id, second.review_id]
    assert [review.review_id for review in store.list_for_task("task-2")] == [other_task.review_id]
    assert store.list_for_task("missing-task") == []


def test_submit_decision_requires_current_subject_hash_and_persists_decision(tmp_path):
    store = DocumentReviewStore(tmp_path / "reviews.db")
    review = _create_review(store)

    with pytest.raises(ValueError, match="subject hash"):
        store.submit_decision(
            review.review_id,
            subject_hash="stale-subject-hash",
            decision={"outcome": "approved"},
            status="approved",
            client_request_id="decision-1",
        )

    assert store.get(review.review_id).status == "pending"
    assert _count_rows(store.db_path, "document_review_decision_commands") == 0

    decided = store.submit_decision(
        review.review_id,
        subject_hash="subject-hash-1",
        decision={"outcome": "approved", "comment": "mapping is correct"},
        status="approved",
        client_request_id="decision-1",
    )

    assert decided.status == "approved"
    assert decided.decision == {"outcome": "approved", "comment": "mapping is correct"}
    assert decided.decided_at is not None
    assert decided.updated_at >= review.updated_at


def test_submit_decision_is_idempotent_and_does_not_duplicate_command_or_review(tmp_path):
    db_path = tmp_path / "reviews.db"
    store = DocumentReviewStore(db_path)
    review = _create_review(store)
    decision = {
        "outcome": "rejected",
        "comment": "missing evidence",
    }

    first = store.submit_decision(
        review.review_id,
        subject_hash="subject-hash-1",
        decision=decision,
        status="rejected",
        client_request_id="decision-retry-1",
    )
    retry = store.submit_decision(
        review.review_id,
        subject_hash="subject-hash-1",
        decision=decision,
        status="rejected",
        client_request_id="decision-retry-1",
    )

    assert retry == first
    assert _count_rows(db_path, "document_reviews") == 1
    assert _count_rows(db_path, "document_review_decision_commands") == 1


def test_submit_decision_rejects_reusing_client_request_for_a_different_command(tmp_path):
    store = DocumentReviewStore(tmp_path / "reviews.db")
    review = _create_review(store)

    store.submit_decision(
        review.review_id,
        subject_hash="subject-hash-1",
        decision="approved",
        status="approved",
        client_request_id="decision-conflict-1",
    )

    with pytest.raises(ValueError, match="idempotency key conflicts"):
        store.submit_decision(
            review.review_id,
            subject_hash="subject-hash-1",
            decision="rejected",
            status="rejected",
            client_request_id="decision-conflict-1",
        )

    assert store.get(review.review_id).status == "approved"
    assert _count_rows(store.db_path, "document_review_decision_commands") == 1


def test_legacy_review_projection_uses_the_resolved_source_content_hash(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
    )
    review_store = DocumentReviewStore(tmp_path / "authoring.db")
    artifact = type(
        "Artifact",
        (),
        {
            "artifact_id": "artifact-a",
            "stage": "review_candidate",
            "content_hash": "artifact-content-hash",
            "approval_subject_hash": "approval-subject-hash",
        },
    )()
    order = type(
        "WorkOrder",
        (),
        {
            "work_order_id": "work-order-a",
            "template_version_id": "template-a",
            "document_schema_id": "schema-a",
            "document_schema_version": "1",
            "source_set_snapshot_id": "snapshot-id-must-not-be-used-as-a-hash",
            "baseline_content_hash": "",
        },
    )()
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = type(
        "DocumentGeneration",
        (),
        {
            "store": type(
                "Store",
                (),
                {
                    "get_icd_scope_review": lambda _self, _work_order_id: None,
                    "list_artifacts": lambda _self, _work_order_id: [artifact],
                },
            )(),
            "resolve_source_snapshot": lambda _self, _order: type(
                "Snapshot", (), {"content_hash": "resolved-source-content-hash"}
            )(),
        },
    )()

    pipeline._materialize_document_reviews(task, order, review_store)

    reviews = review_store.list_for_task(task.task_id)
    assert len(reviews) == 1
    assert reviews[0].source_snapshot_hash == "resolved-source-content-hash"


def test_artifact_review_projection_uses_the_approval_subject_hash_contract(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
    )
    review_store = DocumentReviewStore(tmp_path / "authoring.db")
    artifact = type(
        "Artifact",
        (),
        {
            "artifact_id": "artifact-b",
            "stage": "review_candidate",
            "content_hash": "artifact-content-hash",
            "approval_subject_hash": None,
            "validation_report_id": "report-b",
        },
    )()
    order = type(
        "WorkOrder",
        (),
        {
            "work_order_id": "work-order-b",
            "template_version_id": "template-b",
            "document_schema_id": "schema-b",
            "document_schema_version": "1",
            "source_set_snapshot_id": "snapshot-id",
            "baseline_content_hash": "",
        },
    )()
    report = type("Report", (), {"content_hash": "validation-report-hash"})()
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = type(
        "DocumentGeneration",
        (),
        {
            "store": type(
                "Store",
                (),
                {
                    "get_icd_scope_review": lambda _self, _work_order_id: None,
                    "list_artifacts": lambda _self, _work_order_id: [artifact],
                    "get_validation_report": lambda _self, _report_id: report,
                },
            )(),
            "resolve_source_snapshot": lambda _self, _order: type(
                "Snapshot", (), {"content_hash": "source-content-hash"}
            )(),
        },
    )()

    pipeline._materialize_document_reviews(task, order, review_store)

    expected_subject_hash = content_hash({
        "artifact_content_hash": "artifact-content-hash",
        "validation_report_hash": "validation-report-hash",
        "source_set_snapshot_hash": "source-content-hash",
    })
    reviews = review_store.list_for_task(task.task_id)
    assert len(reviews) == 1
    assert reviews[0].subject_hash == expected_subject_hash


def test_approved_artifact_review_releases_the_candidate_once(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="waiting_human",
    )
    review_store = DocumentReviewStore(tmp_path / "authoring.db")
    review = _create_review(
        review_store,
        task_id=task.task_id,
        review_kind="artifact_approval:artifact-a",
        artifact_id="artifact-a",
        subject_hash="approval-subject-hash",
    )
    candidate = type("Artifact", (), {"artifact_id": "artifact-a", "stage": "review_candidate"})()
    released = type(
        "Artifact",
        (),
        {"artifact_id": "artifact-release", "parent_artifact_id": "artifact-a", "stage": "approved_release"},
    )()
    artifacts = [candidate]
    approve_artifact = Mock(side_effect=lambda *_args, **_kwargs: (artifacts.append(released), released)[1])
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = type(
        "DocumentGeneration",
        (),
        {
            "review_store": review_store,
            "task_service": type("TaskService", (), {"store": task_store})(),
            "approve_document_artifact": approve_artifact,
            "store": type(
                "Store",
                (),
                {"get_artifact": lambda _self, _artifact_id: candidate, "list_artifacts": lambda _self, _work_order_id: artifacts},
            )(),
        },
    )()
    pipeline._document_task_for_context = AppPipeline._document_task_for_context.__get__(pipeline)
    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )

    first = pipeline.submit_document_review_decision(
        ctx,
        review.review_id,
        subject_hash=review.subject_hash,
        decision="approved",
        client_request_id="artifact-decision-1",
    )
    replay = pipeline.submit_document_review_decision(
        ctx,
        review.review_id,
        subject_hash=review.subject_hash,
        decision="approved",
        client_request_id="artifact-decision-1",
    )

    assert first["status"] == "approved"
    assert replay == first
    approve_artifact.assert_called_once_with(ctx, "artifact-a", comment="")
    assert task_store.get(task.task_id).status == "completed"


def test_artifact_approval_release_failure_does_not_commit_review_decision(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="waiting_human",
    )
    review_store = DocumentReviewStore(str(tmp_path / "authoring.db"))
    review = _create_review(
        review_store,
        task_id=task.task_id,
        review_kind="artifact_approval:artifact-stale",
        artifact_id="artifact-stale",
        subject_hash="approval-subject-hash",
    )
    candidate = type("Artifact", (), {"artifact_id": "artifact-stale", "stage": "review_candidate"})()
    approve_artifact = Mock(side_effect=ValueError("candidate is stale"))
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = type(
        "DocumentGeneration",
        (),
        {
            "review_store": review_store,
            "task_service": type("TaskService", (), {"store": task_store})(),
            "approve_document_artifact": approve_artifact,
            "store": type(
                "Store",
                (),
                {
                    "get_artifact": lambda _self, _artifact_id: candidate,
                    "list_artifacts": lambda _self, _work_order_id: [candidate],
                },
            )(),
        },
    )()
    pipeline._document_task_for_context = AppPipeline._document_task_for_context.__get__(pipeline)
    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )

    with pytest.raises(ValueError, match="stale"):
        pipeline.submit_document_review_decision(
            ctx,
            review.review_id,
            subject_hash=review.subject_hash,
            decision="approved",
            client_request_id="artifact-decision-stale",
        )

    persisted = review_store.get(review.review_id)
    assert persisted is not None
    assert persisted.status == "pending"
    assert persisted.decision is None
    approve_artifact.assert_called_once_with(ctx, "artifact-stale", comment="")


def test_legacy_artifact_review_is_not_recreated_after_direct_release(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
    )
    review_store = DocumentReviewStore(tmp_path / "authoring.db")
    candidate = type("Artifact", (), {"artifact_id": "artifact-c", "stage": "review_candidate", "content_hash": "hash-c"})()
    released = type(
        "Artifact",
        (),
        {"artifact_id": "artifact-c-release", "parent_artifact_id": "artifact-c", "stage": "approved_release"},
    )()
    order = type(
        "WorkOrder",
        (),
        {
            "work_order_id": "work-order-c",
            "template_version_id": "template-c",
            "document_schema_id": "schema-c",
            "document_schema_version": "1",
            "source_set_snapshot_id": "snapshot-c",
            "baseline_content_hash": "",
        },
    )()
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = type(
        "DocumentGeneration",
        (),
        {
            "store": type(
                "Store",
                (),
                {
                    "get_icd_scope_review": lambda _self, _work_order_id: None,
                    "list_artifacts": lambda _self, _work_order_id: [candidate, released],
                    "get_validation_report": lambda _self, _report_id: None,
                },
            )(),
            "resolve_source_snapshot": lambda _self, _order: type(
                "Snapshot", (), {"content_hash": "source-content-hash-c"}
            )(),
        },
    )()

    pipeline._materialize_document_reviews(task, order, review_store)

    assert review_store.list_for_task(task.task_id) == []


def test_pipeline_projects_approved_review_back_to_waiting_task_once(tmp_path):
    db_path = tmp_path / "authoring.db"
    task_store = DocumentTaskStore(str(db_path))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="waiting_human",
    )
    review_store = DocumentReviewStore(db_path)
    review = _create_review(review_store, task_id=task.task_id)
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = type(
        "DocumentGeneration",
        (),
        {
            "review_store": review_store,
            "task_service": type("TaskService", (), {"store": task_store})(),
        },
    )()
    pipeline._document_task_for_context = AppPipeline._document_task_for_context.__get__(pipeline)

    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )
    decided = pipeline.submit_document_review_decision(
        ctx,
        review.review_id,
        subject_hash=review.subject_hash,
        decision="approved",
        client_request_id="decision-project-1",
    )
    repeated = pipeline.submit_document_review_decision(
        ctx,
        review.review_id,
        subject_hash=review.subject_hash,
        decision="approved",
        client_request_id="decision-project-1",
    )

    assert decided["status"] == "approved"
    assert repeated == decided
    assert task_store.get(task.task_id).status == "planned"


def test_approved_task_can_be_resumed_through_the_task_identity(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="planned",
    )
    task_store.attach_work_order(task.task_id, "work-order-a")
    order = type(
        "WorkOrder",
        (),
        {
            "work_order_id": "work-order-a",
            "task_id": task.task_id,
            "scope_type": "knowledge_base",
            "knowledge_base_name": "hardware",
            "status": "planned",
        },
    )()
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = type(
        "DocumentGeneration",
        (),
        {
            "task_service": type("TaskService", (), {"store": task_store})(),
            "store": type(
                "Store",
                (),
                {"get_work_order": lambda _self, _work_order_id: order},
            )(),
            "require_work_order_capability": lambda *_args: None,
        },
    )()
    pipeline.document_job_store = DocumentAuthoringJobStore(str(tmp_path / "jobs.db"))
    pipeline._document_task_for_context = AppPipeline._document_task_for_context.__get__(pipeline)
    pipeline._document_reviews_for_task = lambda *_args: []
    pipeline.submit_knowledge_base_document_generation = (
        AppPipeline.submit_knowledge_base_document_generation.__get__(pipeline)
    )

    result = pipeline.resume_document_task(
        RequestContext(
            user_id="user-a",
            tenant_id="tenant-a",
            kb_permissions={"1:hardware": "write"},
            metadata={"resource_department_id": 1, "kb_id": 1},
        ),
        task.task_id,
    )

    assert result["task_id"] == task.task_id
    assert result["work_order_id"] == "work-order-a"
    job = pipeline.document_job_store.get(result["run_id"])
    assert job is not None
    assert job.operation == "generate_work_order"
