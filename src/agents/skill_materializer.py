"""Materialize reviewed text Skills into read-only SKILL.md directories.

The SkillCatalog remains the source of truth for review, versioning,
subscription and department scope.  The official ``SkillsMiddleware`` only
sees a per-turn, read-only directory of the skills this principal may use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from typing import Any

import src.settings as settings

_MAX_SKILLS = 8
_NAME_RE = re.compile(r"[^a-z0-9]+")


def _skill_name(skill_ref: str) -> str:
    name = _NAME_RE.sub("-", str(skill_ref or "").lower()).strip("-")
    if not name:
        name = "skill"
    if len(name) > 48:
        name = name[:48].rstrip("-")
    return name


def _description(content: str, skill_ref: str) -> str:
    """Return the skill's real description, not a YAML metadata key."""

    text = str(content or "")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                key, _, value = line.partition(":")
                if key.strip().lower() == "description":
                    value = value.strip().strip("\"'")
                    if value:
                        return value[:200]
            text = parts[2]
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        # Skip any remaining YAML metadata keys (name:, version:, ...).
        if re.match(r"^[A-Za-z0-9_-]+:", line):
            continue
        return line[:200]
    return f"Hardware knowledge skill {skill_ref}"[:200]


def _skill_markdown(content: str, name: str, description: str) -> str:
    body = str(content or "")
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            body = parts[2].lstrip("\n")
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n"
        f"{body}"
    )


def select_text_skills(ctx: Any) -> list[Any]:
    """Return enabled, authorized text Skill records for this turn."""

    if ctx is None or not bool(getattr(settings, "SKILLS_ENABLED", False)):
        return []
    try:
        from src.sandbox.skills import SkillCatalog

        catalog = SkillCatalog()
        metadata = dict(getattr(ctx, "metadata", {}) or {})
        department_id = str(
            metadata.get("actor_department_id", metadata.get("department_id", ""))
        )
        principal_id = str(getattr(ctx, "user_id", "") or "")
        is_admin = bool(ctx.is_system_admin())
        if not is_admin:
            from src.core.auth import AuthService

            user = AuthService().get_user_by_username(principal_id)
            if (
                user is None
                or not user.is_active
                or user.role != "employee"
                or not user.department_id
                or str(user.department_id) != department_id
            ):
                return []
            principal_id = str(user.id)
            return list(
                catalog.subscribed_text(
                    principal_id=principal_id, department_id=user.department_id
                )
            )[: _MAX_SKILLS]

        refs = metadata.get("skill_refs", metadata.get("skills", ()))
        if not isinstance(refs, (list, tuple, set, frozenset)):
            return []
        records = []
        seen: set[str] = set()
        for raw in refs:
            ref = str(raw or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            try:
                record = catalog.require(ref)
            except Exception:
                continue
            if record.enabled and not record.execution_bundle_ref:
                records.append(record)
            if len(records) >= _MAX_SKILLS:
                break
        return records
    except Exception:
        return []


def _package_files(record: Any) -> dict[str, str] | None:
    """Return the reviewed archive's files, or None for legacy text records."""

    storage_ref = str(getattr(record, "text_storage_ref", "") or "")
    if not storage_ref:
        return None
    try:
        from src.sandbox.skill_packages import SkillPackageStore, load_text_skill_files

        raw = SkillPackageStore().read(storage_ref)
        expected = str(getattr(record, "text_archive_digest", "") or "")
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            return None
        return load_text_skill_files(raw)
    except Exception:
        return None


def _files_fingerprint(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, text in sorted(files.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def materialize_text_skills(
    ctx: Any, *, root: str | None = None
) -> str | None:
    """Write authorized text skills into a cached read-only directory."""

    records = select_text_skills(ctx)
    if not records:
        return None
    try:
        from src.sandbox.skills import SkillCatalog

        catalog = SkillCatalog()
        entries: list[tuple[str, dict[str, str], str]] = []
        used: set[str] = set()
        for record in records:
            name = _skill_name(record.skill_ref)
            if name in used:
                name = f"{name}-{hashlib.sha1(record.skill_ref.encode()).hexdigest()[:6]}"
            used.add(name)
            files = dict(_package_files(record) or {})
            if "SKILL.md" in files:
                skill_markdown = files["SKILL.md"]
            else:
                skill_markdown = catalog.content(record.skill_ref)
            description = _description(skill_markdown, record.skill_ref)
            files["SKILL.md"] = _skill_markdown(skill_markdown, name, description)
            entries.append((name, files, record.content_digest))
        if not entries:
            return None
        # Include every materialized file in the cache key so a reference or
        # script change invalidates the read-only copy as well.
        fingerprint = hashlib.sha256(
            "|".join(
                f"{name}:{digest}:{_files_fingerprint(files)}"
                for name, files, digest in sorted(entries)
            ).encode()
        ).hexdigest()[:20]
        target = root or os.path.join(
            str(settings.STORAGE_DIR), "skill_materialized", fingerprint
        )
        marker = os.path.join(target, ".complete")
        if os.path.exists(marker):
            return target
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        temp = tempfile.mkdtemp(prefix=".skills-", dir=parent)
        try:
            for name, files, _ in entries:
                skill_dir = os.path.join(temp, name)
                for relative, text in sorted(files.items()):
                    path = os.path.join(skill_dir, *relative.split("/"))
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(text)
                    os.chmod(path, 0o444)
            with open(os.path.join(temp, ".complete"), "w", encoding="utf-8") as handle:
                handle.write(fingerprint)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            os.replace(temp, target)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        return target
    except Exception:
        return None


__all__ = [
    "materialize_text_skills",
    "select_text_skills",
]
