from __future__ import annotations

from unittest.mock import patch

import src.circuit.index_lock as index_lock


class _FakeMsvcrt:
    LK_LOCK = 1
    LK_UNLCK = 2

    def __init__(self):
        self.calls = []

    def locking(self, descriptor, operation, length):
        self.calls.append((descriptor, operation, length))


def test_windows_backend_import_and_lock_smoke(tmp_path):
    fake_msvcrt = _FakeMsvcrt()

    with (
        patch.object(index_lock, "_fcntl", None),
        patch.object(index_lock, "_msvcrt", fake_msvcrt),
        index_lock.circuit_index_read_lock(str(tmp_path)),
    ):
        pass

    assert [operation for _descriptor, operation, _length in fake_msvcrt.calls] == [
        fake_msvcrt.LK_LOCK,
        fake_msvcrt.LK_UNLCK,
    ]
