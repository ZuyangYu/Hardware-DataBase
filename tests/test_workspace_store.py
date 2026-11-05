from __future__ import annotations

from pathlib import Path

import pytest

from src.core.workspace_store import WorkspaceStore


def _store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(
        db_path=str(tmp_path / "workspaces.db"),
        workspace_root=str(tmp_path / "workspace-root"),
        backend="sqlite",
    )


def test_workspace_session_mapping_and_default_reuse(tmp_path):
    store = _store(tmp_path)
    general = store.get_or_create_for_user(user_id=7, kind="general")
    same = store.get_or_create_for_user(user_id=7, kind="general")
    knowledge = store.get_or_create_for_user(
        user_id=7, kind="knowledge", kb_name="AAA", kb_id=11
    )

    assert general.id == same.id
    assert knowledge.id != general.id
    assert knowledge.kb_name == "AAA"

    store.attach_session(general.id, 101)
    store.attach_session(general.id, 102)
    # A session can move between workspaces; the mapping is one-to-one.
    store.attach_session(knowledge.id, 102)

    assert store.get_workspace_for_session(101).id == general.id
    assert store.get_workspace_for_session(102).id == knowledge.id
    assert store.list_session_ids(general.id) == [101]
    assert store.list_session_ids(knowledge.id) == [102]


def test_workspace_artifact_versions_and_path_confinement(tmp_path):
    store = _store(tmp_path)
    workspace = store.create_workspace(user_id=7, kind="general", name="demo")
    out = Path(workspace.workspace_dir) / "out" / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("v1", encoding="utf-8")

    first = store.register_artifact(
        workspace.id, str(out), session_id=101, turn_id="turn-1"
    )
    out.write_text("v2", encoding="utf-8")
    second = store.register_artifact(
        workspace.id, str(out), session_id=101, turn_id="turn-2"
    )

    assert (first.version, second.version) == (1, 2)
    assert first.turn_id == "turn-1"
    assert second.turn_id == "turn-2"
    assert store.get_artifact(second.id).name == "report.md"
    assert len(store.list_artifacts(workspace.id)) == 2

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError):
        store.register_artifact(workspace.id, str(outside))


def test_workspace_scan_registers_new_out_files_once(tmp_path):
    store = _store(tmp_path)
    workspace = store.create_workspace(user_id=7, kind="general", name="scan")
    out = Path(workspace.workspace_dir) / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "a.md").write_text("a", encoding="utf-8")

    first = store.scan_workspace_artifacts(
        workspace.id, session_id=5, turn_id="turn-a"
    )
    second = store.scan_workspace_artifacts(
        workspace.id, session_id=5, turn_id="turn-b"
    )

    assert [item.name for item in first] == ["a.md"]
    assert second == []
    assert store.list_artifacts(workspace.id)[0].turn_id == "turn-a"


def test_workspace_queries_are_tenant_scoped(tmp_path):
    store = _store(tmp_path)
    a = store.get_or_create_for_user(user_id=7, kind="general", tenant_id="tenant-a")
    b = store.get_or_create_for_user(user_id=7, kind="general", tenant_id="tenant-b")

    assert a.id != b.id
    assert [item.id for item in store.list_workspaces(7, tenant_id="tenant-a")] == [a.id]
    assert [item.id for item in store.list_workspaces(7, tenant_id="tenant-b")] == [b.id]
    assert store.get_workspace(a.id, 7, tenant_id="tenant-a").id == a.id
    assert store.get_workspace(a.id, 7, tenant_id="tenant-b") is None
