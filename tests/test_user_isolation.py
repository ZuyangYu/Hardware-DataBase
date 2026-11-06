"""User-level isolation for the workspace-first agent.

In this product one user is one tenant: the isolation boundary is ``user_id``.
Departments are sharing groups, not tenant containers.
"""

from __future__ import annotations

from pathlib import Path

from src.core.workspace_store import WorkspaceStore


def test_workspaces_are_user_scoped(tmp_path: Path):
    store = WorkspaceStore(
        db_path=str(tmp_path / "workspaces.db"),
        workspace_root=str(tmp_path / "workspaces"),
        backend="sqlite",
    )
    a = store.get_or_create_for_user(user_id=7, kind="general")
    b = store.get_or_create_for_user(user_id=8, kind="general")

    assert a.id != b.id
    assert [item.id for item in store.list_workspaces(7)] == [a.id]
    assert [item.id for item in store.list_workspaces(8)] == [b.id]
    assert store.get_workspace(a.id, 7).id == a.id
    assert store.get_workspace(a.id, 8) is None
