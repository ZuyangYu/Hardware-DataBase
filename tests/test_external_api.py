from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import src.settings as settings
from src import external_settings
from src.api import external_support
from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.api.routes.external import get_asset_service
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import CircuitDesign, CircuitModule, ComponentInstance, Net, Pin, PinRef
from src.circuit.store import CircuitStore
from src.core.auth import AuthService, ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN
from src.core.doc_assets import DocumentAssetService
from src.pipelines.document_rag.schemas import (
    Evidence,
    IngestResult,
    ParseResult,
    ParsedChunk,
    ScopedDocumentRetrievalOutcome,
)
from src.pipelines.document_store_sqlite import PipelineDocumentStore
from src.pipelines.spreadsheet.sql_materializer import materialize_sheet
from src.pipelines.spreadsheet.table_store import TableIndexStore
from src.services.document_archive import DocumentArchiveManager
from src.services.spreadsheet_index_service import SpreadsheetIndexService


@pytest.fixture(autouse=True)
def _external_test_env(monkeypatch):
    # Keep the security tests independent of a developer's local .env.
    monkeypatch.setenv("OBS_ENVIRONMENT", "development")
    monkeypatch.setenv("REDIS_REQUIRED", "false")
    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_ALL_KBS_MODE_ENABLED", False)


class _FakeBackend:
    def __init__(self, store: PipelineDocumentStore, evidence_record_id: int):
        self.store = store
        self.evidence_record_id = evidence_record_id
        self.last_contract = None

    def retrieve_with_scope(self, _kb_name, _query, **kwargs):
        self.last_contract = kwargs["contract"]
        return ScopedDocumentRetrievalOutcome(
            "success_with_hits",
            evidence=[
                Evidence(
                    id="internal-ragflow-chunk-id",
                    content="Absolute maximum rating: 36 V",
                    score=0.91,
                    metadata={"local_record_id": self.evidence_record_id},
                )
            ],
        )

    def get_parse_result(self, _kb_name, document_id, **_kwargs):
        return ParseResult(
            document_id=document_id,
            file_name="ABC.pdf",
            chunk_count=2,
            chunks=[
                ParsedChunk(
                    index=0,
                    content="Absolute maximum rating: 36 V",
                    metadata={"chunk_id": "internal-chunk-1", "page": 4},
                ),
                ParsedChunk(
                    index=1,
                    content="Operating temperature: -40 to 125 C",
                    metadata={"chunk_id": "internal-chunk-2"},
                ),
            ],
        )


def _setup(tmp_path):
    auth = AuthService(str(tmp_path / "auth.db"))
    admin = auth.get_user_by_username("admin")
    department = auth.create_department("engineering")
    user = auth.create_user_as(
        admin,
        "alice",
        "AlicePassword123!",
        ROLE_EMPLOYEE,
        department.id,
    )
    auth.register_knowledge_base("components", user)
    kb = auth.list_accessible_knowledge_base_identities(user)[0]
    kb_ref = auth.ensure_external_kb_ref(kb.kb_id)

    store = PipelineDocumentStore(str(tmp_path / "pipeline.db"))
    content_hash = hashlib.sha256(b"source").hexdigest()
    store.upsert_document(
        "components",
        "ABC.pdf",
        "design",
        "dataset-1",
        "remote-document-1",
        "docs",
        str(department.id),
        "alice",
        kb_id=kb.kb_id,
        status="parsed",
        original_file_name="ABC.pdf",
        file_size=123,
        content_hash=content_hash,
    )
    record = store.list_documents("components", str(department.id))[0]
    backend = _FakeBackend(store, record.id)

    def fake_upload(file_paths, _kb_name, _ctx, source_group=None, progress_callback=None):
        for path in file_paths:
            with open(path, "rb") as handle:
                payload = handle.read()
            name = os.path.basename(path)
            digest = hashlib.sha256(payload).hexdigest()
            store.upsert_document(
                "components",
                name,
                "design",
                "dataset-upload",
                f"remote-upload-{digest[:16]}",
                source_group or "docs",
                str(department.id),
                "alice",
                kb_id=kb.kb_id,
                status="parsed",
                original_file_name=name,
                file_size=len(payload),
                content_hash=digest,
            )
        return IngestResult(
            success_count=len(file_paths),
            total_count=len(file_paths),
        )

    pipeline = SimpleNamespace(backend=backend, upload_files=fake_upload)
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    return app, auth, user, kb_ref, content_hash, backend


def test_employee_key_is_session_managed_and_external_scope_is_opaque(tmp_path):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/login",
            json={"username": "alice", "password": "AlicePassword123!"},
        )
        assert login.status_code == 200
        session_headers = {"Authorization": f"Bearer {login.json()['token']}"}

        options = client.get("/api/v1/me/api-keys/available-kbs", headers=session_headers)
        assert options.status_code == 200
        assert options.json()[0]["kb_ref"] == kb_ref.kb_ref
        assert "kb_id" not in options.text

        created = client.post(
            "/api/v1/me/api-keys",
            headers=session_headers,
            json={
                "name": "consumer",
                "scopes": ["read", "invoke"],
                "kb_access_mode": "selected_kbs",
                "kb_refs": [kb_ref.kb_ref],
            },
        )
        assert created.status_code == 201
        secret = created.json()["api_key"]
        assert secret.startswith("hdbk_")
        assert secret not in client.get("/api/v1/me/api-keys", headers=session_headers).text

        listed = client.get("/api/v1/me/api-keys", headers=session_headers)
        assert listed.status_code == 200
        assert listed.json()[0]["kb_refs"] == [kb_ref.kb_ref]

        external_headers = {"X-HDB-API-Key": secret}
        capabilities = client.get(
            "/api/v1/external/capabilities",
            headers={**external_headers, "X-Request-ID": "bad id with spaces"},
        )
        assert capabilities.status_code == 200
        assert capabilities.headers["X-Request-ID"] == capabilities.json()["request_id"]
        assert capabilities.headers["X-Request-ID"] != "bad id with spaces"

        # API keys cannot cross into session-only management or normal KB APIs.
        assert client.get("/api/v1/me/api-keys", headers=external_headers).status_code == 401
        assert client.get("/api/v1/kbs", headers=external_headers).status_code == 401
        auth.revoke_employee_api_key(user, created.json()["key_id"])
        revoked = client.get(
            "/api/v1/external/capabilities", headers=external_headers
        )
        assert revoked.status_code == 401
        assert revoked.json()["error"]["code"] == "KEY_REVOKED"


def test_external_retrieval_passes_non_empty_allowed_record_ids_and_filters_internal_ids(tmp_path):
    app, auth, user, kb_ref, content_hash, backend = _setup(tmp_path)
    key = auth.create_employee_api_key(
        user,
        name="consumer",
        scopes=["read", "invoke"],
        kb_access_mode="selected_kbs",
        kb_refs=[kb_ref.kb_ref],
    )
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        files = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files", headers=headers
        )
        assert files.status_code == 200
        item = files.json()["items"][0]
        file_ref = item["file_ref"]
        assert item["status"] == "ready"
        assert "processor_kind" not in item
        assert "dataset_kind" not in item
        assert "local_path" not in files.text
        ready_files = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files",
            headers=headers,
            params={"status": "ready"},
        )
        assert ready_files.status_code == 200
        assert ready_files.json()["total"] == 1
        assert client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files",
            headers=headers,
            params={"status": "failed"},
        ).json()["total"] == 0

        response = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/retrieve",
            headers=headers,
            json={
                "query": "maximum rating",
                "top_k": 5,
                "scope": {
                    "mode": "files",
                    "file_refs": [file_ref],
                    "expected_content_hashes": {file_ref: f"sha256:{content_hash}"},
                },
                "capabilities": ["document_claim_lookup"],
            },
        )
        assert response.status_code == 200
        assert backend.last_contract.allowed_record_ids
        assert backend.last_contract.mode == "frozen"
        assert "internal-ragflow-chunk-id" not in response.text
        assert "local_record_id" not in response.text
        assert "remote-document-1" not in response.text
        assert response.json()["results"][0]["source"]["file_ref"] == file_ref

        whole_kb = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/retrieve",
            headers=headers,
            json={"query": "maximum rating", "top_k": 5, "scope": {"mode": "kb"}},
        )
        assert whole_kb.status_code == 200
        assert backend.last_contract.allowed_record_ids
        assert backend.last_contract.mode == "frozen"


def test_external_retrieval_rejects_unknown_file_without_broadening_scope(tmp_path):
    app, auth, user, kb_ref, _content_hash, backend = _setup(tmp_path)
    key = auth.create_employee_api_key(
        user,
        name="consumer",
        scopes=["read", "invoke"],
        kb_access_mode="selected_kbs",
        kb_refs=[kb_ref.kb_ref],
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/retrieve",
            headers={"X-HDB-API-Key": key["api_key"]},
            json={
                "query": "anything",
                "scope": {"mode": "files", "file_refs": ["fileref_unknown"]},
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert backend.last_contract is None


def _create_key(auth: AuthService, user, kb_ref) -> dict:
    return auth.create_employee_api_key(
        user,
        name="consumer",
        scopes=["read", "invoke"],
        kb_access_mode="selected_kbs",
        kb_refs=[kb_ref.kb_ref],
    )


def _create_write_key(auth: AuthService, user, kb_ref) -> dict:
    return auth.create_employee_api_key(
        user,
        name="consumer upload",
        scopes=["write"],
        kb_access_mode="selected_kbs",
        kb_refs=[kb_ref.kb_ref],
    )


def test_external_hash_mismatch_fails_before_backend_retrieval(tmp_path):
    app, auth, user, kb_ref, _content_hash, backend = _setup(tmp_path)
    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        files = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files", headers=headers
        )
        file_ref = files.json()["items"][0]["file_ref"]
        response = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/retrieve",
            headers=headers,
            json={
                "query": "maximum rating",
                "scope": {
                    "mode": "files",
                    "file_refs": [file_ref],
                    "expected_content_hashes": {file_ref: "sha256:" + "0" * 64},
                },
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SOURCE_HASH_CONFLICT"
        assert backend.last_contract is None


def test_selected_kbs_rejects_unselected_kb(tmp_path):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    auth.register_knowledge_base("other", user)
    other = next(
        identity
        for identity in auth.list_accessible_knowledge_base_identities(user)
        if identity.kb_name == "other"
    )
    other_ref = auth.ensure_external_kb_ref(other.kb_id)
    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/external/kbs/{other_ref.kb_ref}/files",
            headers={"X-HDB-API-Key": key["api_key"]},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "KEY_SCOPE_DENIED"


def test_admin_scope_limits_and_write_keys(tmp_path):
    auth = AuthService(str(tmp_path / "auth.db"))
    admin = auth.get_user_by_username("admin")
    department = auth.create_department("engineering")
    employee = auth.create_user_as(
        admin, "alice", "AlicePassword123!", ROLE_EMPLOYEE, department.id
    )
    auth.register_knowledge_base("components", employee)
    kb = auth.list_accessible_knowledge_base_identities(employee)[0]
    kb_ref = auth.ensure_external_kb_ref(kb.kb_id)
    second_admin = auth.create_user_as(
        admin, "root2", "AdminPassword123!", ROLE_SYSTEM_ADMIN, None
    )

    with pytest.raises(PermissionError):
        auth.create_employee_api_key(
            second_admin,
            name="admin-key",
            scopes=["read"],
            kb_access_mode="selected_kbs",
            kb_refs=[kb_ref.kb_ref],
        )
    with pytest.raises(PermissionError):
        auth.list_employee_api_keys(second_admin)
    write_key = auth.create_employee_api_key(
        employee,
        name="write-key",
        scopes=["write"],
        kb_access_mode="selected_kbs",
        kb_refs=[kb_ref.kb_ref],
    )
    assert write_key["scopes"] == ["write"]
    with pytest.raises(ValueError, match="selected_kbs"):
        auth.create_employee_api_key(
            employee,
            name="empty-key",
            scopes=["read"],
            kb_access_mode="selected_kbs",
            kb_refs=[],
        )
    with pytest.raises(ValueError, match="all_current_accessible_kbs"):
        auth.create_employee_api_key(
            employee,
            name="poc-key",
            scopes=["read"],
            kb_access_mode="all_current_accessible_kbs",
            kb_refs=[],
        )
    with pytest.raises(ValueError, match="不能提交 kb_refs"):
        auth.create_employee_api_key(
            employee,
            name="poc-key-with-refs",
            scopes=["read"],
            kb_access_mode="all_current_accessible_kbs",
            kb_refs=[kb_ref.kb_ref],
        )


def test_deleting_kb_cleans_external_mappings_without_fk_failure(tmp_path):
    app, auth, user, kb_ref, _content_hash, backend = _setup(tmp_path)
    _create_key(auth, user, kb_ref)
    record = backend.store.list_documents("components", str(user.department_id))[0]
    file_ref = auth.ensure_external_file_ref(kb_ref.kb_id, record.id)
    auth.delete_knowledge_base_record(
        "components", department_id=user.department_id, kb_id=kb_ref.kb_id
    )
    assert auth.get_external_kb_ref(kb_ref.kb_ref) is None
    assert auth.get_external_file_ref(file_ref.file_ref) is None


def test_external_request_limit_and_admin_session(tmp_path, monkeypatch):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    key = _create_key(auth, user, kb_ref)
    external_headers = {"X-HDB-API-Key": key["api_key"]}
    admin = auth.get_user_by_username("admin")
    auth.create_user_as(
        admin, "root2", "AdminPassword123!", ROLE_SYSTEM_ADMIN, None
    )
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/login",
            json={"username": "alice", "password": "AlicePassword123!"},
        )
        assert login.status_code == 200
        session_headers = {"Authorization": f"Bearer {login.json()['token']}"}
        admin_login = client.post(
            "/api/v1/login",
            json={"username": "root2", "password": "AdminPassword123!"},
        )
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}
        # Key management is available to the employee without an env switch.
        assert (
            client.get("/api/v1/me/api-keys", headers=session_headers).status_code
            == 200
        )
        assert client.get("/api/v1/me/api-keys", headers=admin_headers).status_code == 403

        monkeypatch.setattr(external_settings, "HDB_EXTERNAL_MAX_REQUEST_BYTES", 16)
        too_large = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/retrieve",
            headers=external_headers,
            json={"query": "x" * 100, "scope": {"mode": "kb"}},
        )
        assert too_large.status_code == 422
        assert too_large.json()["error"]["code"] == "INVALID_REQUEST"
        assert too_large.headers["X-Request-ID"] == too_large.json()["request_id"]


def test_external_rate_limit_returns_429(tmp_path, monkeypatch):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    key = _create_key(auth, user, kb_ref)
    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_RATE_LIMIT_BACKEND", "local")
    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_RATE_LIMIT_PER_MINUTE", 1)
    # The limiter is process-wide by design; isolate this test's fixed user id.
    monkeypatch.setattr(external_support, "_LOCAL_LIMITER", external_support._LocalRateLimiter())
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        assert (
            client.get("/api/v1/external/capabilities", headers=headers).status_code
            == 200
        )
        limited = client.get("/api/v1/external/capabilities", headers=headers)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert limited.headers["Retry-After"] == "60"


def test_expired_key_returns_key_expired(tmp_path):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    key = _create_key(auth, user, kb_ref)
    with auth._connect() as conn:
        conn.execute(
            "UPDATE employee_api_keys SET expires_at = ? WHERE public_key_id = ?",
            ("2000-01-01T00:00:00+00:00", key["key_id"]),
        )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/external/capabilities",
            headers={"X-HDB-API-Key": key["api_key"]},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "KEY_EXPIRED"


def test_poc_all_kb_mode_is_invalidated_when_switch_off(tmp_path, monkeypatch):
    app, auth, user, _kb_ref, _content_hash, _backend = _setup(tmp_path)
    monkeypatch.setattr(settings, "OBS_ENVIRONMENT", "development")
    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_ALL_KBS_MODE_ENABLED", True)
    key = auth.create_employee_api_key(
        user,
        name="poc",
        scopes=["read", "invoke"],
        kb_access_mode="all_current_accessible_kbs",
        kb_refs=[],
    )
    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_ALL_KBS_MODE_ENABLED", False)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/external/capabilities",
            headers={"X-HDB-API-Key": key["api_key"]},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "KEY_SCOPE_DENIED"


def test_production_external_settings_fail_closed(monkeypatch):
    monkeypatch.setenv("OBS_ENVIRONMENT", "production")
    monkeypatch.setenv("REDIS_REQUIRED", "true")
    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_ALL_KBS_MODE_ENABLED", False)
    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_RATE_LIMIT_BACKEND", "local")
    with pytest.raises(RuntimeError, match="local"):
        external_settings.validate_external_settings()

    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setattr(external_settings, "HDB_EXTERNAL_ALL_KBS_MODE_ENABLED", True)
    with pytest.raises(RuntimeError, match="ALL_KBS"):
        external_settings.validate_external_settings()


def test_whole_kb_scope_rejects_structured_records(tmp_path):
    app, auth, user, kb_ref, _content_hash, backend = _setup(tmp_path)
    backend.store.upsert_document(
        "components",
        "table.xlsx",
        "design",
        "dataset-structured",
        "remote-structured-1",
        "docs",
        str(user.department_id),
        "alice",
        kb_id=kb_ref.kb_id,
        status="parsed",
        original_file_name="table.xlsx",
        file_size=10,
        content_hash=hashlib.sha256(b"table").hexdigest(),
        processor_kind="spreadsheet_table",
    )
    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/retrieve",
            headers={"X-HDB-API-Key": key["api_key"]},
            json={"query": "anything", "scope": {"mode": "kb"}},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "CAPABILITY_NOT_ENABLED"
        assert backend.last_contract is None


def test_external_chunks_are_paginated_and_whitelisted(tmp_path):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        files = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files", headers=headers
        )
        file_ref = files.json()["items"][0]["file_ref"]
        response = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files/{file_ref}/chunks",
            headers=headers,
            params={"offset": 0, "limit": 1},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["chunk_count"] == 2
        assert payload["offset"] == 0
        assert payload["limit"] == 1
        assert len(payload["items"]) == 1
        assert payload["items"][0]["index"] == 0
        assert payload["items"][0]["content"] == "Absolute maximum rating: 36 V"
        assert "internal-chunk" not in response.text
        assert "document_id" not in response.text


def test_external_download_is_scoped_and_hash_checked(tmp_path, monkeypatch):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    archive_root = tmp_path / "archives"
    monkeypatch.setattr(settings, "PIPELINE_ARCHIVE_ROOT", str(archive_root))
    archive = DocumentArchiveManager()
    target_dir = os.path.join(
        archive.kb_path(
            "components",
            create=True,
            department_id=user.department_id,
        ),
        "docs",
    )
    os.makedirs(target_dir, exist_ok=True)
    archived_path = os.path.join(target_dir, "ABC.pdf")
    with open(archived_path, "wb") as handle:
        handle.write(b"source")

    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        files = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files", headers=headers
        )
        item = files.json()["items"][0]
        assert item["download_available"] is True
        file_ref = item["file_ref"]
        downloaded = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files/{file_ref}/download",
            headers=headers,
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"source"
        assert downloaded.headers["X-Content-Type-Options"] == "nosniff"
        assert "ABC.pdf" in downloaded.headers["content-disposition"]
        assert str(archive_root) not in downloaded.text

        with open(archived_path, "wb") as handle:
            handle.write(b"tampered")
        changed = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files/{file_ref}/download",
            headers=headers,
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "SOURCE_HASH_CONFLICT"


def test_api_key_rotation_and_allowlist_narrowing(tmp_path):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    auth.register_knowledge_base("other", user)
    other = next(
        identity
        for identity in auth.list_accessible_knowledge_base_identities(user)
        if identity.kb_name == "other"
    )
    other_ref = auth.ensure_external_kb_ref(other.kb_id)
    key = auth.create_employee_api_key(
        user,
        name="two-kb-key",
        scopes=["read", "invoke"],
        kb_access_mode="selected_kbs",
        kb_refs=[kb_ref.kb_ref, other_ref.kb_ref],
    )
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/login",
            json={"username": "alice", "password": "AlicePassword123!"},
        )
        session_headers = {"Authorization": f"Bearer {login.json()['token']}"}
        rotated = client.post(
            f"/api/v1/me/api-keys/{key['key_id']}/rotate",
            headers=session_headers,
            json={},
        )
        assert rotated.status_code == 200
        rotated_payload = rotated.json()
        assert rotated_payload["rotated_from"] == key["key_id"]
        assert set(rotated_payload["kb_refs"]) == {
            kb_ref.kb_ref,
            other_ref.kb_ref,
        }
        assert client.get(
            "/api/v1/external/capabilities",
            headers={"X-HDB-API-Key": key["api_key"]},
        ).status_code == 401
        new_headers = {"X-HDB-API-Key": rotated_payload["api_key"]}
        assert client.get(
            "/api/v1/external/capabilities", headers=new_headers
        ).status_code == 200

        narrowed = client.patch(
            f"/api/v1/me/api-keys/{rotated_payload['key_id']}",
            headers=session_headers,
            json={"kb_refs": [kb_ref.kb_ref]},
        )
        assert narrowed.status_code == 200
        assert narrowed.json()["kb_refs"] == [kb_ref.kb_ref]
        assert client.get(
            f"/api/v1/external/kbs/{other_ref.kb_ref}/files",
            headers=new_headers,
        ).status_code == 403

        expand = client.patch(
            f"/api/v1/me/api-keys/{rotated_payload['key_id']}",
            headers=session_headers,
            json={"kb_refs": [kb_ref.kb_ref, other_ref.kb_ref]},
        )
        assert expand.status_code == 422


def test_api_key_rotation_can_replace_a_key_at_active_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(external_settings, "HDB_API_KEY_MAX_ACTIVE_PER_USER", 2)
    _app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    first = auth.create_employee_api_key(
        user,
        name="first",
        scopes=["read"],
        kb_access_mode="selected_kbs",
        kb_refs=[kb_ref.kb_ref],
    )
    auth.create_employee_api_key(
        user,
        name="second",
        scopes=["read"],
        kb_access_mode="selected_kbs",
        kb_refs=[kb_ref.kb_ref],
    )

    rotated = auth.rotate_employee_api_key(user, first["key_id"])

    assert rotated is not None
    listed = auth.list_employee_api_keys(user)
    assert sum(item["status"] == "active" for item in listed) == 2
    assert any(item["key_id"] == first["key_id"] and item["status"] == "revoked" for item in listed)


def test_api_key_expiry_has_a_finite_server_enforced_maximum(tmp_path):
    app, _auth, _user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/login",
            json={"username": "alice", "password": "AlicePassword123!"},
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        too_long = client.post(
            "/api/v1/me/api-keys",
            headers=headers,
            json={
                "name": "too-long",
                "scopes": ["read"],
                "kb_access_mode": "selected_kbs",
                "kb_refs": [kb_ref.kb_ref],
                "expires_in_days": 366,
            },
        )
        assert too_long.status_code == 422

        maximum = client.post(
            "/api/v1/me/api-keys",
            headers=headers,
            json={
                "name": "maximum-validity",
                "scopes": ["read"],
                "kb_access_mode": "selected_kbs",
                "kb_refs": [kb_ref.kb_ref],
                "expires_in_days": 365,
            },
        )
        assert maximum.status_code == 201
        assert maximum.json()["status"] == "active"


def test_external_asset_index_and_version_registration(tmp_path):
    app, auth, user, kb_ref, _content_hash, backend = _setup(tmp_path)
    asset_service = DocumentAssetService(str(tmp_path / "auth.db"))
    app.dependency_overrides[get_asset_service] = lambda: asset_service
    first_record = backend.store.list_documents(
        "components", str(user.department_id)
    )[0]
    asset_service.create_asset(
        kb_id=kb_ref.kb_id,
        kb_name="components",
        department_id=int(user.department_id),
        actor_user_id=int(user.id),
        title="ABC123 Datasheet",
        doc_no="ABC123",
        category="datasheet",
        manufacturer="Acme Semiconductor",
        mpn="ABC123",
        revision="Rev C",
        initial_version={
            "file_id": str(first_record.id),
            "file_name": "ABC.pdf",
            "content_hash": first_record.content_hash,
            "parse_status": "completed",
        },
    )
    read_key = _create_key(auth, user, kb_ref)
    write_key = _create_write_key(auth, user, kb_ref)
    with TestClient(app) as client:
        listed = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/assets",
            headers={"X-HDB-API-Key": read_key["api_key"]},
        )
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["total"] == 1
        asset_ref = payload["items"][0]["asset_ref"]
        assert payload["items"][0]["title"] == "ABC123 Datasheet"
        assert payload["items"][0]["manufacturer"] == "Acme Semiconductor"
        assert payload["items"][0]["mpn"] == "ABC123"
        assert payload["items"][0]["revision"] == "Rev C"
        assert "id" not in payload["items"][0]

        detail = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/assets/{asset_ref}",
            headers={"X-HDB-API-Key": read_key["api_key"]},
        )
        assert detail.status_code == 200
        assert len(detail.json()["versions"]) == 1
        assert detail.json()["versions"][0]["file_ref"]
        assert "file_id" not in detail.text

        with open(tmp_path / "v2.pdf", "wb") as handle:
            handle.write(b"version two")
        backend.store.upsert_document(
            "components",
            "ABC-v2.pdf",
            "design",
            "dataset-1",
            "remote-document-v2",
            "docs",
            str(user.department_id),
            "alice",
            kb_id=kb_ref.kb_id,
            status="parsed",
            original_file_name="ABC-v2.pdf",
            file_size=11,
            content_hash=hashlib.sha256(b"version two").hexdigest(),
        )
        second_record = next(
            record
            for record in backend.store.list_documents(
                "components", str(user.department_id)
            )
            if record.original_file_name == "ABC-v2.pdf"
        )
        second_ref = auth.ensure_external_file_ref(
            kb_ref.kb_id, second_record.id
        ).file_ref
        added = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/assets/{asset_ref}/versions",
            headers={"X-HDB-API-Key": write_key["api_key"]},
            json={"file_ref": second_ref, "note": "v2 datasheet"},
        )
        assert added.status_code == 200
        versions = added.json()["versions"]
        assert len(versions) == 2
        assert versions[-1]["version_no"] == 2
        assert versions[-1]["state"] == "effective"
        assert "file_id" not in added.text


def test_external_source_scope_evidence_persistence_and_validation(tmp_path):
    app, auth, user, kb_ref, content_hash, backend = _setup(tmp_path)
    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        files = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files", headers=headers
        )
        file_ref = files.json()["items"][0]["file_ref"]
        scope = client.post(
            "/api/v1/external/source-scopes",
            headers=headers,
            json={
                "kb_ref": kb_ref.kb_ref,
                "file_refs": [file_ref],
                "ttl_seconds": 300,
            },
        )
        assert scope.status_code == 201
        scope_payload = scope.json()
        scope_ref = scope_payload["scope_ref"]
        assert scope_payload["files"][0]["file_ref"] == file_ref
        assert scope_payload["files"][0]["content_hash"] == f"sha256:{content_hash}"

        retrieved = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/retrieve",
            headers=headers,
            json={
                "query": "maximum rating",
                "scope": {"mode": "files", "file_refs": [file_ref]},
                "source_scope_ref": scope_ref,
            },
        )
        assert retrieved.status_code == 200
        evidence = retrieved.json()["results"][0]
        evidence_id = evidence["evidence_id"]
        assert evidence_id.startswith("evidence_")

        stored = client.get(
            f"/api/v1/external/evidence/{evidence_id}", headers=headers
        )
        assert stored.status_code == 200
        assert stored.json()["content"] == "Absolute maximum rating: 36 V"
        assert stored.json()["status"] == "current"
        assert stored.json()["source"]["file_ref"] == file_ref

        evidence_content_hash = hashlib.sha256(
            stored.json()["content"].encode("utf-8")
        ).hexdigest()
        validated = client.post(
            "/api/v1/external/evidence/validate",
            headers=headers,
            json={
                "items": [
                    {
                        "evidence_id": evidence_id,
                        "required_terms": ["36 V"],
                        "required_fields": ["rating"],
                        "expected_source_hash": content_hash,
                        "expected_content_hash": evidence_content_hash,
                    }
                ]
            },
        )
        assert validated.status_code == 200
        assert validated.json()["items"][0]["status"] == "supported"
        assert validated.json()["items"][0]["supported"] is True

        wrong_source = client.post(
            "/api/v1/external/evidence/validate",
            headers=headers,
            json={
                "items": [
                    {
                        "evidence_id": evidence_id,
                        "expected_source_hash": "0" * 64,
                    }
                ]
            },
        )
        assert wrong_source.json()["items"][0]["status"] == "source_changed"

        other_key = _create_key(auth, user, kb_ref)
        other_headers = {"X-HDB-API-Key": other_key["api_key"]}
        assert client.get(
            f"/api/v1/external/evidence/{evidence_id}", headers=other_headers
        ).status_code == 404
        other_validate = client.post(
            "/api/v1/external/evidence/validate",
            headers=other_headers,
            json={"items": [{"evidence_id": evidence_id}]},
        )
        assert other_validate.json()["items"][0]["status"] == "not_found"

        missing = client.post(
            "/api/v1/external/evidence/validate",
            headers=headers,
            json={
                "items": [
                    {
                        "evidence_id": evidence_id,
                        "required_terms": ["not-present"],
                    }
                ]
            },
        )
        assert missing.json()["items"][0]["status"] == "missing_terms"

        record = backend.store.list_documents(
            "components", str(user.department_id)
        )[0]
        backend.store.upsert_document(
            "components",
            record.document_name,
            record.dataset_kind,
            record.dataset_id,
            record.document_id,
            record.source_group,
            str(user.department_id),
            "alice",
            kb_id=kb_ref.kb_id,
            status="parsed",
            original_file_name=record.original_file_name,
            file_size=record.file_size,
            content_hash=hashlib.sha256(b"changed").hexdigest(),
        )
        changed = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/retrieve",
            headers=headers,
            json={
                "query": "maximum rating",
                "scope": {"mode": "files", "file_refs": [file_ref]},
                "source_scope_ref": scope_ref,
            },
        )
        assert changed.status_code == 409
        stale = client.post(
            "/api/v1/external/evidence/validate",
            headers=headers,
            json={"items": [{"evidence_id": evidence_id}]},
        )
        assert stale.json()["items"][0]["status"] == "source_changed"


def test_external_batch_retrieve_isolates_item_failures(tmp_path):
    app, auth, user, kb_ref, content_hash, _backend = _setup(tmp_path)
    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        files = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files", headers=headers
        )
        file_ref = files.json()["items"][0]["file_ref"]
        good_scope = {
            "mode": "files",
            "file_refs": [file_ref],
            "expected_content_hashes": {file_ref: f"sha256:{content_hash}"},
        }
        response = client.post(
            "/api/v1/external/evidence/batch-retrieve",
            headers=headers,
            json={
                "items": [
                    {
                        "kb_ref": kb_ref.kb_ref,
                        "query": "maximum rating",
                        "scope": good_scope,
                    },
                    {
                        "kb_ref": kb_ref.kb_ref,
                        "query": "missing",
                        "scope": {
                            "mode": "files",
                            "file_refs": ["fileref_missing"],
                        },
                    },
                ]
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "partial"
        assert payload["items"][0]["status"] == "completed"
        assert payload["items"][0]["results"]
        assert payload["items"][1]["status"] == "failed"
        assert payload["items"][1]["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_external_table_schema_and_readonly_query(tmp_path, monkeypatch):
    app, auth, user, kb_ref, _content_hash, backend = _setup(tmp_path)
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    table_hash = hashlib.sha256(b"table source").hexdigest()
    backend.store.upsert_document(
        "components",
        "spec.xlsx",
        "design",
        "dataset-table",
        "remote-table-1",
        "docs",
        str(user.department_id),
        "alice",
        kb_id=kb_ref.kb_id,
        status="parsed",
        original_file_name="spec.xlsx",
        file_size=10,
        content_hash=table_hash,
        processor_kind="spreadsheet_table",
    )
    record = next(
        item
        for item in backend.store.list_documents(
            "components", str(user.department_id)
        )
        if item.original_file_name == "spec.xlsx"
    )
    service = SpreadsheetIndexService()
    table_store = TableIndexStore(
        service.db_path(user.department_id, "components", create=True)
    )
    with table_store._connect() as conn:
        conn.execute(
            """
            INSERT INTO table_documents (
                record_id, kb_id, kb_name, department_id, document_name,
                source_group, local_path, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                kb_ref.kb_id,
                "components",
                str(user.department_id),
                "spec.xlsx",
                "docs",
                "",
                table_hash,
            ),
        )
        materialize_sheet(
            conn,
            record.id,
            "Spec",
            [
                {"values": {"Voltage": "36"}, "raw_values": {"Voltage": "36"}},
                {"values": {"Voltage": "24"}, "raw_values": {"Voltage": "24"}},
            ],
            table_seq=1,
        )
    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        schema = client.get(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/tables/schema",
            headers=headers,
        )
        assert schema.status_code == 200
        body = schema.json()
        assert len(body["tables"]) == 1
        table_ref = body["tables"][0]["table_ref"]
        assert body["tables"][0]["file_name"] == "spec.xlsx"
        assert body["tables"][0]["columns"][0]["header"] == "Voltage"
        assert table_ref.startswith("tableref_")
        assert f"t_{record.id}_" not in table_ref

        queried = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/tables/query",
            headers=headers,
            json={
                "sql": (
                    f"SELECT col_1 AS voltage FROM {table_ref} "
                    "WHERE col_1 > 30 ORDER BY col_1"
                )
            },
        )
        assert queried.status_code == 200
        result = queried.json()
        assert result["row_count"] == 1
        assert result["rows"][0]["voltage"] == 36
        assert table_ref not in str(result["rows"])

        physical = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/tables/query",
            headers=headers,
            json={"sql": f"SELECT * FROM t_{record.id}_1"},
        )
        assert physical.status_code == 422
        assert physical.json()["error"]["code"] == "INVALID_REQUEST"


def test_external_circuit_query_is_department_scoped(tmp_path, monkeypatch):
    app, auth, user, kb_ref, _content_hash, _backend = _setup(tmp_path)
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    store = CircuitStore()
    design = CircuitDesign(
        design_id="main-board",
        kb_name="components",
        instances=[
            ComponentInstance(
                refdes="U1",
                library_cell="MCU",
                part_number="ABC123",
                pins=[Pin("1", "VCC")],
            )
        ],
        nets=[Net("VCC", [PinRef("U1", "1")], net_type="power")],
        modules=[
            CircuitModule(
                module_id="power",
                name="Power",
                strategy="fixture",
                instances=["U1"],
                nets=["VCC"],
            )
        ],
    )
    store.save(design)
    CircuitIndexService(store=store)._write_metadata(  # noqa: SLF001
        "components",
        "main-board",
        {"department_id": str(user.department_id), "original_name": "board.edf"},
    )
    key = _create_key(auth, user, kb_ref)
    with TestClient(app) as client:
        headers = {"X-HDB-API-Key": key["api_key"]}
        response = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/circuits/query",
            headers=headers,
            json={"kind": "modules", "query": "Power", "limit": 5},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["row_count"] == 1
        assert payload["rows"][0]["name"] == "Power"
        assert payload["rows"][0]["circuit_ref"].startswith("circuitref_")
        assert "design_id" not in response.text
        assert "main-board" not in response.text


def test_external_upload_idempotency_and_job_query(tmp_path):
    app, auth, user, kb_ref, _content_hash, backend = _setup(tmp_path)
    key = _create_write_key(auth, user, kb_ref)
    read_key = _create_key(auth, user, kb_ref)
    headers = {
        "X-HDB-API-Key": key["api_key"],
        "Idempotency-Key": "upload-request-1",
    }
    upload_files = [
        ("files", ("new.pdf", b"new datasheet body", "application/pdf"))
    ]
    with TestClient(app) as client:
        uploaded = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files",
            headers=headers,
            files=upload_files,
            data={"source_group": "docs"},
        )
        assert uploaded.status_code == 202
        payload = uploaded.json()
        assert payload["status"] == "accepted"
        assert payload["files"][0]["success"] is True
        assert payload["files"][0]["status"] == "ready"
        file_ref = payload["files"][0]["file_ref"]
        job_id = payload["files"][0]["job_id"]
        assert file_ref and job_id
        assert "new.pdf" in payload["files"][0]["file_name"]

        replay = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files",
            headers=headers,
            files=upload_files,
            data={"source_group": "docs"},
        )
        assert replay.status_code == 202
        assert replay.json()["files"][0]["file_ref"] == file_ref
        assert replay.json()["files"][0]["job_id"] == job_id
        records = [
            record
            for record in backend.store.list_documents(
                "components", str(user.department_id)
            )
            if record.original_file_name == "new.pdf"
        ]
        assert len(records) == 1

        conflict = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files",
            headers=headers,
            files=[
                ("files", ("new.pdf", b"different body", "application/pdf"))
            ],
            data={"source_group": "docs"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        job = client.get(
            f"/api/v1/external/ingestion-jobs/{job_id}",
            headers={"X-HDB-API-Key": key["api_key"]},
        )
        assert job.status_code == 200
        assert job.json()["file_ref"] == file_ref
        assert job.json()["status"] == "ready"
        assert job.json()["progress"] == 100

        # A read-only key cannot upload and cannot see another Key's job.
        read_headers = {"X-HDB-API-Key": read_key["api_key"]}
        forbidden = client.post(
            f"/api/v1/external/kbs/{kb_ref.kb_ref}/files",
            headers={**read_headers, "Idempotency-Key": "read-key-upload"},
            files=upload_files,
            data={"source_group": "docs"},
        )
        assert forbidden.status_code == 403
        assert client.get(
            f"/api/v1/external/ingestion-jobs/{job_id}",
            headers=read_headers,
        ).status_code == 404
