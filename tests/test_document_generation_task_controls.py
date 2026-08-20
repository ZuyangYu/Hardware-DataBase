from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.document_authoring.models import DocumentArtifact, DocumentWorkOrder
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.harness import runtime as harness_runtime
from src.document_authoring.harness.graph import HarnessExecutionResult
from src.document_authoring.work_order_store import DocumentAuthoringStore


def _work_order(work_order_id: str, status: str) -> DocumentWorkOrder:
    return DocumentWorkOrder(
        work_order_id=work_order_id,
        tenant_id="tenant-a",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id="snapshot-1",
        template_version_id="template-1",
        document_schema_id="schema-1",
        document_schema_version="1",
        template_schema_id="template-schema-1",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="deterministic_only",
        created_by="writer",
        status=status,
    )


def test_pause_marks_work_order_paused():
    service = object.__new__(DocumentGenerationService)
    run = SimpleNamespace(harness_run_id="run-1", work_order_id="wo-1", checkpoint_id="checkpoint-1")
    order = SimpleNamespace(work_order_id="wo-1", status="retrieving")
    service._harness_run_for_context = Mock(return_value=run)
    service.store = SimpleNamespace(
        request_harness_run_state=Mock(return_value=run),
        finalize_harness_checkpoint=Mock(),
    )
    service._order_raw = Mock(return_value=order)
    service._replace_order = Mock()

    service.pause_harness_run("ctx", "run-1")

    service._replace_order.assert_called_once_with(order, status="paused")


def test_terminal_delete_removes_owned_artifact_and_keeps_audit(tmp_path):
    store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    order = store.create_work_order(_work_order("wo-complete", "complete"))
    content = b"generated workbook"
    artifact = store.save_artifact(
        DocumentArtifact(
            artifact_id="artifact-1",
            tenant_id="tenant-a",
            work_order_id=order.work_order_id,
            run_id="run-1",
            stage="review_candidate",
            content_hash=hashlib.sha256(content).hexdigest(),
            validation_report_id="report-1",
            integrity_manifest_id="manifest-1",
        ),
        content,
        "xlsx",
    )

    audit = store.delete_terminal_work_order(
        order.work_order_id,
        actor_id="writer",
        reason="用户确认删除",
    )

    assert store.get_work_order(order.work_order_id) is None
    assert store.get_artifact(artifact.artifact_id) is None
    assert not Path(artifact.storage_ref).exists()
    assert audit.work_order_id == order.work_order_id
    assert audit.actor_id == "writer"


def test_running_work_order_cannot_be_deleted(tmp_path):
    store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    order = store.create_work_order(_work_order("wo-running", "retrieving"))

    with pytest.raises(ValueError, match="terminal"):
        store.delete_terminal_work_order(order.work_order_id, actor_id="writer", reason="删除")


def test_harness_progress_mirrors_checkpoint_into_run(monkeypatch):
    """Status API reads harness_runs, so progress callbacks must update it too."""
    running = SimpleNamespace(
        harness_run_id="run-1",
        work_order_id="wo-1",
        fencing_token=3,
        status="running",
    )
    store = Mock()
    store.claim_harness_run.return_value = running
    store.update_harness_run_owned.return_value = running
    store.heartbeat_harness_run.return_value = running
    store.save_harness_checkpoint_owned.side_effect = lambda checkpoint, *_args: checkpoint

    class FakeGraph:
        def __init__(self, *_args, **kwargs):
            self.on_progress = kwargs["on_progress"]

        def run(self, **_kwargs):
            self.on_progress({
                "current_node": "rerank_evidence",
                "step_count": 7,
                "retrieval_round_count": 2,
            })
            return HarnessExecutionResult()

    class FakeManifest:
        def model_copy(self, **_kwargs):
            return self

    monkeypatch.setattr(harness_runtime, "AuthoringGraph", FakeGraph)
    runtime = harness_runtime.InternalDocumentHarnessRuntime(store)
    runtime.execute(
        work_order=SimpleNamespace(
            work_order_id="wo-1", input_fingerprint="input", source_set_snapshot_id="snapshot",
        ),
        run=SimpleNamespace(harness_run_id="run-1"),
        manifest=FakeManifest(),
        policy=SimpleNamespace(status="approved", lease_seconds=30),
        schema=SimpleNamespace(),
        snapshot=SimpleNamespace(),
        legacy_claims=[],
        writer=SimpleNamespace(),
        retrieve=Mock(),
    )

    progress_updates = [call.kwargs for call in store.update_harness_run_owned.call_args_list]
    assert any(
        update.get("current_node") == "rerank_evidence"
        and update.get("step_count") == 7
        and update.get("retrieval_round_count") == 2
        and update.get("completed_units") == 0
        and update.get("total_units") == 0
        for update in progress_updates
    )
