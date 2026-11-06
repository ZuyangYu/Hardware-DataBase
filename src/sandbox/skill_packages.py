"""Validation and persistence for reviewable Skill source packages.

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

import yaml

import src.settings as settings

from .skills import review_flags


class SkillPackageError(ValueError):
    pass


_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
_MAX_UNPACKED_BYTES = 16 * 1024 * 1024
_MAX_FILES = 128
_MAX_FILE_BYTES = 4 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".lock",
    ".csv", ".xml", ".drawio", ".svg", ".ini", ".cfg", ".sql", ".js", ".ts",
    ".html", ".css",
}
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


@dataclass(frozen=True)
class TextSkillPackage:
    archive_digest: str
    skill_ref: str
    version: str
    description: str
    content: str
    files: tuple[str, ...]
    resources: tuple[tuple[str, str], ...] = ()


_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SKILL_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"\A---\n(.{1,4096}?)\n---(?:\n|\Z)", re.DOTALL)


def _read_text_archive(raw: bytes) -> tuple[dict[str, str], str]:
    """Read one inert text Skill ZIP and return its files plus folder prefix.

    Text Skill assets are stored and reviewed as immutable source.  They are
    never imported or executed by HDB; the agent reads them on demand from the
    materialized read-only Skill directory.
    """
    if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= _MAX_ARCHIVE_BYTES:
        raise SkillPackageError("text Skill ZIP exceeds the 8 MiB limit")
    data = bytes(raw)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise SkillPackageError("text Skill must be a valid ZIP archive") from exc
    files: dict[str, str] = {}
    total = 0
    try:
        for info in archive.infolist():
            name = _safe_name(info.filename.rstrip("/"))
            if info.is_dir():
                continue
            if name in files or len(files) >= _MAX_FILES:
                raise SkillPackageError("text Skill contains duplicate or too many files")
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise SkillPackageError("text Skill symlinks are not allowed")
            if info.file_size > _MAX_FILE_BYTES or total + info.file_size > _MAX_UNPACKED_BYTES:
                raise SkillPackageError("text Skill contains an oversized file")
            if name.lower().split("/")[-1] in _FORBIDDEN_NAMES:
                raise SkillPackageError("text Skill contains a forbidden build recipe")
            if Path(name).suffix.lower() not in _TEXT_SUFFIXES:
                raise SkillPackageError(f"text Skill file type is not allowed: {name}")
            content = archive.read(info)
            total += len(content)
            if total > _MAX_UNPACKED_BYTES:
                raise SkillPackageError("text Skill is too large after unpacking")
            try:
                files[name] = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SkillPackageError(f"text Skill file is not UTF-8: {name}") from exc
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SkillPackageError("text Skill ZIP cannot be read") from exc
    finally:
        archive.close()

    roots = [name for name in files if name == "SKILL.md" or name.endswith("/SKILL.md")]
    if len(roots) != 1:
        raise SkillPackageError("text Skill ZIP must contain exactly one SKILL.md")
    root = roots[0]
    prefix = root.removesuffix("SKILL.md")
    if prefix and (prefix.count("/") != 1 or any(not name.startswith(prefix) for name in files)):
        raise SkillPackageError("text Skill ZIP must contain just one Skill folder")
    return files, prefix


def validate_text_skill_package(data: bytes) -> TextSkillPackage:
    """Validate a text Skill ZIP with optional on-demand resource files.

    All files are inert UTF-8 source.  SKILL.md and its references are kept as
    separate files when materialized, so large rule sets and helper scripts no
    longer have to be squeezed into one conversation-sized prompt block.
    """
    raw = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
    files, prefix = _read_text_archive(raw)
    root = f"{prefix}SKILL.md"
    markdown = files[root].replace("\r\n", "\n").replace("\r", "\n")
    frontmatter = _FRONTMATTER_RE.match(markdown)
    if not frontmatter:
        raise SkillPackageError("SKILL.md must start with YAML name and description")
    try:
        metadata = yaml.safe_load(frontmatter.group(1))
    except yaml.YAMLError as exc:
        raise SkillPackageError("SKILL.md frontmatter is invalid YAML") from exc
    if not isinstance(metadata, dict):
        raise SkillPackageError("SKILL.md frontmatter must be a mapping")
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or len(name) > 64 or not _SKILL_NAME_RE.fullmatch(name):
        raise SkillPackageError("SKILL.md name must use lowercase letters, digits and hyphens")
    if prefix and prefix[:-1] != name:
        raise SkillPackageError("Skill folder name must match SKILL.md name")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise SkillPackageError("SKILL.md description must be 1-1024 characters")
    declared = metadata.get("metadata") or {}
    if not isinstance(declared, dict):
        raise SkillPackageError("SKILL.md metadata must be a mapping")
    version = declared.get("version", "1.0.0")
    if not isinstance(version, str) or not _SKILL_VERSION_RE.fullmatch(version) or ".." in version:
        raise SkillPackageError("SKILL.md metadata.version is invalid")

    names = sorted(value for value in files if value != root)
    combined = markdown + "".join(
        f"\n\n---\n### 随附资料：{value.removeprefix(prefix)}\n\n{files[value]}"
        for value in names
    )
    return TextSkillPackage(
        archive_digest=hashlib.sha256(raw).hexdigest(), skill_ref=f"skill/{name}",
        version=version, description=description.strip(), content=combined,
        files=tuple(sorted(value.removeprefix(prefix) for value in files)),
        resources=tuple(
            sorted((value.removeprefix(prefix), files[value]) for value in files)
        ),
    )


def load_text_skill_files(data: bytes) -> dict[str, str]:
    """Return ``relative path -> text`` for one validated text Skill archive."""

    return dict(validate_text_skill_package(data).resources)


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

    def save(self, skill_ref: str, package: SkillPackage | TextSkillPackage, data: bytes) -> str:
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

    def delete(self, storage_ref: str) -> bool:
        """Best-effort removal of one stored source archive.

        Used when a revoked Skill is deleted or an upload fails before the
        catalog row exists.  Audit rows and task history are unaffected.
        """
        value = str(storage_ref or "")
        if not value:
            return False
        path = os.path.abspath(os.path.join(self.root, value))
        if not path.startswith(self.root + os.sep) or not os.path.isfile(path):
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        parent = os.path.dirname(path)
        try:
            if parent != self.root and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass
        return True


__all__ = ["SkillPackage", "TextSkillPackage", "SkillPackageError", "SkillPackageStore", "load_text_skill_files", "validate_skill_package", "validate_text_skill_package", "validate_requirements_lock"]
