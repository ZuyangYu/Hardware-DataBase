"""Skill source provenance, Ed25519 verification and SBOM review evidence."""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.settings as settings
from src.api.deps import current_user
from src.api.routes import sandbox as routes
from src.api.schemas import SandboxSkillPackageUploadRequest
from src.sandbox.catalog import SandboxCatalog
from src.sandbox.provenance import (
    SkillProvenanceError,
    SkillProvenanceStore,
    build_sbom,
    signed_source_payload,
)
from src.sandbox.skill_packages import SkillPackageStore, validate_skill_package
from src.sandbox.skills import SkillCatalog
from src.sandbox.pip_sources import SkillPipSourceStore


def archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, content in files.items():
            handle.writestr(name, content)
    return buffer.getvalue()


def public_key_text(private: Ed25519PrivateKey) -> str:
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def signature_text(private: Ed25519PrivateKey, *, digest: str, source_ref: str, commit: str) -> str:
    return base64.urlsafe_b64encode(
        private.sign(signed_source_payload(
            package_digest=digest, source_ref=source_ref, source_commit=commit,
        ))
    ).decode("ascii").rstrip("=")


def test_signature_requires_an_admin_registered_key_and_never_accepts_uploader_key(tmp_path):
    store = SkillProvenanceStore(str(tmp_path / "provenance.db"))
    private = Ed25519PrivateKey.generate()
    digest = "a" * 64
    source_ref = "https://git.example/hdb/skill.git"
    commit = "a" * 40
    signature = signature_text(private, digest=digest, source_ref=source_ref, commit=commit)

    with pytest.raises(SkillProvenanceError, match="not registered"):
        from src.sandbox.provenance import verify_signature

        verify_signature(
            package_digest=digest, source_ref=source_ref, source_commit=commit,
            signing_key_id="release-key", signature=signature, trusted_public_key=None,
        )

    registered = store.register_key(
        key_id="release-key", public_key=public_key_text(private), actor="admin",
    )
    assert registered["state"] == "active"
    assert registered["fingerprint"] == hashlib.sha256(
        private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).hexdigest()

    from src.sandbox.provenance import verify_signature

    verified = verify_signature(
        package_digest=digest, source_ref=source_ref, source_commit=commit,
        signing_key_id="release-key", signature=signature,
        trusted_public_key=registered["public_key"],
    )
    assert verified["status"] == "verified"

    store.revoke_key("release-key", actor="admin")
    assert store.get_key("release-key", active_only=True) is None


def test_generated_sbom_is_explicitly_not_an_attestation(tmp_path):
    package = validate_skill_package(archive({
        "SKILL.md": "# Report\n",
        "entrypoint.py": "print('ok')\n",
        "requirements.lock": "six==1.17.0 --hash=sha256:" + "a" * 64 + "\n",
    }))
    sbom, summary = build_sbom(
        package_digest=package.archive_digest,
        source_manifest=package.source_manifest,
        dependencies=package.dependencies,
    )
    assert sbom["attestation_status"] == "generated_not_attested"
    assert summary["status"] == "generated_not_attested"
    assert summary["dependency_count"] == 1
    assert len(sbom["source_manifest"]) == 3

    with pytest.raises(SkillProvenanceError, match="does not match"):
        build_sbom(
            package_digest=package.archive_digest,
            source_manifest=package.source_manifest,
            provided={
                "format": "hdb-sbom-v1",
                "package_digest": package.archive_digest,
                "source_manifest": [],
            },
        )


def test_signed_external_upload_persists_source_sbom_and_review_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "a" * 64)
    monkeypatch.setattr(routes, "_audit", lambda *args, **kwargs: None)
    db = str(tmp_path / "tasks.db")
    skills = SkillCatalog(db)
    bundles = SandboxCatalog(db)
    packages = SkillPackageStore(str(tmp_path / "source"))
    pip_sources = SkillPipSourceStore(db)
    raw = archive({"SKILL.md": "# Signed report\n", "entrypoint.py": "print('ok')\n"})
    package = validate_skill_package(raw)
    private = Ed25519PrivateKey.generate()
    provenance = SkillProvenanceStore(db)
    provenance.register_key(key_id="release-key", public_key=public_key_text(private), actor="admin")
    source_ref = "https://git.example/hdb/signed-report.git"
    commit = "b" * 40
    body = SandboxSkillPackageUploadRequest(
        skill_ref="skill/signed-report", version="2.0.0", department_id="dept-1",
        source_kind="git", source_ref=source_ref, source_commit=commit,
        signing_key_id="release-key",
        signature_base64=signature_text(private, digest=package.archive_digest,
                                        source_ref=source_ref, commit=commit),
        package_base64=base64.b64encode(raw).decode("ascii"),
    )

    class FakeRunner:
        def skill_build(self, **kwargs):
            return {
                "bundle_ref": "bundle/skill-signed-report",
                "bundle_digest": "c" * 64,
                "package_digest": kwargs["package_digest"],
                "profile_ref": kwargs["profile_ref"],
                "image_digest": "sha256:" + "d" * 64,
            }

    admin = SimpleNamespace(id=1, username="admin", department_id="dept-1", role="system_admin")
    response = routes.upload_skill_package(
        body, actor=admin, catalog=skills, packages=packages, bundles=bundles,
        pip_sources=pip_sources, client=FakeRunner(),
    )
    view = response.skill
    assert view["provenance"]["source_kind"] == "git"
    assert view["provenance"]["signature"]["status"] == "verified"
    assert view["provenance"]["sbom"]["status"] == "generated_not_attested"
    assert view["provenance"]["review"]["state"] == "built"

    detail = routes.get_skill_provenance("skill/signed-report", actor=admin, catalog=skills)
    assert detail["provenance"]["package_digest"] == package.archive_digest
    assert detail["sbom"]["package_digest"] == package.archive_digest
    assert [item["action"] for item in detail["reviews"]] == [
        "source_uploaded", "bundle_build",
    ]

    invalid = body.model_copy(update={
        "skill_ref": "skill/invalid-signature",
        "signature_base64": signature_text(private, digest=package.archive_digest,
                                             source_ref="https://git.example/other.git", commit=commit),
    })
    with pytest.raises(HTTPException) as exc:
        routes.upload_skill_package(
            invalid, actor=admin, catalog=skills, packages=packages, bundles=bundles,
            pip_sources=pip_sources, client=FakeRunner(),
        )
    assert exc.value.status_code == 400
    assert skills.get("skill/invalid-signature") is None


def test_signing_key_api_is_admin_only_and_does_not_return_secret_material(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_audit", lambda *args, **kwargs: None)
    catalog = SkillCatalog(str(tmp_path / "tasks.db"))
    private = Ed25519PrivateKey.generate()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=1, username="admin", role="system_admin", department_id=None,
    )
    app.dependency_overrides[routes.get_skill_catalog] = lambda: catalog
    with TestClient(app) as client:
        registered = client.post("/sandbox/skill-signing-keys", json={
            "key_id": "release-key", "public_key": public_key_text(private),
        })
        assert registered.status_code == 200
        assert registered.json()["key"]["fingerprint"]
        assert "public_key" not in registered.json()["key"]
        listed = client.get("/sandbox/skill-signing-keys")
        assert listed.status_code == 200 and listed.json()["items"][0]["key_id"] == "release-key"
        revoked = client.post("/sandbox/skill-signing-keys/release-key/revoke")
        assert revoked.status_code == 200 and revoked.json()["key"]["state"] == "revoked"
