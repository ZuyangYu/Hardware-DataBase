"""Atomic file publication (v3 §12.1).

Write to a same-directory temporary file, fsync the data, then ``os.replace``
the target.  Readers never observe a truncated or partially written file, and
a crash mid-write leaves the previous version intact.
"""

from __future__ import annotations

import os
import tempfile


def _fsync_directory(directory: str) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: str, data: bytes, *, mode: int | None = None) -> None:
    """Publish ``data`` at ``path`` atomically."""
    absolute = os.path.abspath(str(path))
    directory = os.path.dirname(absolute) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=directory, prefix=".tmp-", suffix=os.path.basename(absolute)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, absolute)
        _fsync_directory(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: str,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    atomic_write_bytes(str(path), str(text).encode(encoding), mode=mode)
