"""Reviewed text skills materialize into official SKILL.md directories."""

from __future__ import annotations

from types import SimpleNamespace

import src.settings as settings
from src.agents.skill_materializer import (
    load_skill_prompt_context,
    materialize_text_skills,
    select_text_skills,
)
from src.sandbox.skills import SkillCatalog


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


def test_temporary_disable_excludes_skill_from_materialization(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_TASK_DB_PATH", str(tmp_path / "skills.db"))
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    catalog = SkillCatalog(settings.SANDBOX_TASK_DB_PATH)
    catalog.register(
        skill_ref="skill/hw-review",
        version="v1",
        content="# Hardware Review\n\nCheck power.",
        department_id="1739",
    )
    catalog.review("skill/hw-review")
    catalog.mark_tested("skill/hw-review")
    catalog.enable("skill/hw-review")
    disabled_ctx = SimpleNamespace(
        user_id="4",
        metadata={
            "department_id": "1739",
            "skill_refs": ["skill/hw-review"],
            "disabled_skill_refs": ["skill/hw-review"],
        },
        is_system_admin=lambda: True,
    )
    assert select_text_skills(disabled_ctx) == []
    assert materialize_text_skills(disabled_ctx, root=str(tmp_path / "out")) is None
    assert load_skill_prompt_context(disabled_ctx) == ""
