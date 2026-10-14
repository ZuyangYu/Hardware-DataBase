"""Account creation keeps administrator grants behind existing administrator access."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.settings as settings
from src.api.deps import get_auth_service
from src.api.errors import install_error_handlers
from src.api.routes import auth as auth_routes, users
from src.core.app_logs import AppLogService
from src.core.auth import AuthService, ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN

PASSWORD = "AccountCreationTest123!"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(settings, "AUTH_DEFAULT_ADMIN_USERNAME", "bootstrap-admin")
    monkeypatch.setattr(settings, "AUTH_DEFAULT_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "")
    auth = AuthService()
    admin = auth.get_user_by_username("bootstrap-admin")
    department = auth.create_department_as(admin, "研发部")
    employee = auth.create_user_as(admin, "employee", PASSWORD, ROLE_EMPLOYEE, department.id)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_auth_service] = lambda: auth
    with TestClient(app) as client:
        yield SimpleNamespace(auth=auth, admin=admin, department=department, employee=employee, client=client)


def headers(accounts, username="bootstrap-admin"):
    login = accounts.client.post("/api/v1/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def test_administrator_can_create_a_login_capable_unmanaged_administrator(accounts):
    q = accounts
    created = q.auth.create_user_as(q.admin, "support-admin", PASSWORD, ROLE_SYSTEM_ADMIN)
    assert created.role == ROLE_SYSTEM_ADMIN and created.is_active
    assert created.department_name == "system"
    assert q.auth.get_kb_permissions_for_user(created) == {}
    session, reason = q.auth.authenticate("support-admin", PASSWORD)
    assert session is not None, reason
    with closing(q.auth._connect()) as conn:
        assert conn.execute("SELECT managed_by_env FROM users WHERE id=?", (created.id,)).fetchone()[0] == 0
        assert conn.execute("SELECT managed_by_env FROM users WHERE id=?", (q.admin.id,)).fetchone()[0] == 1
    events = AppLogService().list_audit_events(q.admin, action="create_user", keyword="support-admin")
    assert any(event.success and event.actor_username == q.admin.username for event in events)


def test_inactive_administrator_cannot_grant_an_administrator_role(accounts):
    q = accounts
    with pytest.raises(PermissionError):
        q.auth.create_user_as(replace(q.admin, is_active=False), "blocked-admin", PASSWORD, ROLE_SYSTEM_ADMIN)
    assert q.auth.get_user_by_username("blocked-admin") is None


def test_administrator_creation_rejects_a_business_department(accounts):
    q = accounts
    with pytest.raises(ValueError, match="业务部门"):
        q.auth.create_user_as(q.admin, "wrong-department", PASSWORD, ROLE_SYSTEM_ADMIN, q.department.id)
    assert q.auth.get_user_by_username("wrong-department") is None


def test_api_created_administrator_can_login_and_register_an_employee(accounts):
    q = accounts
    response = q.client.post("/api/v1/users", headers=headers(q), json={
        "username": "support-admin", "password": PASSWORD, "role": "system_admin", "department_id": None,
    })
    assert response.status_code == 200
    assert response.json()["role"] == "system_admin"
    assert response.json()["department_name"] == "system"
    created = q.client.post("/api/v1/users", headers=headers(q, "support-admin"), json={
        "username": "new-employee", "password": PASSWORD, "role": "employee", "department_id": q.department.id,
    })
    assert created.status_code == 200
    assert created.json()["role"] == "employee" and created.json()["department_id"] == q.department.id


@pytest.mark.parametrize("actor", [None, "employee"])
def test_anonymous_and_employee_requests_cannot_create_administrators(accounts, actor):
    response = accounts.client.post("/api/v1/users", headers=headers(accounts, actor) if actor else {}, json={
        "username": "blocked-admin", "password": PASSWORD, "role": "system_admin",
    })
    assert response.status_code == (401 if actor is None else 403)
    assert accounts.auth.get_user_by_username("blocked-admin") is None


@pytest.mark.parametrize("changes, status", [
    ({"password": "short"}, 400),
    ({"role": "superuser"}, 422),
    ({"role": "employee", "department_id": None}, 400),
])
def test_invalid_account_requests_do_not_create_users(accounts, changes, status):
    response = accounts.client.post("/api/v1/users", headers=headers(accounts), json={
        "username": "invalid-account", "password": PASSWORD, "role": "system_admin", **changes,
    })
    assert response.status_code == status
    assert accounts.auth.get_user_by_username("invalid-account") is None


def test_duplicate_username_does_not_replace_the_deployment_administrator(accounts):
    q = accounts
    response = q.client.post("/api/v1/users", headers=headers(q), json={
        "username": q.admin.username, "password": "AnotherTestPassword123!", "role": "system_admin",
    })
    assert response.status_code == 400
    assert "用户名已存在" in response.json()["detail"]
    assert q.auth.authenticate(q.admin.username, PASSWORD)[0] is not None


def test_concurrent_duplicate_creation_returns_one_account_and_a_clear_error(accounts):
    q = accounts

    def create():
        try:
            return q.auth.create_user_as(q.admin, "duplicate-admin", PASSWORD, ROLE_SYSTEM_ADMIN)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert [result for result in results if isinstance(result, str)] == ["用户名已存在"]
