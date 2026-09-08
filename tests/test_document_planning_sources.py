from __future__ import annotations

from dataclasses import asdict

import pytest

from src.attachments.models import AttachmentRef
from src.document_authoring.models import KnowledgeBaseSourceSnapshot
from src.document_authoring.planning.sources import FrozenSourceScope
from src.projects.models import SourceSetSnapshot


def _project_snapshot() -> SourceSetSnapshot:
    return SourceSetSnapshot(
        source_set_snapshot_id="project-snapshot-1",
        tenant_id="tenant-a",
        work_order_id="wo-1",
        project_id="project-1",
        baseline_id="baseline-1",
        baseline_content_hash="baseline-hash",
        baseline_item_ids=["item-1"],
        source_version_ids=["source-2", "source-1"],
        shared_reference_version_ids=["shared-1"],
        processing_artifact_ids=["artifact-1"],
        region_policy_versions={"source-1": "policy-1"},
        authorization_snapshot_id="auth-1",
    )


def _attachment(attachment_id: str, sha256: str) -> AttachmentRef:
    return AttachmentRef(
        attachment_id=attachment_id,
        asset_id=f"asset-{attachment_id}",
        session_id=42,
        filename=f"{attachment_id}.edf",
        media_type="application/edf",
        extension=".edf",
        size_bytes=10,
        sha256=sha256,
        usage_hint="data",
        parse_status="ready",
    )


def test_project_and_kb_adapters_expose_only_frozen_identity_scope_and_hash():
    project = FrozenSourceScope.from_project_snapshot(_project_snapshot())
    kb_snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="kb-snapshot-1",
        tenant_id="tenant-a",
        knowledge_base_name="ADAS",
        source_names=["released-1", "released-2"],
        created_by="alice",
    )
    kb = FrozenSourceScope.from_knowledge_base_snapshot(kb_snapshot)

    assert project.snapshot_hash == _project_snapshot().content_hash
    assert kb.snapshot_hash == kb_snapshot.content_hash
    assert {item.source_type for item in project.refs} >= {"baseline", "project_source"}
    assert {item.source_type for item in kb.refs} == {"knowledge_base_source"}
    assert all(set(item.model_dump()) == {"source_id", "source_type", "scope", "content_hash", "ordinal"} for item in project.refs + kb.refs)
    assert all("content" not in item.model_dump() and "path" not in item.model_dump() for item in project.refs + kb.refs)


def test_attachment_adapter_preserves_order_and_user_assertions_are_hash_only():
    refs = [_attachment("att-2", "hash-2"), _attachment("att-1", "hash-1")]
    first = FrozenSourceScope.from_attachment_refs(refs)
    second = FrozenSourceScope.from_attachment_refs([asdict(item) for item in refs])
    assert first == second
    assert [item.source_id for item in first.refs] == ["att-2", "att-1"]
    assert [item.ordinal for item in first.refs] == [0, 1]

    assertions = FrozenSourceScope.from_user_assertion_hashes(
        ["sha256:assertion-2", "sha256:assertion-1"], scope="project:project-1"
    )
    assert [item.content_hash for item in assertions.refs] == [
        "sha256:assertion-2", "sha256:assertion-1"
    ]
    assert all(item.source_type == "user_assertion" for item in assertions.refs)


def test_combined_scope_is_stable_and_rejects_raw_content_or_paths():
    attachment = asdict(_attachment("att-1", "hash-1"))
    first = FrozenSourceScope.from_inputs(
        project_snapshot=_project_snapshot(),
        attachments=[attachment],
        user_assertion_hashes=["sha256:assertion"],
    )
    second = FrozenSourceScope.from_inputs(
        project_snapshot=_project_snapshot(),
        attachments=[attachment],
        user_assertion_hashes=["sha256:assertion"],
    )
    assert first == second
    assert first.snapshot_hash.startswith("sha256:")

    with pytest.raises(ValueError, match="path|content"):
        FrozenSourceScope.from_attachment_refs([{"attachment_id": "att-1", "path": "/tmp/a"}])

    with pytest.raises(ValueError, match="assertion|content"):
        FrozenSourceScope.from_user_assertion_hashes([{"content": "raw fact"}])


def test_caller_supplied_snapshot_hash_must_match_frozen_refs():
    scope = FrozenSourceScope.from_attachment_refs([_attachment("att-1", "hash-1")])
    with pytest.raises(ValueError, match="snapshot_hash"):
        FrozenSourceScope(
            snapshot_id=scope.snapshot_id,
            snapshot_hash="sha256:wrong",
            refs=scope.refs,
            version_policy=scope.version_policy,
        )

