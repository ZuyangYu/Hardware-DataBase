"""Tenant isolation for auth/KB/workspace boundaries."""

from __future__ import annotations

from pathlib import Path

from src.core.auth import AuthService
from src.core.workspace_store import WorkspaceStore


def test_auth_kb_permissions_are_tenant_scoped(tmp_path: Path):
    auth = AuthService(db_path=str(tmp_path / "auth.db"))
    tenant_a = auth.create_tenant("Tenant A")
    tenant_b = auth.create_tenant("Tenant B")
    dept_a = auth.create_department("Dept A", tenant_id=tenant_a.id)
    dept_b = auth.create_department("Dept B", tenant_id=tenant_b.id)
    user_a = auth._create_user_record(
        "user-a", "password123", department_id=dept_a.id, tenant_id=tenant_a.id
    )
    user_b = auth._create_user_record(
        "user-b", "password123", department_id=dept_b.id, tenant_id=tenant_b.id
    )

    auth.register_knowledge_base("shared-name", owner=user_a)
    auth.register_knowledge_base("shared-name", owner=user_b)

    perms_a = auth.get_kb_permissions_for_user(user_a)
    perms_b = auth.get_kb_permissions_for_user(user_b)

    assert set(perms_a) == {f"{dept_a.id}:shared-name"}
    assert set(perms_b) == {f"{dept_b.id}:shared-name"}
    assert auth.list_accessible_kbs(user_a, ["shared-name"]) == ["shared-name"]
    assert auth.list_accessible_kbs(user_b, ["shared-name"]) == ["shared-name"]


def test_workspaces_are_tenant_scoped(tmp_path: Path):
    store = WorkspaceStore(
        db_path=str(tmp_path / "workspaces.db"),
        workspace_root=str(tmp_path / "workspaces"),
        backend="sqlite",
    )
    a = store.get_or_create_for_user(user_id=7, kind="general", tenant_id="tenant-a")
    b = store.get_or_create_for_user(user_id=7, kind="general", tenant_id="tenant-b")

    assert a.id != b.id
    assert [item.id for item in store.list_workspaces(7, tenant_id="tenant-a")] == [a.id]
    assert [item.id for item in store.list_workspaces(7, tenant_id="tenant-b")] == [b.id]
    assert store.get_workspace(a.id, 7, tenant_id="tenant-a").id == a.id
    assert store.get_workspace(a.id, 7, tenant_id="tenant-b") is None
