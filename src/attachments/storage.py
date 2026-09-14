"""Private blob storage for attachment source files.

Layout (design §10 / §17):

    CHAT_ATTACHMENT_STORAGE_DIR/
        sources/<session_id>/<asset_id>/source<ext>     uploaded bytes
        circuits/<session_id>/<asset_id>/...            EDF designs (CircuitStore root)
        table_indexes/<session_id>/<asset_id>/...       xlsx index databases

Files are addressed by ``storage_key`` persisted on the asset row; callers
never build paths from user input. Every path is validated to stay inside the
configured root (``safe_child_path``).
"""

from __future__ import annotations

import hashlib
import os
import tempfile

import src.settings
from src.attachments.models import AttachmentQuotaExceeded
from src.ingestion.kb_paths import safe_child_path


def _root() -> str:
    root = src.settings.CHAT_ATTACHMENT_STORAGE_DIR
    os.makedirs(root, exist_ok=True)
    return root


def sha256_of_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def asset_source_path(session_id: int, asset_id: str, extension: str, *, create: bool = False) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    parent = safe_child_path(_root(), "sources", str(int(session_id)), asset_id)
    if create:
        os.makedirs(parent, exist_ok=True)
    return os.path.join(parent, f"source{ext}")


def asset_circuit_root(session_id: int) -> str:
    """Root for an attachment-scoped ``CircuitStore`` (never a fake kb_name)."""
    path = safe_child_path(_root(), "circuits", str(int(session_id)), create=True)
    return path


def asset_table_index_dir(session_id: int, asset_id: str, *, create: bool = False) -> str:
    return safe_child_path(
        _root(), "table_indexes", str(int(session_id)), asset_id, create=create
    )


def write_source_stream(session_id: int, asset_id: str, extension: str, stream) -> tuple[str, str, int]:
    """Stream an upload to private storage; return (path, sha256, size).

    Streaming write bounds peak memory for large files; the digest is
    computed over the same bytes that land on disk, so hash and size always
    describe the stored object.
    """
    target = asset_source_path(session_id, asset_id, extension, create=True)
    digest = hashlib.sha256()
    size = 0
    fd, tmp_path = tempfile.mkstemp(prefix=".upload-", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024) if hasattr(stream, "read") else None
                if chunk is None:
                    data = stream.read(1024 * 1024) if hasattr(stream, "read") else b""
                else:
                    data = chunk
                if not data:
                    break
                # Hard cap during write: never buffer an oversized upload.
                size += len(data)
                if size > int(src.settings.CHAT_ATTACHMENT_MAX_BYTES):
                    raise AttachmentQuotaExceeded(
                        "attachment exceeds the configured size limit"
                    )
                digest.update(data)
                out.write(data)
            os.fsync(out.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target, digest.hexdigest(), size


def write_source_bytes(session_id: int, asset_id: str, extension: str, content: bytes) -> tuple[str, str, int]:
    import io

    return write_source_stream(session_id, asset_id, extension, io.BytesIO(content))


def read_source_bytes(storage_key: str) -> bytes:
    path = resolve_storage_key(storage_key)
    with open(path, "rb") as fh:
        return fh.read()


def resolve_storage_key(storage_key: str) -> str:
    """Map a persisted storage key back to a validated absolute path."""
    raw = str(storage_key or "").strip()
    if not raw:
        raise ValueError("empty storage key")
    # Accept old manifests that stored an absolute path, but only when the
    # path is already inside the configured private root. External callers
    # still cannot use this as a local-path escape hatch.
    if os.path.isabs(raw):
        absolute = os.path.abspath(raw)
        root = os.path.abspath(_root())
        if os.path.commonpath([root, absolute]) != root:
            raise ValueError("storage path escapes the configured storage root")
        return absolute
    key = raw.lstrip("/")
    if not key:
        raise ValueError("empty storage key")
    return safe_child_path(_root(), *key.split("/"))


def to_storage_key(path: str | os.PathLike[str]) -> str:
    """Convert an internal root-contained path to a portable storage key."""
    absolute = os.path.abspath(os.fspath(path))
    root = os.path.abspath(_root())
    if os.path.commonpath([root, absolute]) != root:
        raise ValueError("storage path escapes the configured storage root")
    return os.path.relpath(absolute, root).replace(os.sep, "/")


def delete_storage_key(storage_key: str) -> bool:
    try:
        path = resolve_storage_key(storage_key)
    except Exception:
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    # Prune now-empty asset/session directories, bounded walk upwards.
    try:
        parent = os.path.dirname(path)
        root = os.path.abspath(_root())
        while os.path.commonpath([root, parent]) == root and parent != root:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
            else:
                break
    except OSError:
        pass
    return True


def delete_session_storage(session_id: int) -> bool:
    """Best-effort recursive removal of a session's attachment storage."""
    import shutil

    root = os.path.abspath(_root())
    removed = False
    for namespace in ("sources", "circuits", "table_indexes"):
        resolved = safe_child_path(root, namespace, str(int(session_id)))
        if os.path.isdir(resolved):
            shutil.rmtree(resolved, ignore_errors=True)
            removed = True
    return removed
