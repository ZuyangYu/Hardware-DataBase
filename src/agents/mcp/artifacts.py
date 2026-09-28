"""Validated candidate-artifact storage for the MCP write channel.

The external service returns an artifact *reference* (filename + base64 body +
sha256).  Nothing is accepted until the extension whitelist, the size cap and
the declared hash all pass; only then is the body written under the dedicated
artifact directory.  Originals are never opened or modified by this module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import src.settings as settings


class McpArtifactRejected(Exception):
    """Artifact refused by policy; ``reason`` is a bounded metric label."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = str(reason)
        super().__init__(message or self.reason)


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\u4e00-\u9fff-]+")


def allowed_extensions() -> set[str]:
    raw = str(getattr(settings, "MCP_ARTIFACT_ALLOWED_EXTENSIONS", "") or "")
    return {item.strip().lower().lstrip(".") for item in raw.split(",") if item.strip()}


def sanitize_filename(filename: Any) -> str:
    """Keep one safe basename; path separators and control chars are dropped."""

    text = str(filename or "").replace("\\", "/")
    base = os.path.basename(text).strip()
    base = _SAFE_NAME_RE.sub("_", base).strip("._")
    return (base or "artifact")[:120]


@dataclass(frozen=True)
class McpArtifactRef:
    artifact_ref: str
    filename: str
    sha256: str
    bytes: int
    media_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_ref": self.artifact_ref,
            "filename": self.filename,
            "sha256": self.sha256,
            "bytes": int(self.bytes),
            "media_type": self.media_type,
        }


class McpArtifactStore:
    """Filesystem store for validated candidate artifacts."""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = os.path.abspath(
            base_dir or getattr(settings, "MCP_ARTIFACT_STORAGE_DIR", "")
        )

    def save(
        self,
        *,
        user_id: int,
        proposal_id: str,
        filename: Any,
        content_base64: Any = None,
        content_bytes: bytes | None = None,
        sha256: Any = "",
        media_type: Any = "",
    ) -> McpArtifactRef:
        if content_bytes is None:
            try:
                content_bytes = base64.b64decode(str(content_base64 or ""), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise McpArtifactRejected("encoding", "artifact content is not valid base64") from exc
        if not content_bytes:
            raise McpArtifactRejected("empty", "artifact is empty")

        safe_name = sanitize_filename(filename)
        extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
        if extension not in allowed_extensions():
            raise McpArtifactRejected(
                "extension", f"artifact extension '{extension or '-'}' is not allowed"
            )

        max_bytes = max(1, int(getattr(settings, "MCP_MAX_ARTIFACT_BYTES", 50 * 1024 * 1024)))
        if len(content_bytes) > max_bytes:
            raise McpArtifactRejected(
                "size", f"artifact exceeds the {max_bytes} byte limit"
            )

        declared = str(sha256 or "").strip().lower()
        actual = hashlib.sha256(content_bytes).hexdigest()
        if not declared or declared != actual:
            raise McpArtifactRejected(
                "hash", "declared sha256 does not match the artifact content"
            )

        relative = os.path.join(str(int(user_id)), str(proposal_id), safe_name)
        target = os.path.join(self.base_dir, relative)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(content_bytes)
        except OSError as exc:
            raise McpArtifactRejected("store", "artifact could not be stored") from exc

        return McpArtifactRef(
            artifact_ref=relative.replace(os.sep, "/"),
            filename=safe_name,
            sha256=actual,
            bytes=len(content_bytes),
            media_type=str(media_type or "")[:120],
        )

    def path_for(self, artifact_ref: Any) -> str:
        candidate = os.path.abspath(
            os.path.join(self.base_dir, str(artifact_ref or ""))
        )
        if not candidate.startswith(self.base_dir + os.sep):
            raise McpArtifactRejected("path", "artifact path escapes the store")
        if not os.path.isfile(candidate):
            raise FileNotFoundError(candidate)
        return candidate

    def read(self, artifact_ref: Any) -> bytes:
        with open(self.path_for(artifact_ref), "rb") as handle:
            return handle.read()

    def size(self, artifact_ref: Any) -> int:
        return os.path.getsize(self.path_for(artifact_ref))


def _payload_from(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        nested = value.get("artifact")
        if isinstance(nested, dict):
            return _payload_from(nested)
        if "content_base64" in value or (
            "filename" in value and "sha256" in value
        ):
            return value
        if value.get("type") == "resource":
            resource = value.get("resource") or {}
            blob = resource.get("blob")
            if blob:
                uri = str(resource.get("uri") or "artifact")
                return {
                    "content_base64": blob,
                    "filename": os.path.basename(uri) or "artifact",
                    "sha256": resource.get("sha256", ""),
                    "media_type": resource.get("mimeType", ""),
                }
        for key in ("structuredContent", "structured_content", "result", "data", "text"):
            payload = _payload_from(value.get(key))
            if payload is not None:
                return payload
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            payload = _payload_from(item)
            if payload is not None:
                return payload
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return _payload_from(parsed)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _payload_from(dump())
        except Exception:  # noqa: BLE001 - unknown content block types are ignored
            return None
    return None


def extract_artifact_payload(result: Any) -> dict[str, Any] | None:
    """Pull ``{filename, content_base64, sha256, media_type}`` from a tool result."""

    if isinstance(result, tuple) and len(result) == 2:
        payload = _payload_from(result[1])
        if payload is not None:
            return payload
        return _payload_from(result[0])
    return _payload_from(result)


__all__ = [
    "McpArtifactRejected",
    "McpArtifactRef",
    "McpArtifactStore",
    "allowed_extensions",
    "extract_artifact_payload",
    "sanitize_filename",
]
