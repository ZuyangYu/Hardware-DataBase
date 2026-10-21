"""Central audit coverage for the memory HTTP boundary.

``MemoryService`` owns a domain audit table, but the cross-domain
``audit_events`` trail used by governance had no memory rows at all.  These
tests lock the route-level success/failure attribution and the rule that
memory content/reason text never enters audit metadata.
"""

from __future__ import annotations

import json

import src.settings
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import current_user
from src.api.routes.memories import get_memory_service
from src.core.app_logs import AppLogService
from src.core.auth import ROLE_SYSTEM_ADMIN, AuthUser


class _FakeMemoryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_delete = False
        self.probe = None

    def update_draft(self, **kwargs):
        if self.probe is not None:
            self.probe()
        self.calls.append(("update_draft", kwargs))
        return {
            "operation_id": "op-draft",
            "memory_id": kwargs["memory_id"],
            "status": "accepted",
            "revision": kwargs["expected_revision"] + 1,
        }

    def delete_memory(self, **kwargs):
        self.calls.append(("delete_memory", kwargs))
        if self.fail_delete:
            raise RuntimeError("stale expected_revision")
        return {"operation_id": "op-delete", "memory_id": kwargs["memory_id"]}

    def set_user_memory_settings(self, **kwargs):
        self.calls.append(("set_user_memory_settings", kwargs))
        return {
            "opt_in": kwargs["opt_in"],
            "policy_version": "v1",
            "revoke_generation": 0,
            "updated_at": "2026-01-01T00:00:00Z",
        }

    def create_memory_consent(self, **kwargs):
        self.calls.append(("create_memory_consent", kwargs))
        return {
            "consent_event_id": "consent-1",
            "session_id": kwargs["session_id"],
            "source_count": len(kwargs["message_ids"]),
            "manifest_hash": "hash",
            "policy_version": "v1",
            "revoke_generation": 0,
            "status": "active",
            "granted_at": "2026-01-01T00:00:00Z",
            "revoked_at": None,
        }

    def close(self) -> None:
        pass


def _user() -> AuthUser:
    return AuthUser(
        id=7,
        username="alice",
        role="employee",
        is_active=True,
        department_id=3,
        department_name="hw",
    )


def test_memory_mutations_are_centrally_audited(tmp_path, monkeypatch):
    db_path = str(tmp_path / "auth.db")
    monkeypatch.setattr(src.settings, "AUTH_DB_PATH", db_path)
    monkeypatch.setattr(src.settings, "AUTH_STORE_BACKEND", "sqlite")

    admin = AuthUser(
        id=99,
        username="root",
        role=ROLE_SYSTEM_ADMIN,
        is_active=True,
        department_id=None,
    )
    service = _FakeMemoryService()
    started_before_service = []

    def _probe_started() -> None:
        started_before_service.append(
            len(
                AppLogService(db_path).list_audit_events(
                    admin, action="memory.update_draft.requested"
                )
            )
        )

    service.probe = _probe_started
    app = create_app()
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[get_memory_service] = lambda: service
    try:
        with TestClient(app) as client:
            draft = client.patch(
                "/api/v1/memories/mem-1/draft",
                json={
                    "content": {"text": "SECRET-CONTENT"},
                    "expected_revision": 2,
                    "reason": "SECRET-REASON",
                    "request_id": "req-draft",
                },
            )
            assert draft.status_code == 202, draft.text

            settings = client.put(
                "/api/v1/memory-settings",
                json={"opt_in": True, "reason": "SECRET-REASON", "request_id": "req-set"},
            )
            assert settings.status_code == 200, settings.text

            consent = client.post(
                "/api/v1/conversations/9/memory-consents",
                json={
                    "message_ids": [1, 2],
                    "reason": "SECRET-REASON",
                    "request_id": "req-consent",
                },
            )
            assert consent.status_code == 202, consent.text

            service.fail_delete = True
            deleted = client.request(
                "DELETE",
                "/api/v1/memories/mem-1",
                json={
                    "expected_revision": 3,
                    "reason": "SECRET-REASON",
                    "request_id": "req-delete",
                },
            )
            assert deleted.status_code == 409, deleted.text
    finally:
        app.dependency_overrides.clear()

    # The requested row must already exist when the service is invoked; this
    # is the write-ahead property that closes the crash window.
    assert started_before_service == [1]

    logs = AppLogService(db_path)
    draft_rows = logs.list_audit_events(admin, action="memory.update_draft")
    draft_started = logs.list_audit_events(
        admin, action="memory.update_draft.requested"
    )
    assert len(draft_rows) == 1
    assert len(draft_started) == 1
    assert draft_rows[0].success is True
    assert draft_rows[0].actor_username == "alice"
    assert draft_rows[0].target_type == "memory"
    assert draft_rows[0].target_id == "mem-1"
    assert "SECRET" not in draft_rows[0].metadata_json
    assert "op-draft" in draft_rows[0].metadata_json
    finished_meta = json.loads(draft_rows[0].metadata_json)
    started_meta = json.loads(draft_started[0].metadata_json)
    assert finished_meta["phase"] == "finished"
    assert started_meta["phase"] == "requested"
    # Both rows share the correlation id, so an orphaned request is traceable.
    assert finished_meta["attempt_id"] == started_meta["attempt_id"]

    settings_rows = logs.list_audit_events(admin, action="memory.settings_update")
    assert len(settings_rows) == 1
    assert '"opt_in": true' in settings_rows[0].metadata_json

    consent_rows = logs.list_audit_events(admin, action="memory.consent_create")
    assert len(consent_rows) == 1
    assert consent_rows[0].target_id == "consent-1"
    assert '"message_count": 2' in consent_rows[0].metadata_json

    delete_rows = logs.list_audit_events(admin, action="memory.delete")
    assert len(delete_rows) == 1
    assert delete_rows[0].success is False
    assert "stale expected_revision" in delete_rows[0].error_message
    assert "SECRET" not in delete_rows[0].metadata_json

    # Write-ahead rows stay in the event table and get their own bucket; they
    # must not inflate completed success/failure counts or the action list.
    assert logs.audit_breakdown(admin) == {
        "success": 3,
        "failed": 1,
        "requested": 4,
    }
    assert logs.count_audit_events(admin) == 8
    assert all(
        not action.endswith(".requested")
        for action, _count in logs.audit_action_breakdown(admin)
    )
    assert all(
        not action.endswith(".requested")
        for action in logs.list_audit_actions(admin)
    )


def test_audit_write_failure_does_not_break_memory_mutation(tmp_path, monkeypatch):
    """The governance trail is fail-soft: an audit outage must not 500 a write."""

    monkeypatch.setattr(src.settings, "AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(src.settings, "AUTH_STORE_BACKEND", "sqlite")

    def _broken_audit(self, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(AppLogService, "record_audit", _broken_audit)
    app = create_app()
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[get_memory_service] = lambda: _FakeMemoryService()
    try:
        with TestClient(app) as client:
            response = client.patch(
                "/api/v1/memories/mem-1/draft",
                json={
                    "content": {"text": "still written"},
                    "expected_revision": 2,
                    "reason": "test",
                    "request_id": "req-fail-soft",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
