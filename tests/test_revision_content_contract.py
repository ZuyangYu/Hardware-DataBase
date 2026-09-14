"""Contract tests for executable, plan-bound document revisions."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from src.document_authoring.models import DocumentArtifact, DocumentUnitDraft
from src.document_authoring.planning.models import DocumentPlan, OutputSpec
from src.document_authoring.planning.revision import (
    RevisionPatch,
    compile_revision_plan,
)
from src.document_authoring.revisions import ArtifactRevisionStore, DocumentRevisionService
from src.document_authoring.tasks import DocumentTaskStore
from src.document_authoring.writers.managed import DeterministicEvidenceWriter, ManagedWriter
from src.document_authoring.writers.provider import WriterRequest

from tests.test_document_planning_contracts import _output_payload, _plan_payload


def test_plan_bound_revision_with_unchanged_target_is_not_marked_successful(tmp_path) -> None:
    """A generated child must contain the requested semantic change."""

    from tests.test_document_revisions import _xlsx_bytes

    task_store = DocumentTaskStore(str(tmp_path / "tasks.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="completed",
    )
    content = _xlsx_bytes({"A1": "Summary", "A2": "unchanged"})
    parent = DocumentArtifact(
        artifact_id="artifact-parent",
        tenant_id="tenant-a",
        work_order_id="wo-revision",
        run_id="run-parent",
        output_format="xlsx",
        stage="approved_release",
        content_hash=hashlib.sha256(content).hexdigest(),
        validation_report_id="report-parent",
        integrity_manifest_id="manifest-parent",
    )
    child = DocumentArtifact(
        artifact_id="artifact-child",
        tenant_id="tenant-a",
        work_order_id="wo-revision",
        run_id="run-child",
        output_format="xlsx",
        stage="review_candidate",
        parent_artifact_id=parent.artifact_id,
        content_hash=parent.content_hash,
        validation_report_id="report-child",
        integrity_manifest_id="manifest-child",
    )
    draft = DocumentUnitDraft(
        unit_id="summary",
        run_id="run-parent",
        generated_by="managed_writer",
        content="unchanged",
        proposed_value="unchanged",
        evidence_ids=["ev-summary"],
    )
    child_draft = draft.model_copy(update={"run_id": "run-child"})

    class AuthoringStore:
        db_path = str(tmp_path / "authoring.db")

        def __init__(self):
            self.artifacts = {parent.artifact_id: parent, child.artifact_id: child}

        def get_artifact(self, artifact_id):
            return self.artifacts.get(artifact_id)

        def get_work_order(self, work_order_id):
            return SimpleNamespace(
                work_order_id=work_order_id,
                task_id=task.task_id,
                tenant_id="tenant-a",
                knowledge_base_name="hardware",
            )

        def read_artifact_content(self, artifact_id):
            return content

        def list_unit_drafts(self, run_id):
            return [draft if run_id == "run-parent" else child_draft]

        def update_artifact(self, artifact_id, **updates):
            updated = self.artifacts[artifact_id].model_copy(update=updates)
            self.artifacts[artifact_id] = updated
            return updated

    revision_store = ArtifactRevisionStore(tmp_path / "revisions.db")
    revision = revision_store.create(
        revision_id="revision-unchanged-target",
        task_id=task.task_id,
        work_order_id=parent.work_order_id,
        parent_artifact_id=parent.artifact_id,
        request_type="section_update",
        request="修改 Summary",
        changed_sections=["Summary"],
        input_snapshot_hash="input-hash",
        source_snapshot_hash="source-hash",
        parent_plan_id="plan-parent",
        parent_plan_version=1,
        parent_plan_hash="parent-plan-hash",
        child_plan_id="plan-child",
        child_plan_version=2,
        child_plan_hash="child-plan-hash",
        affected_unit_ids=["summary"],
        metadata={"target_unit_ids": ["summary"]},
        client_request_id="unchanged-target",
    )
    service = DocumentRevisionService(
        authoring_store=AuthoringStore(),
        task_store=task_store,
        revision_store=revision_store,
    )

    completed = service.bind_generated_child(
        SimpleNamespace(
            work_order_id=parent.work_order_id,
            task_id=task.task_id,
            revision_id=revision.revision_id,
        ),
        child,
        revalidation_status="passed",
    )

    assert completed.revalidation_status == "failed"
    assert completed.status == "failed"
    assert completed.revalidation_result["error_code"] == "revision_content_unchanged"


def test_section_revision_binds_instruction_to_only_the_target_unit() -> None:
    """A local revision must change execution input, not merely create lineage."""

    parent = DocumentPlan.model_validate(_plan_payload(status="accepted"))
    output_spec = OutputSpec(**_output_payload())
    patch = RevisionPatch(
        patch_id="revision-patch:english-pins",
        revision_id="artifact-revision-english-pins",
        parent_artifact_id="artifact-parent",
        parent_artifact_content_hash="sha256:parent",
        parent_plan_id=parent.document_plan_id,
        parent_plan_version=parent.version,
        parent_plan_hash=parent.plan_hash,
        operation="section_update",
        instruction="把第二章改成英文，其他章节别动",
        changed_sections=["第二章"],
    )

    compilation = compile_revision_plan(parent, output_spec, patch)

    assert compilation.target_unit_ids == ["pins"]
    assert compilation.preserved_unit_ids == ["cover"]
    target = next(
        unit for unit in compilation.child_plan.semantic_units
        if unit.unit_id == "pins"
    )
    preserved = next(
        unit for unit in compilation.child_plan.semantic_units
        if unit.unit_id == "cover"
    )
    assert target.output_schema["revision_instruction"] == patch.instruction
    assert "revision_instruction" not in preserved.output_schema


def test_explicit_text_replacement_is_compiled_as_server_owned_revision_edit() -> None:
    parent = DocumentPlan.model_validate(_plan_payload(status="accepted"))
    output_spec = OutputSpec(**_output_payload())
    patch = RevisionPatch(
        patch_id="revision-patch:replace",
        revision_id="artifact-revision-replace",
        parent_artifact_id="artifact-parent",
        parent_artifact_content_hash="sha256:parent",
        parent_plan_id=parent.document_plan_id,
        parent_plan_version=parent.version,
        parent_plan_hash=parent.plan_hash,
        operation="section_update",
        instruction="将“旧标题”替换为“新标题”",
        changed_sections=["cover"],
    )

    compilation = compile_revision_plan(parent, output_spec, patch)
    target = next(
        unit for unit in compilation.child_plan.semantic_units
        if unit.unit_id == "cover"
    )

    assert target.output_schema["revision_edit"] == {
        "kind": "replace_text",
        "before": "旧标题",
        "after": "新标题",
    }


def _revision_writer_request(*, revision_edit=None, field_value_type="text") -> WriterRequest:
    return WriterRequest(
        work_order_id="work-order-revision",
        run_id="run-revision",
        unit_id="field:cover",
        unit_label="Cover",
        field_value_type=field_value_type,
        revision_edit=revision_edit,
        prompt_version="1",
        evidence=[{
            "id": "evidence-cover",
            "content": "Cover title: ADAS ICD",
            "source_name": "design.pdf",
            "metadata": {},
            "locator": {},
            "fact_type": None,
        }],
    )


def test_managed_writer_applies_compiled_text_edit_to_renderable_scalar_and_keeps_anchor() -> None:
    request = _revision_writer_request(revision_edit={
        "kind": "replace_text",
        "before": "ADAS ICD",
        "after": "ADAS Interface Control Document",
    })

    draft = ManagedWriter(DeterministicEvidenceWriter()).generate(request)

    assert draft.content == "Cover title: ADAS Interface Control Document"
    assert draft.proposed_value == draft.content
    assert draft.typed_value is not None
    assert draft.typed_value.normalized_values == ["ADAS Interface Control Document"]
    assert draft.typed_value.display_value == "ADAS Interface Control Document"
    # The assertion remains grounded in the source phrase; the edit itself is
    # recorded as a server-owned transformation in operational metadata.
    assert draft.assertions[0].text == "Cover title: ADAS ICD"
    assert draft.assertions[0].evidence_ids == ["evidence-cover"]
    assert draft.metadata["revision_edit"] == {
        "kind": "replace_text",
        "before": "ADAS ICD",
        "after": "ADAS Interface Control Document",
        "applied": True,
        "target_fields": [
            "content",
            "proposed_value",
            "typed_value.display_value",
            "typed_value.normalized_values[0]",
        ],
    }


def test_managed_writer_rejects_compiled_text_edit_when_renderable_target_is_absent() -> None:
    request = _revision_writer_request(revision_edit={
        "kind": "replace_text",
        "before": "not in draft",
        "after": "new value",
    })

    with pytest.raises(ValueError, match="not found"):
        ManagedWriter(DeterministicEvidenceWriter()).generate(request)


def test_managed_writer_fails_closed_for_text_edits_on_typed_tables() -> None:
    request = _revision_writer_request(
        field_value_type="table",
        revision_edit={
            "kind": "replace_text",
            "before": "ADAS ICD",
            "after": "new value",
        },
    )

    with pytest.raises(ValueError, match="table"):
        ManagedWriter(DeterministicEvidenceWriter()).generate(request)
