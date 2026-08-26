from __future__ import annotations

import builtins
import importlib.util
import sys
import threading
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


def test_module_imports_and_locks_when_fcntl_is_unavailable(tmp_path, monkeypatch):
    fake_msvcrt = _FakeMsvcrt()
    real_import = builtins.__import__

    def windows_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fcntl":
            raise ImportError("fcntl is unavailable on Windows")
        if name == "msvcrt":
            return fake_msvcrt
        return real_import(name, globals, locals, fromlist, level)

    module_name = "_windows_index_lock_smoke"
    spec = importlib.util.spec_from_file_location(module_name, index_lock.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(builtins, "__import__", windows_import)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        with module.circuit_index_write_lock(str(tmp_path)):
            pass
    finally:
        sys.modules.pop(module_name, None)

    assert module._fcntl is None
    assert module._msvcrt is fake_msvcrt


def test_symlink_and_real_storage_roots_share_one_lock(tmp_path):
    real_root = tmp_path / "real-circuits"
    real_root.mkdir()
    symlink_root = tmp_path / "linked-circuits"
    symlink_root.symlink_to(real_root, target_is_directory=True)
    writer_entered = threading.Event()
    release_writer = threading.Event()
    reader_entered = threading.Event()

    def hold_writer():
        with index_lock.circuit_index_write_lock(str(real_root)):
            writer_entered.set()
            release_writer.wait(3)

    def enter_reader():
        with index_lock.circuit_index_read_lock(str(symlink_root)):
            reader_entered.set()

    writer = threading.Thread(target=hold_writer)
    reader = threading.Thread(target=enter_reader)
    writer.start()
    assert writer_entered.wait(1)
    reader.start()
    try:
        assert not reader_entered.wait(0.2)
    finally:
        release_writer.set()
        writer.join(3)
        reader.join(3)

    assert reader_entered.is_set()
