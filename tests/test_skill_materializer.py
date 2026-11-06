"""Reviewed text skills materialize into official SKILL.md directories."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import src.settings as settings
from src.agents.skill_materializer import materialize_text_skills, select_text_skills
from src.sandbox.skill_packages import SkillPackageStore, validate_text_skill_package
from src.sandbox.skills import SkillCatalog


def _archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, content in files.items():
            handle.writestr(name, content)
    return buffer.getvalue()


def _ctx(skill_ref: str) -> SimpleNamespace:
    return SimpleNamespace(
        user_id="4",
        metadata={"department_id": "1739", "skill_refs": [skill_ref]},
        is_system_admin=lambda: True,
    )


def test_materializes_enabled_skill_with_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_TASK_DB_PATH", str(tmp_path / "skills.db"))
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    catalog = SkillCatalog(settings.SANDBOX_TASK_DB_PATH)
    catalog.register(
        skill_ref="skill/hw-review",
        version="v1",
        content="# Hardware Review\n\nCheck power, clock and storage.",
        department_id="1739",
    )
    catalog.review("skill/hw-review")
    catalog.mark_tested("skill/hw-review")
    catalog.enable("skill/hw-review")

    records = select_text_skills(_ctx("skill/hw-review"))
    root = materialize_text_skills(_ctx("skill/hw-review"), root=str(tmp_path / "out"))

    assert [item.skill_ref for item in records] == ["skill/hw-review"]
    assert root is not None
    text = (tmp_path / "out" / "skill-hw-review" / "SKILL.md").read_text()
    assert text.startswith("---\n")
    assert "name: skill-hw-review" in text
    assert "description:" in text
    assert "Check power, clock and storage." in text

    from deepagents.backends.filesystem import FilesystemBackend
    from deepagents.middleware import SkillsMiddleware

    middleware = SkillsMiddleware(
        backend=FilesystemBackend(root_dir=root, virtual_mode=True),
        sources=["/"],
    )
    update = middleware.before_agent({}, None, None)
    names = [item["name"] for item in (update or {}).get("skills_metadata", [])]
    assert "skill-hw-review" in names


def test_materializes_reviewed_package_with_reference_and_script_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_TASK_DB_PATH", str(tmp_path / "skills.db"))
    monkeypatch.setattr(settings, "SANDBOX_SKILL_STORAGE_DIR", str(tmp_path / "packages"))
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    raw = _archive({
        "hw-design/SKILL.md": (
            "---\nname: hw-design\ndescription: Hardware design workflow.\n---\n"
            "# Workflow\nRead references/block.md before drawing.\n"
        ),
        "hw-design/references/block.md": "Use orthogonal edges and label every line.\n",
        "hw-design/scripts/check.py": "print('geometry check')\n",
    })
    package = validate_text_skill_package(raw)
    store = SkillPackageStore(str(tmp_path / "packages"))
    storage_ref = store.save(package.skill_ref, package, raw)
    catalog = SkillCatalog(settings.SANDBOX_TASK_DB_PATH)
    catalog.register(
        skill_ref=package.skill_ref,
        version=package.version,
        content=package.content,
        department_id="1739",
        text_storage_ref=storage_ref,
        text_archive_digest=package.archive_digest,
        text_files=package.files,
    )
    catalog.review(package.skill_ref)
    catalog.mark_tested(package.skill_ref)
    catalog.enable(package.skill_ref)

    root = materialize_text_skills(_ctx(package.skill_ref), root=str(tmp_path / "out"))
    assert root is not None
    skill_dir = tmp_path / "out" / "skill-hw-design"
    assert "Hardware design workflow." in (skill_dir / "SKILL.md").read_text()
    assert "orthogonal edges" in (skill_dir / "references" / "block.md").read_text()
    assert "geometry check" in (skill_dir / "scripts" / "check.py").read_text()
    assert (skill_dir / "scripts" / "check.py").stat().st_mode & 0o222 == 0
