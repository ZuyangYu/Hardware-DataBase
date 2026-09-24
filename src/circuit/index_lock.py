"""Portable process-safe lock for coherent circuit index publication."""

from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

try:  # POSIX: real shared/exclusive process locks.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised through the Windows smoke double
    _fcntl = None

try:  # Windows: byte-range locking is exclusive for both read and write paths.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - normal on POSIX
    _msvcrt = None


@dataclass
class _LockEntry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    active_count: int = 0


_LOCKS: dict[str, _LockEntry] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_path(root: str) -> str:
    root = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    lock_dir = os.path.join(root, ".index-locks")
    os.makedirs(lock_dir, exist_ok=True)
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()
    return os.path.join(lock_dir, f"root-{digest}.lock")


def _acquire_file_lock(descriptor: int, *, exclusive: bool) -> None:
    if _fcntl is not None:
        operation = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
        _fcntl.flock(descriptor, operation)
        return
    if _msvcrt is not None:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _msvcrt.locking(descriptor, _msvcrt.LK_LOCK, 1)
        return
    raise RuntimeError("No supported filesystem lock backend is available.")


def _release_file_lock(descriptor: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)


@contextmanager
def circuit_index_lock(root: str, *, exclusive: bool) -> Iterator[None]:
    """Lock the authoritative circuit root across readers and publishers.

    Windows has no shared mode in ``msvcrt.locking``; readers therefore use
    the same exclusive byte-range lock there. This costs concurrency but keeps
    the publication boundary correct and importable on every advertised OS.
    """

    path = _lock_path(root)
    with _LOCKS_GUARD:
        entry = _LOCKS.get(path)
        if entry is None:
            entry = _LockEntry()
            _LOCKS[path] = entry
        entry.active_count += 1
    try:
        with entry.lock:
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            acquired = False
            try:
                _acquire_file_lock(descriptor, exclusive=exclusive)
                acquired = True
                yield
            finally:
                if acquired:
                    _release_file_lock(descriptor)
                os.close(descriptor)
    finally:
        with _LOCKS_GUARD:
            entry.active_count -= 1
            if entry.active_count == 0 and _LOCKS.get(path) is entry:
                del _LOCKS[path]


def circuit_index_read_lock(root: str):
    return circuit_index_lock(root, exclusive=False)


def circuit_index_write_lock(root: str):
    return circuit_index_lock(root, exclusive=True)
