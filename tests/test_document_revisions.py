from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from src.document_authoring.revisions import (
    ArtifactRevisionStore,
    DocumentRevisionService,
)
from src.document_authoring.tasks import DocumentTaskStore
from src.pipelines.document_rag.schemas import RequestContext


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )


def test_revision_store_round_trips_and_reuses_idempotent_request(tmp_path):
    store = ArtifactRevisionStore(tmp_path / "revisions.db")
    payload = {
        "task_id": "task-a",
        "work_order_id": "wo-a",
        "parent_artifact_id": "artifact-parent",
        "request_type": "field_update",
        "request": "更新 PCB 版本",
        "changed_fields": ["pcb_revision"],
        "changed_sections": [],
        "input_snapshot_hash": "input-hash",
        "source_snapshot_hash": "source-hash",
        "template_version_id": "template-a",
        "schema_hash": "schema-hash",
        "impact_scope": {"kind": "fields", "fields": ["pcb_revision"]},
        "revalidation_scope": ["artifact", "approval", "pcb_revision"],
        "client_request_id": "revision-request-1",
    }
    created = store.create(**payload)
    retry = store.create(**{**payload, "revision_id": "another-id"})
    loaded = ArtifactRevisionStore(tmp_path / "revisions.db").get(created.revision_id)

    assert retry == created
    assert loaded is not None
    assert loaded.parent_artifact_id == "artifact-parent"
    assert loaded.impact_scope["fields"] == ["pcb_revision"]
    assert loaded.revalidation_scope == ["artifact", "approval", "pcb_revision"]

    with pytest.raises(ValueError, match="idempotency"):
        store.create(**{**payload, "request": "更新另一个版本"})


def test_revision_service_freezes_input_impact_and_marks_parent_for_revalidation(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "authoring.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="completed",
    )
    parent = SimpleNamespace(
        artifact_id="artifact-parent",
        work_order_id="wo-a",
        content_hash="artifact-content-hash",
        validation_report_id="report-a",
        stage="approved_release",
        validity_status="current",
        policy_status="active",
    )
    order = SimpleNamespace(
        work_order_id="wo-a",
        task_id=task.task_id,
        tenant_id="tenant-a",
        knowledge_base_name="hardware",
        source_set_snapshot_id="snapshot-a",
        baseline_content_hash="",
        template_version_id="template-a",
        template_schema_id="template-schema-a",
        template_schema_version="1",
        document_schema_id="schema-a",
        document_schema_version="1",
    )
    updates: list[tuple[str, dict]] = []

    class AuthoringStore:
        def get_artifact(self, artifact_id):
            return parent if artifact_id == parent.artifact_id else None

        def get_work_order(self, work_order_id):
            return order if work_order_id == order.work_order_id else None

        def update_artifact(self, artifact_id, **kwargs):
            updates.append((artifact_id, kwargs))
            return SimpleNamespace(**{**vars(parent), **kwargs})

    service = DocumentRevisionService(
        authoring_store=AuthoringStore(),
        task_store=task_store,
        revision_store=ArtifactRevisionStore(tmp_path / "revisions.db"),
        source_snapshot_resolver=lambda _order: SimpleNamespace(
            content_hash="resolved-source-content-hash"
        ),
    )

    revision = service.create_revision(
        _ctx(),
        task_id=task.task_id,
        parent_artifact_id=parent.artifact_id,
        request_type="field_update",
        request="更新 PCB 版本",
        changed_fields=["pcb_revision"],
        client_request_id="revision-request-1",
    )

    assert revision.status == "planned"
    assert revision.work_order_id == "wo-a"
    assert revision.impact_scope == {
        "kind": "fields",
        "fields": ["pcb_revision"],
        "sections": [],
        "requires_full_revalidation": False,
    }
    assert revision.revalidation_scope == ["artifact", "approval", "pcb_revision"]
    assert len(revision.input_snapshot_hash) == 64
    assert revision.source_snapshot_hash == "resolved-source-content-hash"
    assert updates == [("artifact-parent", {
        "validity_status": "revalidation_required",
        "regeneration_status": "recommended",
    })]


def test_revision_service_rejects_parent_from_another_task(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "tasks.db"))
    task = task_store.create_task(
        tenant_id="tenant-a", user_id="user-a", origin="workbench", created_by="user-a",
        knowledge_base_name="hardware",
    )
    service = DocumentRevisionService(
        authoring_store=SimpleNamespace(
            get_artifact=lambda _artifact_id: SimpleNamespace(
                artifact_id="artifact-parent", work_order_id="wo-a", content_hash="hash",
            ),
            get_work_order=lambda _work_order_id: SimpleNamespace(
                work_order_id="wo-a", task_id="other-task", tenant_id="tenant-a",
                knowledge_base_name="hardware",
            ),
        ),
        task_store=task_store,
        revision_store=ArtifactRevisionStore(tmp_path / "revisions.db"),
    )

    with pytest.raises(ValueError, match="does not belong to task"):
        service.create_revision(
            _ctx(),
            task_id=task.task_id,
            parent_artifact_id="artifact-parent",
            request_type="section_update",
            request="更新结论",
            changed_sections=["conclusion"],
            client_request_id="revision-request-1",
        )


def test_revision_service_binds_child_and_persists_revalidation_result(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "tasks.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="completed",
    )
    parent = SimpleNamespace(
        artifact_id="artifact-parent",
        work_order_id="wo-a",
        content_hash="parent-content-hash",
        validation_report_id="report-parent",
        stage="approved_release",
        validity_status="current",
        policy_status="active",
        approval_event_ids=["approval-parent"],
        revision_id=None,
        status_reasons=[],
    )
    child = SimpleNamespace(
        artifact_id="artifact-child",
        work_order_id="wo-a",
        content_hash="child-content-hash",
        validation_report_id="report-child",
        stage="review_candidate",
        parent_artifact_id="artifact-parent",
        validity_status="current",
        policy_status="active",
        regeneration_status="not_needed",
        revision_id=None,
        status_reasons=[],
    )
    order = SimpleNamespace(
        work_order_id="wo-a",
        task_id=task.task_id,
        tenant_id="tenant-a",
        knowledge_base_name="hardware",
        source_set_snapshot_id="snapshot-a",
        baseline_content_hash="",
        template_version_id="template-a",
        template_schema_id="template-schema-a",
        template_schema_version="1",
        document_schema_id="schema-a",
        document_schema_version="1",
    )
    artifacts = {parent.artifact_id: parent, child.artifact_id: child}

    class AuthoringStore:
        def get_artifact(self, artifact_id):
            return artifacts.get(artifact_id)

        def get_work_order(self, work_order_id):
            return order if work_order_id == order.work_order_id else None

        def update_artifact(self, artifact_id, **kwargs):
            current = artifacts[artifact_id]
            updated = SimpleNamespace(**{**vars(current), **kwargs})
            artifacts[artifact_id] = updated
            return updated

    service = DocumentRevisionService(
        authoring_store=AuthoringStore(),
        task_store=task_store,
        revision_store=ArtifactRevisionStore(tmp_path / "revisions.db"),
        source_snapshot_resolver=lambda _order: SimpleNamespace(
            content_hash="resolved-source-content-hash"
        ),
    )
    revision = service.create_revision(
        _ctx(),
        task_id=task.task_id,
        parent_artifact_id=parent.artifact_id,
        request_type="field_update",
        request="更新 PCB 版本",
        changed_fields=["pcb_revision"],
        client_request_id="revision-request-1",
    )

    completed = service.complete_revision(
        _ctx(),
        revision.revision_id,
        child_artifact_id=child.artifact_id,
        revalidation_status="passed",
        revalidation_result={"report_id": "report-child", "checked_fields": ["pcb_revision"]},
    )
    replay = service.complete_revision(
        _ctx(),
        revision.revision_id,
        child_artifact_id=child.artifact_id,
        revalidation_status="passed",
        revalidation_result={"report_id": "report-child", "checked_fields": ["pcb_revision"]},
    )

    assert completed == replay
    assert completed.child_artifact_id == child.artifact_id
    assert completed.status == "revalidated"
    assert completed.revalidation_status == "passed"
    assert completed.revalidation_result == {
        "report_id": "report-child",
        "checked_fields": ["pcb_revision"],
        "input_snapshot_hash": revision.input_snapshot_hash,
        "source_snapshot_hash": revision.source_snapshot_hash,
        "schema_hash": revision.schema_hash,
        "child_artifact_content_hash": child.content_hash,
    }
    assert artifacts[child.artifact_id].revision_id == completed.revision_id
    assert artifacts[child.artifact_id].validity_status == "current"
    assert artifacts[parent.artifact_id].validity_status == "revalidation_required"
    assert task_store.get(task.task_id).status == "waiting_human"

    released = service.mark_released(_ctx(), child.artifact_id)
    assert released is not None
    assert released.status == "completed"

    with pytest.raises(ValueError, match="revalidation"):
        service.complete_revision(
            _ctx(),
            revision.revision_id,
            child_artifact_id=child.artifact_id,
            revalidation_status="failed",
            revalidation_result={"report_id": "report-child"},
        )


def _child_binding_fixture(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "tasks.db"))
    task = task_store.create_task(
        tenant_id="tenant-a",
        user_id="user-a",
        origin="workbench",
        created_by="user-a",
        knowledge_base_name="hardware",
        status="completed",
    )
    parent = SimpleNamespace(
        artifact_id="artifact-parent",
        work_order_id="wo-a",
        content_hash="parent-content-hash",
        validation_report_id="report-parent",
        stage="approved_release",
        validity_status="current",
        policy_status="active",
        approval_event_ids=["approval-parent"],
        revision_id=None,
        status_reasons=[],
    )
    child = SimpleNamespace(
        artifact_id="artifact-child",
        work_order_id="wo-a",
        content_hash="child-content-hash",
        validation_report_id="report-child",
        stage="review_candidate",
        parent_artifact_id="artifact-parent",
        validity_status="current",
        policy_status="active",
        regeneration_status="not_needed",
        revision_id=None,
        status_reasons=[],
    )
    order = SimpleNamespace(
        work_order_id="wo-a",
        task_id=task.task_id,
        tenant_id="tenant-a",
        knowledge_base_name="hardware",
        source_set_snapshot_id="snapshot-a",
        baseline_content_hash="",
        template_version_id="template-a",
        template_schema_id="template-schema-a",
        template_schema_version="1",
        document_schema_id="schema-a",
        document_schema_version="1",
    )
    artifacts = {parent.artifact_id: parent, child.artifact_id: child}

    class AuthoringStore:
        def get_artifact(self, artifact_id):
            return artifacts.get(artifact_id)

        def get_work_order(self, work_order_id):
            return order if work_order_id == order.work_order_id else None

        def update_artifact(self, artifact_id, **kwargs):
            current = artifacts[artifact_id]
            updated = SimpleNamespace(**{**vars(current), **kwargs})
            artifacts[artifact_id] = updated
            return updated

    service = DocumentRevisionService(
        authoring_store=AuthoringStore(),
        task_store=task_store,
        revision_store=ArtifactRevisionStore(tmp_path / "revisions.db"),
        source_snapshot_resolver=lambda _order: SimpleNamespace(
            content_hash="resolved-source-hash"
        ),
    )
    revision = service.create_revision(
        _ctx(),
        task_id=task.task_id,
        parent_artifact_id=parent.artifact_id,
        request_type="field_update",
        request="更新 PCB 版本",
        changed_fields=["pcb_revision"],
        client_request_id="revision-request-1",
    )
    return service, task_store, artifacts, order, child, revision


def test_worker_path_binds_generated_child_without_request_context(tmp_path):
    service, task_store, artifacts, order, child, revision = _child_binding_fixture(tmp_path)

    completed = service.bind_generated_child(
        SimpleNamespace(work_order_id="wo-a", task_id=order.task_id, revision_id=revision.revision_id),
        child,
        revalidation_status="passed",
        revalidation_result={"report_id": "report-child"},
    )
    replay = service.bind_generated_child(
        SimpleNamespace(work_order_id="wo-a", task_id=order.task_id, revision_id=revision.revision_id),
        child,
        revalidation_status="passed",
        revalidation_result={"report_id": "report-child"},
    )

    assert completed.child_artifact_id == child.artifact_id
    assert completed.status == "revalidated"
    assert completed.revalidation_status == "passed"
    assert completed == replay
    assert artifacts[child.artifact_id].revision_id == completed.revision_id
    assert task_store.get(order.task_id).status == "waiting_human"


def test_worker_path_rejects_child_from_another_work_order(tmp_path):
    service, _task_store, _artifacts, _order, child, revision = _child_binding_fixture(tmp_path)

    with pytest.raises(ValueError, match="does not belong"):
        service.bind_generated_child(
            SimpleNamespace(work_order_id="wo-a", task_id="task-x", revision_id=revision.revision_id),
            SimpleNamespace(**{**vars(child), "work_order_id": "wo-other"}),
            revalidation_status="passed",
        )


def test_revision_restart_creates_linked_work_order_and_is_idempotent(tmp_path):
    from src.document_authoring.models import DocumentArtifact
    from src.document_authoring.service import DocumentGenerationService
    from src.document_authoring.work_order_store import DocumentAuthoringStore

    authoring_store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=authoring_store)
    service._template = lambda _template_id: SimpleNamespace(
        status="approved",
        template_version_id="template-1",
        template_schema_id="ts-1",
        template_schema_version="1",
        format="xlsx",
    )
    service._schema = lambda _schema_id, _version: SimpleNamespace(
        status="approved", execution_mode="deterministic_only", fields=[], review_items=[],
        document_schema_id="schema-1", version="1",
    )
    service._policy = lambda _template: SimpleNamespace(version="1")
    resolved_snapshot = SimpleNamespace(
        content_hash="resolved-hash", tenant_id="tenant-a", project_id=None,
        baseline_id=None, baseline_content_hash="", source_set_snapshot_id="snapshot-1",
    )
    service.resolve_source_snapshot = lambda _order: resolved_snapshot
    service.revision_service.source_snapshot_resolver = lambda _order: resolved_snapshot
    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )
    snapshot = SimpleNamespace(
        tenant_id="tenant-a", project_id=None, baseline_id=None,
        baseline_content_hash="", source_set_snapshot_id="snapshot-1",
    )
    original = service._create_frozen_work_order(
        ctx, scope_type="knowledge_base", snapshot=snapshot, knowledge_base_name="hardware",
        template_version_id="template-1", document_schema_id="schema-1",
        document_schema_version="1", idempotency_key="turn-1:document-generation",
        execution_mode="deterministic_only",
    )
    parent = DocumentArtifact(
        artifact_id="artifact-parent", tenant_id="tenant-a", work_order_id=original.work_order_id,
        run_id="run-1", output_format="xlsx", stage="approved_release",
        content_hash="parent-hash", validation_report_id="report-1",
        integrity_manifest_id="manifest-1",
    )
    authoring_store.save_artifact(parent, b"parent", "xlsx")
    revision = service.revision_service.create_revision(
        ctx, task_id=original.task_id, parent_artifact_id="artifact-parent",
        request_type="section_update", request="更新结论", changed_sections=["conclusion"],
        client_request_id="revision-request-1",
    )

    restarted = service.restart_work_order_for_revision(
        ctx, original.work_order_id, revision_id=revision.revision_id,
    )
    replay = service.restart_work_order_for_revision(
        ctx, original.work_order_id, revision_id=revision.revision_id,
    )

    assert restarted.revision_id == revision.revision_id
    assert restarted.restart_of_work_order_id == original.work_order_id
    assert restarted.idempotency_key == f"revision:{revision.revision_id}"
    assert restarted.work_order_id != original.work_order_id
    assert replay.work_order_id == restarted.work_order_id
    assert service.revision_service.store.get(revision.revision_id).status == "generating"
    task = service.task_service.store.get(original.task_id)
    assert task.work_order_id == restarted.work_order_id

    child = DocumentArtifact(
        artifact_id="artifact-child", tenant_id="tenant-a", work_order_id=restarted.work_order_id,
        run_id="run-x", output_format="xlsx", stage="review_candidate",
        content_hash="child-hash", validation_report_id="vr-x",
        integrity_manifest_id="im-x", parent_artifact_id="artifact-parent",
    )
    authoring_store.save_artifact(child, b"child", "xlsx")
    completed = service.revision_service.bind_generated_child(
        restarted, child, revalidation_status="passed",
    )
    assert completed.child_artifact_id == "artifact-child"
    with pytest.raises(ValueError, match="already generated"):
        service.restart_work_order_for_revision(
            ctx, original.work_order_id, revision_id=revision.revision_id,
        )


def test_revision_child_artifacts_carry_parent_lineage(tmp_path):
    from src.document_authoring.models import DocumentArtifact
    from src.document_authoring.service import DocumentGenerationService
    from src.document_authoring.work_order_store import DocumentAuthoringStore

    authoring_store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=authoring_store)
    service._template = lambda _template_id: SimpleNamespace(
        status="approved", template_version_id="template-1", template_schema_id="ts-1",
        template_schema_version="1", format="xlsx",
    )
    service._schema = lambda _schema_id, _version: SimpleNamespace(
        status="approved", execution_mode="deterministic_only", fields=[], review_items=[],
        document_schema_id="schema-1", version="1",
    )
    service._policy = lambda _template: SimpleNamespace(version="1")
    resolved_snapshot = SimpleNamespace(
        content_hash="resolved-hash", tenant_id="tenant-a", project_id=None,
        baseline_id=None, baseline_content_hash="", source_set_snapshot_id="snapshot-1",
    )
    service.resolve_source_snapshot = lambda _order: resolved_snapshot
    service.revision_service.source_snapshot_resolver = lambda _order: resolved_snapshot
    ctx = RequestContext(
        user_id="user-a", tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )
    snapshot = SimpleNamespace(
        tenant_id="tenant-a", project_id=None, baseline_id=None,
        baseline_content_hash="", source_set_snapshot_id="snapshot-1",
    )
    order = service._create_frozen_work_order(
        ctx, scope_type="knowledge_base", snapshot=snapshot, knowledge_base_name="hardware",
        template_version_id="template-1", document_schema_id="schema-1",
        document_schema_version="1", idempotency_key="turn-1:document-generation",
        execution_mode="deterministic_only",
    )
    parent = DocumentArtifact(
        artifact_id="artifact-parent", tenant_id="tenant-a", work_order_id=order.work_order_id,
        run_id="run-1", output_format="xlsx", stage="approved_release",
        content_hash="parent-hash", validation_report_id="report-1",
        integrity_manifest_id="manifest-1",
    )
    authoring_store.save_artifact(parent, b"parent", "xlsx")
    revision = service.revision_service.create_revision(
        ctx, task_id=order.task_id, parent_artifact_id="artifact-parent",
        request_type="field_update", request="更新版本",
        changed_fields=["pcb_revision"],
        client_request_id="lineage-1",
    )
    artifact = DocumentArtifact(
        artifact_id="artifact-child", tenant_id="tenant-a", work_order_id=order.work_order_id,
        run_id="run-x", output_format="xlsx", stage="review_candidate",
        content_hash="child-hash", validation_report_id="vr-1",
        integrity_manifest_id="im-1",
    )

    saved = service._save_artifact_for_task(
        order.model_copy(update={"revision_id": revision.revision_id}),
        artifact, b"child", "xlsx",
    )

    assert saved.parent_artifact_id == "artifact-parent"


def test_revision_orders_never_auto_publish():
    from src.document_authoring.service import _automatic_release_allowed

    report = SimpleNamespace(status="passed")
    assert _automatic_release_allowed(
        SimpleNamespace(revision_id="rev-1", generation_session_id=None),
        report, requires_review=False,
    ) is False
    assert _automatic_release_allowed(
        SimpleNamespace(revision_id=None, generation_session_id=None),
        report, requires_review=False,
    ) is True


def _xlsx_bytes(cells: dict[str, str]) -> bytes:
    import io
    import zipfile

    cell_xml = "".join(
        f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
        for ref, value in cells.items()
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData><row r="1">{cell_xml}</row></sheetData></worksheet>'
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_bind_generated_child_persists_deterministic_diff_report(tmp_path):
    from src.document_authoring.models import DocumentArtifact
    from src.document_authoring.service import DocumentGenerationService
    from src.document_authoring.work_order_store import DocumentAuthoringStore

    authoring_store = DocumentAuthoringStore(
        db_path=str(tmp_path / "authoring.db"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    service = DocumentGenerationService(store=authoring_store)
    service._template = lambda _template_id: SimpleNamespace(
        status="approved", template_version_id="template-1", template_schema_id="ts-1",
        template_schema_version="1", format="xlsx",
    )
    service._schema = lambda _schema_id, _version: SimpleNamespace(
        status="approved", execution_mode="deterministic_only", fields=[], review_items=[],
        document_schema_id="schema-1", version="1",
    )
    service._policy = lambda _template: SimpleNamespace(version="1")
    resolved_snapshot = SimpleNamespace(
        content_hash="resolved-hash", tenant_id="tenant-a", project_id=None,
        baseline_id=None, baseline_content_hash="", source_set_snapshot_id="snapshot-1",
    )
    service.resolve_source_snapshot = lambda _order: resolved_snapshot
    service.revision_service.source_snapshot_resolver = lambda _order: resolved_snapshot
    ctx = RequestContext(
        user_id="user-a", tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )
    snapshot = SimpleNamespace(
        tenant_id="tenant-a", project_id=None, baseline_id=None,
        baseline_content_hash="", source_set_snapshot_id="snapshot-1",
    )
    order = service._create_frozen_work_order(
        ctx, scope_type="knowledge_base", snapshot=snapshot, knowledge_base_name="hardware",
        template_version_id="template-1", document_schema_id="schema-1",
        document_schema_version="1", idempotency_key="turn-1:document-generation",
        execution_mode="deterministic_only",
    )
    parent_bytes = _xlsx_bytes({"A1": "Signal", "B1": "Pin", "A2": "CAN_TX"})
    child_bytes = _xlsx_bytes({"A1": "Signal", "B1": "Pin", "A2": "CAN_RX", "A3": "LIN_TX"})
    parent = DocumentArtifact(
        artifact_id="artifact-parent", tenant_id="tenant-a", work_order_id=order.work_order_id,
        run_id="run-1", output_format="xlsx", stage="approved_release",
        content_hash=hashlib.sha256(parent_bytes).hexdigest(), validation_report_id="report-1",
        integrity_manifest_id="manifest-1",
    )
    authoring_store.save_artifact(parent, parent_bytes, "xlsx")
    revision = service.revision_service.create_revision(
        ctx, task_id=order.task_id, parent_artifact_id="artifact-parent",
        request_type="field_update", request="更新接口行",
        changed_fields=["signals"],
        client_request_id="diff-1",
    )
    child = DocumentArtifact(
        artifact_id="artifact-child", tenant_id="tenant-a", work_order_id=order.work_order_id,
        run_id="run-x", output_format="xlsx", stage="review_candidate",
        content_hash=hashlib.sha256(child_bytes).hexdigest(), validation_report_id="vr-x",
        integrity_manifest_id="im-x", parent_artifact_id="artifact-parent",
    )
    authoring_store.save_artifact(child, child_bytes, "xlsx")

    completed = service.revision_service.bind_generated_child(
        order.model_copy(update={"revision_id": revision.revision_id}),
        child, revalidation_status="passed",
    )
    replay = service.revision_service.bind_generated_child(
        order.model_copy(update={"revision_id": revision.revision_id}),
        child, revalidation_status="passed",
    )

    report = completed.revalidation_result["diff_report"]
    assert report["format"] == "xlsx"
    assert report["summary"] == {"changed": 1, "added": 1, "removed": 0, "unchanged": 2}
    assert report["truncated"] is False
    assert {"location": "Review!A2", "kind": "changed", "before": "CAN_TX", "after": "CAN_RX"} in report["changes"]
    assert {"location": "Review!A3", "kind": "added", "before": "", "after": "LIN_TX"} in report["changes"]
    assert completed == replay
    assert service.revision_service.store.get(revision.revision_id).revalidation_result["diff_report"] == report


def test_diff_report_degrades_gracefully_when_bytes_unreadable(tmp_path):
    task_store = DocumentTaskStore(str(tmp_path / "tasks.db"))
    task = task_store.create_task(
        tenant_id="tenant-a", user_id="user-a", origin="workbench", created_by="user-a",
        knowledge_base_name="hardware", status="completed",
    )
    child = SimpleNamespace(
        artifact_id="artifact-child", work_order_id="wo-a", content_hash="child-hash",
        stage="review_candidate", parent_artifact_id="artifact-parent",
        output_format="xlsx", revision_id=None, status_reasons=[],
    )
    service = DocumentRevisionService(
        authoring_store=SimpleNamespace(
            get_artifact=lambda _artifact_id: SimpleNamespace(output_format="xlsx"),
            get_work_order=lambda _work_order_id: None,
            read_artifact_content=lambda _artifact_id: (_ for _ in ()).throw(OSError("missing file")),
        ),
        task_store=task_store,
        revision_store=ArtifactRevisionStore(tmp_path / "revisions.db"),
    )
    revision = service.store.create(
        task_id=task.task_id, work_order_id="wo-a", parent_artifact_id="artifact-parent",
        request_type="field_update", request="更新", input_snapshot_hash="i",
        source_snapshot_hash="s", template_version_id="t", schema_hash="h",
        client_request_id="diff-err-1",
    )

    report = service._diff_report_for(revision, child)

    assert report is not None
    assert "diff unavailable" in report["error"]
