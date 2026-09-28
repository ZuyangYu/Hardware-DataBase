"""Artifact manifests and content/path safety checks for sandbox I/O.

An artifact is addressed by an opaque ID.  The helpers in this module never
accept a host path as an artifact reference and never extract an archive.  The
broker is responsible for storage and ACLs; this module supplies the shared
validation primitives.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import src.settings as settings


class ArtifactRejected(ValueError):
    """Artifact input or metadata failed a bounded safety check."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = str(reason or "invalid")[:64]
        super().__init__(message or self.reason)


_ARTIFACT_ID_RE = re.compile(r"^art-[a-f0-9]{16,64}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9._\u4e00-\u9fff-]{0,119}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

DEFAULT_ARCHIVE_MAX_FILES = 10_000
DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_COMPRESSION_RATIO = 1000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def new_artifact_id() -> str:
    import secrets

    return f"art-{secrets.token_hex(16)}"


def allowed_extensions() -> set[str]:
    raw = str(getattr(settings, "SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS", "") or "")
    return {item.strip().lower().lstrip(".") for item in raw.split(",") if item.strip()}


def validate_artifact_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _ARTIFACT_ID_RE.fullmatch(text):
        raise ArtifactRejected("artifact_id", "artifact_id is not a valid opaque reference")
    return text


def validate_filename(value: Any) -> str:
    """Strictly validate one basename; do not silently sanitize paths."""

    text = str(value or "")
    if not text or _CONTROL_RE.search(text) or "\x00" in text:
        raise ArtifactRejected("filename", "artifact filename is invalid")
    if text.startswith(('/', "\\")) or "/" in text or "\\" in text:
        raise ArtifactRejected("path", "artifact filename must be a basename")
    if text in {".", ".."} or ".." in PurePosixPath(text).parts:
        raise ArtifactRejected("path", "artifact filename contains traversal")
    if not _SAFE_NAME_RE.fullmatch(text):
        raise ArtifactRejected("filename", "artifact filename contains unsupported characters")
    extension = text.rsplit(".", 1)[-1].lower() if "." in text else ""
    if extension not in allowed_extensions():
        raise ArtifactRejected("extension", f"artifact extension '{extension or '-'}' is not allowed")
    return text


def validate_digest(declared: Any, content: bytes) -> str:
    actual = sha256_bytes(content)
    value = str(declared or "").strip().lower().removeprefix("sha256:")
    if not _SHA256_RE.fullmatch(value) or value != actual:
        raise ArtifactRejected("hash", "declared sha256 does not match artifact content")
    return actual


def validate_content(content: Any, *, output: bool = False) -> bytes:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ArtifactRejected("content", "artifact content must be bytes")
    data = bytes(content)
    if not data:
        raise ArtifactRejected("empty", "artifact is empty")
    setting = "SANDBOX_MAX_OUTPUT_BYTES" if output else "SANDBOX_MAX_INPUT_BYTES"
    maximum = max(1, int(getattr(settings, setting, 50 * 1024 * 1024)))
    if len(data) > maximum:
        raise ArtifactRejected("size", f"artifact exceeds the {maximum} byte limit")
    return data


def validate_storage_ref(base_dir: str, storage_ref: str) -> str:
    """Resolve a broker-relative path without following a symlink escape."""

    base = os.path.abspath(base_dir)
    candidate = os.path.abspath(os.path.join(base, str(storage_ref or "")))
    if candidate != base and not candidate.startswith(base + os.sep):
        raise ArtifactRejected("path", "artifact storage reference escapes the broker")
    relative_parts = os.path.relpath(candidate, base).split(os.sep)
    if any(part in {"", ".", ".."} for part in relative_parts):
        raise ArtifactRejected("path", "artifact storage reference is invalid")
    # Check each existing component.  A symlink in the path would make an
    # otherwise safe-looking ID resolve outside the broker root.
    current = base
    for part in relative_parts:
        current = os.path.join(current, part)
        if os.path.lexists(current) and os.path.islink(current):
            raise ArtifactRejected("symlink", "artifact storage reference contains a symlink")
    return candidate


def build_registered_scanner():
    """Resolve the deployment's registered scanner reference.

    ``scanner://builtin/*`` selects the bounded structural scanner shipped with
    this profile.  Any other reference must be injected by the deployment (for
    example a sidecar or in-process AV client); returning ``None`` keeps the
    broker's fail-closed behaviour instead of silently scanning nothing.
    """

    ref = str(getattr(settings, "SANDBOX_ARTIFACT_SCANNER_REF", "") or "").strip()
    if not ref:
        return None
    if ref.startswith("scanner://builtin/"):
        return basic_artifact_scanner
    return None


def basic_artifact_scanner(content: bytes, manifest: Any) -> tuple[bool, str]:
    """Bounded structural scanner for the dev-l1 profile.

    Checks the reviewed extension allowlist, the output size cap, embedded NUL
    bytes and (for text formats) UTF-8 decodability.  It is deliberately not an
    antivirus engine: a production deployment must inject a real scanner
    callable for the registered ``SANDBOX_ARTIFACT_SCANNER_REF``.
    """

    filename = str(getattr(manifest, "filename", "") or "")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in allowed_extensions():
        return False, "extension"
    max_bytes = max(1, int(getattr(settings, "SANDBOX_MAX_OUTPUT_BYTES", 50 * 1024 * 1024)))
    if len(content) > max_bytes:
        return False, "size"
    if b"\x00" in content:
        return False, "binary"
    if extension in {"txt", "json", "csv", "md", "kicad_sch", "kicad_pcb", "kicad_pro"}:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return False, "encoding"
    return True, ""


def inspect_archive(
    content: bytes,
    *,
    max_files: int = DEFAULT_ARCHIVE_MAX_FILES,
    max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: int = DEFAULT_ARCHIVE_MAX_COMPRESSION_RATIO,
) -> None:
    """Reject zip-slip, symlink and decompression-bomb characteristics."""

    try:
        with zipfile.ZipFile(__import__("io").BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > max_files:
                raise ArtifactRejected("archive_files", "archive contains too many files")
            total = 0
            for entry in entries:
                name = str(entry.filename or "").replace("\\", "/")
                path = PurePosixPath(name)
                if name.startswith("/") or ".." in path.parts or any(
                    part in {"", "."} for part in path.parts
                ):
                    raise ArtifactRejected("archive_path", "archive contains path traversal")
                mode = (entry.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
                    raise ArtifactRejected("archive_special_file", "archive contains a special file")
                total += max(0, int(entry.file_size))
                if total > max_uncompressed_bytes:
                    raise ArtifactRejected("archive_size", "archive expands beyond the configured limit")
                compressed = max(1, int(entry.compress_size))
                if int(entry.file_size) > compressed * max(1, max_compression_ratio):
                    raise ArtifactRejected("archive_ratio", "archive compression ratio is unsafe")
    except ArtifactRejected:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ArtifactRejected("archive", "artifact archive is malformed") from exc


@dataclass(frozen=True)
class ArtifactManifest:
    artifact_id: str
    task_id: str
    execution_id: str
    owner_id: str
    department_id: str
    filename: str
    media_type: str
    size: int
    sha256: str
    storage_ref: str
    state: str = "staged"
    role: str = "input"
    scan_status: str = "pending"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    expires_at: str = ""
    consumed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_internal: bool = False) -> dict[str, Any]:
        """Return an API-safe manifest; storage paths stay internal by default."""

        result = {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "owner_id": self.owner_id,
            "department_id": self.department_id,
            "filename": self.filename,
            "media_type": self.media_type,
            "size": int(self.size),
            "sha256": self.sha256,
            "state": self.state,
            "role": self.role,
            "scan_status": self.scan_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
            "metadata": dict(self.metadata),
        }
        if include_internal:
            result["storage_ref"] = self.storage_ref
        return result


def infer_media_type(filename: str, supplied: Any = "") -> str:
    value = str(supplied or "").strip().lower()
    if value and len(value) <= 120 and "/" in value and "\n" not in value:
        return value
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def validate_manifest(manifest: ArtifactManifest) -> ArtifactManifest:
    validate_artifact_id(manifest.artifact_id)
    validate_filename(manifest.filename)
    if manifest.role not in {"input", "output", "log"}:
        raise ArtifactRejected("role", "artifact role is invalid")
    if manifest.state not in {"staged", "scanning", "committed", "consumed", "deleted", "rejected"}:
        raise ArtifactRejected("state", "artifact state is invalid")
    return manifest


__all__ = [
    "ArtifactManifest",
    "ArtifactRejected",
    "allowed_extensions",
    "infer_media_type",
    "inspect_archive",
    "new_artifact_id",
    "sha256_bytes",
    "validate_artifact_id",
    "validate_content",
    "validate_digest",
    "validate_filename",
    "validate_manifest",
    "validate_storage_ref",
]
