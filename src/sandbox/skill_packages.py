"""Validation and persistence for administrator-uploaded executable Skills.

The HDB process stores and validates the source package, but never imports or
executes it.  The package is later sent to the independent Runner, which uses
a fixed Dockerfile template to build an immutable bundle image.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import zipfile
from dataclasses import dataclass
from pathlib import Path

import src.settings as settings

from .skills import review_flags


class SkillPackageError(ValueError):
    pass


_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
_MAX_UNPACKED_BYTES = 16 * 1024 * 1024
_MAX_FILES = 128
_MAX_FILE_BYTES = 4 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".lock"}
_FORBIDDEN_NAMES = {
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".env",
    "setup.py",
    "pyproject.toml",
    "package.json",
}
_LOCK_LINE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}==[A-Za-z0-9][A-Za-z0-9_.!+-]{0,99}"
    r"(?:[ \t]+--hash=sha256:[0-9a-fA-F]{64}){1,4}$"
)


def validate_requirements_lock(data: bytes) -> int:
    if not data.strip():
        return 0
    if len(data) > 128 * 1024:
        raise SkillPackageError("requirements.lock is too large")
    lines = [line.strip() for line in data.decode("utf-8").splitlines() if line.strip()]
    if len(lines) > 64 or any(not _LOCK_LINE_RE.fullmatch(line) for line in lines):
        raise SkillPackageError("requirements.lock requires pinned wheel versions and SHA-256 hashes")
    return len(lines)


@dataclass(frozen=True)
class SkillPackage:
    archive_digest: str
    skill_markdown: str
    entrypoint: bytes
    files: tuple[str, ...]
    review_flags: tuple[str, ...]
    requirements_declared: bool = False


def _safe_name(name: str) -> str:
    value = str(name or "").replace("\\", "/").strip()
    if (
        not value
        or value.startswith("/")
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or not _NAME_RE.fullmatch(value)
    ):
        raise SkillPackageError("Skill package contains an unsafe path")
    return value


def validate_skill_package(data: bytes) -> SkillPackage:
    if not isinstance(data, (bytes, bytearray)):
        raise SkillPackageError("Skill package must be bytes")
    raw = bytes(data)
    if not raw or len(raw) > _MAX_ARCHIVE_BYTES:
        raise SkillPackageError("Skill package exceeds the archive size limit")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as exc:
        raise SkillPackageError("Skill package must be a valid ZIP archive") from exc
    names: list[str] = []
    contents: dict[str, bytes] = {}
    total = 0
    try:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = _safe_name(info.filename)
            if name in names:
                raise SkillPackageError("Skill package contains duplicate paths")
            names.append(name)
            if len(names) > _MAX_FILES:
                raise SkillPackageError("Skill package contains too many files")
            if info.file_size > _MAX_FILE_BYTES:
                raise SkillPackageError("Skill package contains an oversized file")
            # ZIP symlinks are never accepted, even when their target looks safe.
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise SkillPackageError("Skill package symlinks are not allowed")
            lower = name.lower()
            if lower.split("/")[-1] in _FORBIDDEN_NAMES:
                raise SkillPackageError("Skill package contains a forbidden build recipe")
            suffix = Path(name).suffix.lower()
            if suffix not in _TEXT_SUFFIXES:
                raise SkillPackageError(f"Skill package file type is not allowed: {name}")
            content = archive.read(info)
            total += len(content)
            if total > _MAX_UNPACKED_BYTES:
                raise SkillPackageError("Skill package is too large after unpacking")
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SkillPackageError(f"Skill package file is not UTF-8 text: {name}") from exc
            contents[name] = content
    finally:
        archive.close()
    required = {"SKILL.md", "entrypoint.py"}
    if not required.issubset(contents):
        raise SkillPackageError("Skill package must contain SKILL.md and entrypoint.py at its root")
    dependency_count = validate_requirements_lock(contents.get("requirements.lock", b""))
    markdown = contents["SKILL.md"].decode("utf-8")
    if not markdown.strip():
        raise SkillPackageError("SKILL.md cannot be empty")
    entrypoint = contents["entrypoint.py"]
    if b"\x00" in entrypoint:
        raise SkillPackageError("entrypoint.py contains a NUL byte")
    flags = list(review_flags(markdown))
    for name, content in contents.items():
        if name.endswith(".py"):
            flags.extend(review_flags(content.decode("utf-8")))
    return SkillPackage(
        archive_digest=digest,
        skill_markdown=markdown,
        entrypoint=entrypoint,
        files=tuple(sorted(names)),
        review_flags=tuple(sorted(set(flags))),
        requirements_declared=dependency_count > 0,
    )


class SkillPackageStore:
    """Private HDB-side source archive store; it never executes package bytes."""

    def __init__(self, root: str | None = None) -> None:
        self.root = os.path.abspath(str(root or getattr(settings, "SANDBOX_SKILL_STORAGE_DIR", os.path.join(settings.STORAGE_DIR, "sandbox_skills"))))
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @staticmethod
    def _ref_part(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or ""))[:100] or "skill"

    def save(self, skill_ref: str, package: SkillPackage, data: bytes) -> str:
        directory = os.path.join(self.root, self._ref_part(skill_ref))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, f"{package.archive_digest}.zip")
        if not os.path.exists(path):
            temporary = f"{path}.upload-{secrets.token_hex(8)}"
            with open(temporary, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        return os.path.relpath(path, self.root)

    def read(self, storage_ref: str) -> bytes:
        value = str(storage_ref or "")
        path = os.path.abspath(os.path.join(self.root, value))
        if not path.startswith(self.root + os.sep) or not os.path.isfile(path):
            raise SkillPackageError("Skill package source is unavailable")
        with open(path, "rb") as handle:
            data = handle.read(_MAX_ARCHIVE_BYTES + 1)
        if len(data) > _MAX_ARCHIVE_BYTES:
            raise SkillPackageError("Skill package source exceeds the archive size limit")
        return data


__all__ = ["SkillPackage", "SkillPackageError", "SkillPackageStore", "validate_skill_package", "validate_requirements_lock"]
