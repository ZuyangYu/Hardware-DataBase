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
from src.sandbox.skill_packages import (
    SkillPackageError, SkillPackageStore, validate_skill_package, validate_text_skill_package,
)
from src.sandbox.schema import normalize_schema, validate_value
from src.sandbox.pip_sources import SkillPipSourceError, SkillPipSourceStore
from src.sandbox.skills import SkillCatalog, SkillCatalogError, SkillNotAuthorized, SkillStateConflict


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


def test_text_skill_zip_imports_multiple_markdown_files_without_execution():
    raw = archive({
        "bom-guide/SKILL.md": "---\nname: bom-guide\ndescription: Check BOM consistency.\nmetadata:\n  version: 1.2.0\n---\n# BOM\nSee references/checks.md\n",
        "bom-guide/references/checks.md": "Flag duplicate designators and missing voltage ratings.\n",
        "bom-guide/LICENSE.txt": "MIT\n",
    })
    package = validate_text_skill_package(raw)
    assert package.skill_ref == "skill/bom-guide" and package.version == "1.2.0"
    assert package.files == ("LICENSE.txt", "SKILL.md", "references/checks.md")
    assert "Flag duplicate designators" in package.content
    assert "### 随附资料：references/checks.md" in package.content
    assert validate_text_skill_package(archive({
        "SKILL.md": "---\nname: bom-guide\ndescription: Check BOM\n---\n# Guide",
        "notes.md": "extra material",
    })).files == ("SKILL.md", "notes.md")
    for extra in ({"bom-guide/entrypoint.py": "print('unsafe')"},
                  {"bom-guide/scripts/run.sh": "echo no"},
                  {"../escape.md": "bad"},
                  {"other/NOTE.md": "outside folder"}):
        with pytest.raises(SkillPackageError):
            validate_text_skill_package(archive({
                "bom-guide/SKILL.md": "---\nname: bom-guide\ndescription: Check BOM\n---\n# Guide",
                **extra,
            }))
    with pytest.raises(SkillPackageError):
        validate_text_skill_package(archive({"SKILL.md": "# Missing frontmatter"}))
    with pytest.raises(SkillPackageError, match="16 KiB"):
        validate_text_skill_package(archive({
            "SKILL.md": "---\nname: bom-guide\ndescription: Check BOM\n---\n# Guide",
            "references/long.md": "X" * 17_000,
        }))
    with pytest.raises(SkillPackageError, match="folder name"):
        validate_text_skill_package(archive({
            "wrong/SKILL.md": "---\nname: bom-guide\ndescription: Check BOM\n---\n# Guide",
        }))


def test_sample_skill_packages_follow_actual_upload_contract():
    executable_samples = (
        (
            "ohms-law",
            {"type": "object", "properties": {
                "voltage_v": {"type": "number"},
                "resistance_ohm": {"type": "number"},
            }, "required": ["voltage_v", "resistance_ohm"], "additionalProperties": False},
            {"voltage_v": 12, "resistance_ohm": 1000},
        ),
        (
            "bom-csv",
            {"type": "object", "properties": {"items": {
                "type": "array", "items": {"type": "object", "properties": {
                    "reference": {"type": "string"},
                    "part_number": {"type": "string"},
                    "quantity": {"type": "integer"},
                }, "required": ["reference", "part_number", "quantity"],
                   "additionalProperties": False},
            }}, "required": ["items"], "additionalProperties": False},
            {"items": [{"reference": "R1", "part_number": "10k", "quantity": 1}]},
        ),
    )
    for name, raw_schema, sample in executable_samples:
        package = validate_skill_package(archive({
            "SKILL.md": f"# {name}\nA package used to validate the upload contract.\n",
            "entrypoint.py": "print('ok')\n",
        }))
        assert package.files == ("SKILL.md", "entrypoint.py")
        assert package.requirements_declared is False
        validate_value(sample, normalize_schema(raw_schema))

    for name in ("demo-bom-review", "demo-datasheet-compare"):
        item = validate_text_skill_package(archive({
            f"{name}/SKILL.md": (
                f"---\nname: {name}\ndescription: Validate a text Skill package.\n---\n"
                f"# {name}\nUse only supplied evidence.\n"
            ),
            f"{name}/references/checklist.md": "Record missing information for human review.\n",
        }))
        assert item.skill_ref == f"skill/{name}"
        assert item.version == "1.0.0"
        assert any(path.startswith("references/") for path in item.files)


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
    assert client.delete("/sandbox/skills/skill%2Fexample").status_code == 403
    assert client.post("/sandbox/skills/text-packages", json={}).status_code == 403


def test_admin_uploads_and_downloads_reviewable_text_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    catalog = SkillCatalog(str(tmp_path / "db.sqlite"))
    packages = SkillPackageStore(str(tmp_path / "sources"))
    raw = archive({
        "bom-guide/SKILL.md": "---\nname: bom-guide\ndescription: Check BOM\n---\n# Checks",
        "bom-guide/references/notes.md": "Missing voltage is a review item",
    })
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=1, role="system_admin", username="admin", department_id=None,
    )
    app.dependency_overrides[routes.get_skill_catalog] = lambda: catalog
    app.dependency_overrides[routes.get_skill_package_store] = lambda: packages
    with TestClient(app) as client:
        response = client.post("/sandbox/skills/text-packages", json={
            "department_id": "42", "package_base64": base64.b64encode(raw).decode(),
        })
        assert response.status_code == 200, response.text
        item = response.json()["skill"]
        assert item["skill_ref"] == "skill/bom-guide" and item["approval_state"] == "draft"
        assert item["execution_bundle_ref"] == "" and item["package_archive_digest"] == ""
        assert item["text_archive_digest"] == hashlib.sha256(raw).hexdigest()
        assert item["text_files"] == ["SKILL.md", "references/notes.md"]
        assert item["metadata"]["summary"] == "Check BOM"
        downloaded = client.get("/sandbox/skills/skill%2Fbom-guide/source")
        assert downloaded.status_code == 200 and downloaded.content == raw
        for action in ("review", "tested", "enable"):
            assert client.post(f"/sandbox/skills/skill%2Fbom-guide/{action}").status_code == 200
        catalog.subscribe("skill/bom-guide", principal_id="user-42", department_id="42")
        assert catalog.is_subscribed("skill/bom-guide", "user-42")
        assert "Missing voltage" in catalog.prompt_block("skill/bom-guide", principal_id="user-42", department_id="42")


def test_employee_text_skill_upload_review_and_department_share(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    catalog = SkillCatalog(str(tmp_path / "db.sqlite"))
    packages = SkillPackageStore(str(tmp_path / "sources"))
    people = {
        "owner": SimpleNamespace(id=71, role="employee", username="owner", department_id="47"),
        "colleague": SimpleNamespace(id=72, role="employee", username="colleague", department_id="47"),
        "outsider": SimpleNamespace(id=73, role="employee", username="outsider", department_id="48"),
        "admin": SimpleNamespace(id=1, role="system_admin", username="admin", department_id=None),
    }
    actor = [people["owner"]]
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: actor[0]
    app.dependency_overrides[routes.get_skill_catalog] = lambda: catalog
    app.dependency_overrides[routes.get_skill_package_store] = lambda: packages
    app.dependency_overrides[routes.get_auth_service] = lambda: None
    app.dependency_overrides[routes.get_sandbox_authorization_store] = lambda: None
    raw = archive({
        "SKILL.md": "---\nname: bom-guide\ndescription: Check BOM\n---\n# BOM guide\nCheck designators.",
        "references/checks.md": "Check voltage ratings too.",
    })
    path = "/sandbox/skills/skill%2Fpersonal%2F71%2Fbom-guide"
    share_path = "/sandbox/skills/personal/skill%2Fpersonal%2F71%2Fbom-guide/share"
    with TestClient(app) as client:
        submitted = client.post("/sandbox/skills/personal/text-packages", json={
            "package_base64": base64.b64encode(raw).decode(),
        })
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["skill"]["skill_ref"] == "skill/personal/71/bom-guide"
        assert submitted.json()["skill"]["approval_state"] == "draft"
        assert "text_storage_ref" not in submitted.json()["skill"]
        assert catalog.require("skill/personal/71/bom-guide").catalog_source == "employee"
        assert client.get("/sandbox/skills/mine").json()["items"][0]["share_scope"] == "private"
        assert client.get("/sandbox/skills/available").json()["items"] == []
        assert client.post(share_path, json={"share_to_department": True}).status_code == 409
        assert client.post("/sandbox/skills/personal/text-packages", json={
            "package_base64": base64.b64encode(raw).decode(),
        }).status_code == 409
        assert client.post("/sandbox/skills/personal/text-packages", json={
            "package_base64": base64.b64encode(archive({"SKILL.md": "---\nname: script\ndescription: Unsafe\n---\n# script", "entrypoint.py": "print(1)"})).decode(),
        }).status_code == 400
        assert client.get(path + "/source").status_code == 403
        assert client.post(path + "/review").status_code == 403
        assert client.post("/sandbox/skills/packages", json={}).status_code == 403
        assert client.post("/sandbox/skills/personal/text-packages", json={
            "package_base64": base64.b64encode(raw).decode(), "department_id": "*",
        }).status_code == 422
        actor[0] = SimpleNamespace(id=74, role="employee", username="no-dept", department_id=None)
        assert client.post("/sandbox/skills/personal/text-packages", json={
            "package_base64": base64.b64encode(raw).decode(),
        }).status_code == 403

        actor[0] = people["admin"]
        assert client.get("/sandbox/skills/mine").status_code == 403
        assert client.post("/sandbox/skills/personal/text-packages", json={}).status_code == 403
        assert client.get(path + "/source").content == raw
        for action in ("review", "tested", "enable"):
            assert client.post(path + "/" + action).status_code == 200
        actor[0] = people["owner"]
        assert [item["skill_ref"] for item in client.get("/sandbox/skills/available").json()["items"]] == ["skill/personal/71/bom-guide"]
        assert client.post(path + "/subscribe").status_code == 200
        assert "Check voltage ratings" in catalog.prompt_block(
            "skill/personal/71/bom-guide", principal_id=71, department_id="47",
        )
        actor[0] = people["colleague"]
        assert client.get("/sandbox/skills/mine").json()["items"] == []
        assert client.get("/sandbox/skills/available").json()["items"] == []
        assert client.post(path + "/subscribe").status_code == 403
        assert client.post(share_path, json={"share_to_department": True}).status_code == 404
        with pytest.raises(SkillNotAuthorized):
            catalog.prompt_block("skill/personal/71/bom-guide", principal_id=72, department_id="47")

        actor[0] = people["owner"]
        assert client.post(share_path, json={"share_to_department": True}).json()["skill"]["share_scope"] == "department"
        actor[0] = people["colleague"]
        assert client.get("/sandbox/skills/available").json()["items"][0]["share_scope"] == "department"
        assert client.post(path + "/subscribe").status_code == 200
        assert [item.skill_ref for item in catalog.subscribed_text(principal_id=72, department_id="47")] == ["skill/personal/71/bom-guide"]
        actor[0] = people["outsider"]
        assert client.get("/sandbox/skills/available").json()["items"] == []
        assert client.post(path + "/subscribe").status_code == 403
        actor[0] = people["owner"]
        assert client.post(share_path, json={"share_to_department": False}).json()["skill"]["share_scope"] == "private"
        assert catalog.is_subscribed("skill/personal/71/bom-guide", 71)
        assert not catalog.is_subscribed("skill/personal/71/bom-guide", 72)
        assert catalog.subscribed_text(principal_id=72, department_id="47") == []
        actor[0] = people["colleague"]
        assert client.get("/sandbox/skills/available").json()["items"] == []
        assert client.post(path + "/subscribe").status_code == 403
        with pytest.raises(SkillNotAuthorized):
            catalog.prompt_block("skill/personal/71/bom-guide", principal_id=72, department_id="47")
        actor[0] = people["admin"]
        assert client.post(path + "/revoke").status_code == 200
        actor[0] = people["owner"]
        assert client.post(share_path, json={"share_to_department": True}).status_code == 409
        assert client.get("/sandbox/skills/available").json()["items"] == []


def test_employee_executable_upload_requires_source_and_image_review(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_BACKEND", "runner_api")
    monkeypatch.setattr(settings, "SANDBOX_RUNNER_URL", "https://runner.example")
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_AUTH_REF", "workload://test")
    monkeypatch.setattr(settings, "SANDBOX_REQUIRE_RUNTIME_ATTESTATION", False)
    monkeypatch.setattr(settings, "SANDBOX_ALLOW_DYNAMIC_INSTALL", False)
    monkeypatch.setattr(settings, "SANDBOX_DEFAULT_NETWORK", "none")
    monkeypatch.setattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "a" * 64)
    monkeypatch.setattr(routes, "_context_capabilities", lambda *args: {"sandbox.execute", "sandbox.write_artifact"})
    skills = SkillCatalog(str(tmp_path / "tasks.db"))
    bundles = SandboxCatalog(skills.db_path)
    packages = SkillPackageStore(str(tmp_path / "sources"))
    pip_sources = SkillPipSourceStore(skills.db_path)
    people = {
        "owner": SimpleNamespace(id=81, role="employee", username="owner", department_id="47"),
        "colleague": SimpleNamespace(id=82, role="employee", username="colleague", department_id="47"),
        "outsider": SimpleNamespace(id=83, role="employee", username="outsider", department_id="48"),
        "admin": SimpleNamespace(id=1, role="system_admin", username="admin", department_id=None),
    }
    actor = [people["owner"]]
    raw = archive({"SKILL.md": "# Calculator\n", "entrypoint.py": "from pathlib import Path\nPath('/work/out/value.txt').write_text('ok')\n"})
    package = validate_skill_package(raw)
    input_schema = {"type": "object", "properties": {"voltage_v": {"type": "number"}},
                    "required": ["voltage_v"], "additionalProperties": False}
    calls = []

    class FakeRunner:
        def skill_build(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["package_bytes"] == raw
            assert kwargs["package_digest"] == package.archive_digest
            assert kwargs["input_schema"] == input_schema
            assert kwargs["runtime_policy_ref"] == "profile/dev-l1"
            assert kwargs["runtime_policy_digest"] == "a" * 64
            return {"bundle_ref": "bundle/skill-employee-calculator", "bundle_digest": hashlib.sha256(package.entrypoint).hexdigest(),
                    "package_digest": package.archive_digest, "profile_ref": kwargs["profile_ref"], "image_digest": "sha256:" + "f" * 64}

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: actor[0]
    app.dependency_overrides[routes.get_skill_catalog] = lambda: skills
    app.dependency_overrides[routes.get_sandbox_catalog] = lambda: bundles
    app.dependency_overrides[routes.get_skill_package_store] = lambda: packages
    app.dependency_overrides[routes.get_skill_pip_source_store] = lambda: pip_sources
    app.dependency_overrides[routes.get_sandbox_service_client] = lambda: FakeRunner()
    app.dependency_overrides[routes.get_auth_service] = lambda: None
    app.dependency_overrides[routes.get_sandbox_authorization_store] = lambda: None
    path = "/sandbox/skills/skill%2Fpersonal%2F81%2Fcalculator"
    share_path = "/sandbox/skills/personal/skill%2Fpersonal%2F81%2Fcalculator/share"
    build_body = {"profile_ref": "profile/dev-l1", "input_schema": input_schema}
    with TestClient(app) as client:
        invalid = client.post("/sandbox/skills/personal/packages", json={
            "skill_name": "bad", "package_base64": base64.b64encode(raw).decode(), "input_schema": {},
        })
        assert invalid.status_code == 400
        submitted = client.post("/sandbox/skills/personal/packages", json={
            "skill_name": "calculator", "version": "1.0.0", "package_base64": base64.b64encode(raw).decode(),
            "input_schema": input_schema,
        })
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["skill"]["kind"] == "executable"
        assert submitted.json()["skill"]["package_build_state"] == "uploaded"
        assert submitted.json()["skill"]["share_scope"] == "private"
        assert "package_storage_ref" not in submitted.json()["skill"]
        assert skills.require("skill/personal/81/calculator").metadata["submitted_input_schema"] == input_schema
        assert client.get("/sandbox/skills/available").json()["items"] == []
        assert client.get(path + "/source").status_code == 403
        assert client.post(path + "/approve-source").status_code == 403
        assert client.post(path + "/build", json=build_body).status_code == 403

        actor[0] = people["admin"]
        assert client.post(path + "/review").status_code == 409
        assert client.post(path + "/build", json=build_body).status_code == 409
        assert not calls
        assert client.get(path + "/source").content == raw
        assert client.post(path + "/approve-source").json()["skill"]["package_build_state"] == "source_reviewed"
        built = client.post(path + "/build", json=build_body)
        assert built.status_code == 200, built.text
        assert len(calls) == 1
        skill = skills.require("skill/personal/81/calculator")
        assert skill.package_build_state == "built" and skill.execution_bundle_ref == "bundle/skill-employee-calculator"
        assert client.post(path + "/review").status_code == 200
        assert client.post(path + "/tested").status_code == 200
        assert client.post(path + "/enable").status_code == 409
        bundle = bundles.require(skill.execution_bundle_ref)
        bundles.review(bundle.bundle_ref)
        bundles.mark_tested(bundle.bundle_ref)
        bundles.enable(bundle.bundle_ref)
        assert client.post(path + "/enable").status_code == 200

        actor[0] = people["owner"]
        available = client.get("/sandbox/skills/available").json()["items"]
        assert len(available) == 1 and available[0]["kind"] == "executable"
        assert client.post(path + "/subscribe").status_code == 200
        assert skills.execution_bundle("skill/personal/81/calculator", principal_id=81, department_id="47").bundle_ref == bundle.bundle_ref
        actor[0] = people["colleague"]
        assert client.get("/sandbox/skills/available").json()["items"] == []
        assert client.post(path + "/subscribe").status_code == 403
        actor[0] = people["owner"]
        assert client.post(share_path, json={"share_to_department": True}).json()["skill"]["share_scope"] == "department"
        actor[0] = people["colleague"]
        assert client.get("/sandbox/skills/available").json()["items"][0]["kind"] == "executable"
        assert client.post(path + "/subscribe").status_code == 200
        assert skills.execution_bundle("skill/personal/81/calculator", principal_id=82, department_id="47").bundle_ref == bundle.bundle_ref
        actor[0] = people["outsider"]
        assert client.get("/sandbox/skills/available").json()["items"] == []
        assert client.post(path + "/subscribe").status_code == 403
        actor[0] = people["owner"]
        assert client.post(share_path, json={"share_to_department": False}).status_code == 200
        assert not skills.is_subscribed("skill/personal/81/calculator", 82)
        with pytest.raises(SkillNotAuthorized):
            skills.execution_bundle("skill/personal/81/calculator", principal_id=82, department_id="47")


def test_revoked_skill_delete_endpoint_checks_role_state_and_clears_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    catalog = SkillCatalog(str(tmp_path / "db.sqlite"))
    catalog.register(skill_ref="skill/example", version="v1", content="Example", department_id="17")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=1, role="system_admin", username="admin", department_id=None,
    )
    app.dependency_overrides[routes.get_skill_catalog] = lambda: catalog
    with TestClient(app) as client:
        path = "/sandbox/skills/skill%2Fexample"
        assert client.delete(path).status_code == 409
        assert client.post(path + "/revoke").status_code == 200
        deleted = client.delete(path).json()
        assert deleted["deleted"] is True
        assert deleted["sources_purged"] == 0
        assert catalog.get("skill/example") is None
        assert client.delete(path).status_code == 404


def test_skill_package_store_delete_is_path_safe(tmp_path):
    store = SkillPackageStore(str(tmp_path / "source"))
    raw = archive({"SKILL.md": "# Report", "entrypoint.py": "print('ok')\n"})
    package = validate_skill_package(raw)
    ref = store.save("skill/x", package, raw)
    target = Path(store.root) / ref
    assert target.exists()
    assert store.delete(ref) is True
    assert not target.exists()
    assert store.delete("") is False
    assert store.delete("../../etc/passwd") is False


def test_delete_skill_purges_source_and_generated_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    catalog = SkillCatalog(str(tmp_path / "tasks.db"))
    packages = SkillPackageStore(str(tmp_path / "source"))
    raw = archive({"SKILL.md": "---\nname: purge-me\ndescription: purge me\n---\n# guide\n"})
    package = validate_text_skill_package(raw)
    storage_ref = packages.save("skill/purge-me", package, raw)
    purged: list[str] = []

    class FakeClient:
        def purge_generated_bundle(self, bundle_ref, *, envelope=None):
            purged.append(bundle_ref)
            return {"purged": True, "cleanup_pending": False}

    item = SimpleNamespace(
        text_storage_ref=storage_ref, package_storage_ref="",
        execution_bundle_ref="bundle/skill-purge-me", version="v1",
        content_digest="digest", package_archive_digest="", text_archive_digest="a" * 64,
    )
    monkeypatch.setattr(catalog, "delete_revoked", lambda ref: item)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=1, role="system_admin", username="admin", department_id=None,
    )
    app.dependency_overrides[routes.get_skill_catalog] = lambda: catalog
    app.dependency_overrides[routes.get_skill_package_store] = lambda: packages
    app.dependency_overrides[routes.get_sandbox_service_client] = lambda: FakeClient()
    with TestClient(app) as client:
        response = client.delete("/sandbox/skills/skill%2Fpurge-me")
        assert response.status_code == 200, response.text
        body = response.json()
    assert body["sources_purged"] == 1
    assert body["bundle_purged"] is True
    assert body["cleanup_pending"] is False
    assert purged == ["bundle/skill-purge-me"]
    assert not (Path(packages.root) / storage_ref).exists()


def test_failed_personal_upload_removes_orphan_package(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_audit", lambda *a, **kw: None)
    catalog = SkillCatalog(str(tmp_path / "tasks.db"))
    packages = SkillPackageStore(str(tmp_path / "source"))

    def _conflict(**kwargs):
        raise SkillStateConflict("race")

    monkeypatch.setattr(catalog, "register", _conflict)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=2, role="employee", username="employee", department_id="17",
    )
    app.dependency_overrides[routes.get_skill_catalog] = lambda: catalog
    app.dependency_overrides[routes.get_skill_package_store] = lambda: packages
    raw = archive({"SKILL.md": "---\nname: race\ndescription: race\n---\n# x\n"})
    with TestClient(app) as client:
        response = client.post(
            "/sandbox/skills/personal/text-packages",
            json={"package_base64": base64.b64encode(raw).decode("ascii")},
        )
    assert response.status_code == 409, response.text
    assert list(Path(packages.root).rglob("*.zip")) == []
