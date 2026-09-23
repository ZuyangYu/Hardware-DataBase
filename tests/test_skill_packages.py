"""Admin package ingestion, build binding and employee subscription fences."""

import base64
import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import src.settings as settings
from src.api.deps import current_user
from src.api.routes import sandbox as routes
from src.api.schemas import SandboxSkillPackageUploadRequest
from src.sandbox.catalog import SandboxCatalog
from src.sandbox.skill_packages import SkillPackageError, SkillPackageStore, validate_skill_package
from src.sandbox.pip_sources import SkillPipSourceError, SkillPipSourceStore
from src.sandbox.skills import SkillCatalog, SkillCatalogError, SkillStateConflict


def archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, content in files.items():
            handle.writestr(name, content)
    return buffer.getvalue()


def test_skill_package_rejects_paths_build_recipes_and_dependencies():
    basic = {"SKILL.md": "# Report", "entrypoint.py": "print('ok')\n"}
    assert validate_skill_package(archive(basic)).entrypoint == b"print('ok')\n"
    for extra in ({"../escape.py": "x"}, {"Dockerfile": "FROM scratch"},
                  {"requirements.lock": "requests==2.0 --hash=sha256:abc"}):
        with pytest.raises(SkillPackageError):
            validate_skill_package(archive({**basic, **extra}))
    locked = validate_skill_package(archive({**basic, "requirements.lock":
                                            "six==1.17.0 --hash=sha256:" + "a" * 64 + "\n"}))
    assert locked.requirements_declared


def test_admin_dependency_source_is_host_allowlisted_and_persistent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_SKILL_PIP_ALLOWED_HOSTS", "pypi.org,packages.example")
    sources = SkillPipSourceStore(str(tmp_path / "db.sqlite"))
    assert sources.set("https://pypi.org/simple") == "https://pypi.org/simple/"
    assert SkillPipSourceStore(str(tmp_path / "db.sqlite")).get() == "https://pypi.org/simple/"
    for url in ("http://pypi.org/simple", "https://evil.example/simple",
                "https://pypi.org@evil.example/simple", "https://pypi.org/simple?token=x"):
        with pytest.raises(SkillPipSourceError):
            sources.set(url)


def test_admin_can_configure_pip_source_over_http(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_SKILL_PIP_ALLOWED_HOSTS", "pypi.org")
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    sources = SkillPipSourceStore(str(tmp_path / "config.db"))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=1, role="system_admin", username="admin", department_id="dept-1",
    )
    app.dependency_overrides[routes.get_skill_pip_source_store] = lambda: sources
    with TestClient(app) as client:
        assert client.get("/sandbox/skill-dependencies").json()["index_url"] == ""
        updated = client.put("/sandbox/skill-dependencies", json={"index_url": "https://pypi.org/simple"})
        assert updated.status_code == 200
        assert updated.json()["index_url"] == "https://pypi.org/simple/"
        assert client.get("/sandbox/skill-dependencies").json()["index_url"] == updated.json()["index_url"]
        assert client.put("/sandbox/skill-dependencies", json={"index_url": "https://other.example"}).status_code == 400


def test_runner_revalidates_dependency_index_independently(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "deploy/sandbox-runner/runner.py"
    spec = importlib.util.spec_from_file_location("test_pip_runner", path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setenv("SANDBOX_RUNNER_PIP_ALLOWED_HOSTS", "pypi.org")
    lock = ("six==1.17.0 --hash=sha256:" + "a" * 64 + "\n").encode()
    assert runner._skill_dependency_index("https://pypi.org/simple", lock) == "https://pypi.org/simple/"
    with pytest.raises(HTTPException):
        runner._skill_dependency_index("https://evil.example/simple", lock)
    with pytest.raises(HTTPException):
        runner._skill_dependency_index("https://pypi.org/simple", b"six>=1.0\n")


def test_upload_build_review_subscription_and_persistent_source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "a" * 64)
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    db = str(tmp_path / "tasks.db")
    skills = SkillCatalog(db)
    bundles = SandboxCatalog(db)
    packages = SkillPackageStore(str(tmp_path / "source"))
    pip_sources = SkillPipSourceStore(db)
    pip_sources.set("https://pypi.org/simple/")
    admin = SimpleNamespace(id=1, username="admin", department_id="dept-1")
    employee = SimpleNamespace(id=2, username="employee", department_id="dept-1")
    raw = archive({
        "SKILL.md": "# Admin report\n",
        "entrypoint.py": "from pathlib import Path\nPath('/work/out/result.txt').write_text('ok')\n",
        "lib/helper.py": "VALUE = 'ok'\n",
        "requirements.lock": "six==1.17.0 --hash=sha256:" + "a" * 64 + "\n",
    })
    package = validate_skill_package(raw)

    class FakeRunner:
        def skill_build(self, **kwargs):
            assert kwargs["package_bytes"] == raw
            assert kwargs["package_digest"] == package.archive_digest
            assert kwargs["base_bundle_ref"] == "bundle/report"
            assert kwargs["dependency_index_url"] == "https://pypi.org/simple/"
            return {
                "bundle_ref": "bundle/skill-admin-report-123456789abc",
                "bundle_digest": hashlib.sha256(package.entrypoint).hexdigest(),
                "package_digest": package.archive_digest,
                "profile_ref": kwargs["profile_ref"],
                "image_digest": "sha256:" + "f" * 64,
            }

    response = routes.upload_skill_package(
        SandboxSkillPackageUploadRequest(
            skill_ref="skill/admin-report", version="v1", department_id="dept-1",
            package_base64=base64.b64encode(raw).decode(),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ), actor=admin, catalog=skills, packages=packages, bundles=bundles,
        pip_sources=pip_sources, client=FakeRunner(),
    )
    skill = skills.require("skill/admin-report")
    assert response.skill["package_build_state"] == "built"
    assert packages.read(skill.package_storage_ref) == raw
    assert skill.package_archive_digest == hashlib.sha256(raw).hexdigest()
    assert skill.execution_bundle_digest == hashlib.sha256(package.entrypoint).hexdigest()
    assert bundles.require(skill.execution_bundle_ref).metadata["image_digest"] == "sha256:" + "f" * 64
    with pytest.raises(SkillCatalogError):
        skills.subscribe(skill.skill_ref, principal_id=employee.id, department_id=employee.department_id)
    bundle = bundles.require(skill.execution_bundle_ref)
    bundles.review(bundle.bundle_ref)
    bundles.mark_tested(bundle.bundle_ref)
    monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_BACKEND", "runner_api")
    monkeypatch.setattr(settings, "SANDBOX_RUNNER_URL", "https://runner.example")
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_AUTH_REF", "workload://test")
    monkeypatch.setattr(settings, "SANDBOX_REQUIRE_RUNTIME_ATTESTATION", False)
    monkeypatch.setattr(settings, "SANDBOX_ALLOW_DYNAMIC_INSTALL", False)
    monkeypatch.setattr(settings, "SANDBOX_DEFAULT_NETWORK", "none")
    bundles.enable(bundle.bundle_ref)
    skills.review(skill.skill_ref)
    skills.mark_tested(skill.skill_ref)
    skills.enable(skill.skill_ref)
    skills.subscribe(skill.skill_ref, principal_id=employee.id, department_id=employee.department_id)
    assert skills.is_subscribed(skill.skill_ref, employee.id)
    assert packages.read(skill.package_storage_ref) == raw
    skills.unsubscribe(skill.skill_ref, principal_id=employee.id)
    assert not skills.is_subscribed(skill.skill_ref, employee.id)
    with pytest.raises(SkillCatalogError):
        skills.subscribe(skill.skill_ref, principal_id=3, department_id="another-dept")
    bundles.revoke(bundle.bundle_ref)
    with pytest.raises(SkillCatalogError):
        skills.subscribe(skill.skill_ref, principal_id=employee.id, department_id=employee.department_id)
    assert packages.read(skill.package_storage_ref) == raw


def test_failed_build_keeps_source_as_unreviewable_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    skills = SkillCatalog(str(tmp_path / "db.sqlite"))
    bundles = SandboxCatalog(skills.db_path)
    packages = SkillPackageStore(str(tmp_path / "source"))
    raw = archive({"SKILL.md": "# Draft", "entrypoint.py": "print(1)\n"})
    admin = SimpleNamespace(id=1, username="admin", department_id="dept-1")
    with pytest.raises(HTTPException) as exc:
        routes.upload_skill_package(
            SandboxSkillPackageUploadRequest(skill_ref="skill/draft", version="v1", department_id="dept-1",
                                             package_base64=base64.b64encode(raw).decode()),
            actor=admin, catalog=skills, packages=packages, bundles=bundles, client=None,
        )
    assert exc.value.status_code == 409
    item = skills.require("skill/draft")
    assert item.package_build_state == "uploaded" and packages.read(item.package_storage_ref) == raw
    with pytest.raises(SkillStateConflict, match="built bundle"):
        skills.review(item.skill_ref)


def test_employee_cannot_upload_build_review_or_read_skill_sources():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=2, role="employee", username="employee", department_id="dept-1"
    )
    client = TestClient(app)
    for path in ("/sandbox/skills/packages", "/sandbox/skills/skill%2Fexample/build",
                 "/sandbox/skills/skill%2Fexample/review"):
        assert client.post(path, json={}).status_code == 403
    assert client.get("/sandbox/skills/skill%2Fexample/source").status_code == 403
    assert client.get("/sandbox/skill-dependencies").status_code == 403
    assert client.put("/sandbox/skill-dependencies", json={"index_url": "https://pypi.org/simple/"}).status_code == 403
