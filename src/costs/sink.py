"""Bounded asynchronous cost sink with atomic disk spool and idempotent replay.

The model path only uses put_nowait. SDK/DB/fs operations run on a daemon
consumer. Events queued before disk persistence can still be lost to SIGKILL;
start/end mismatches and sink metrics describe this boundary explicitly.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import threading
import time
import uuid
try:
    import fcntl
except ImportError:
    fcntl = None
from pathlib import Path
from functools import partial

import src.settings as settings
from src.quality.store import dumps, sha256
from .store import CostConflict, CostStore


class CostSink:
    def __init__(self, store_factory=CostStore, *, spool: Path, replay_root: Path | None = None, max_events=1024, max_bytes=64 * 1024 * 1024):
        self.store_factory = store_factory
        self.spool = spool
        self.replay_root = replay_root
        self.max_bytes = max_bytes
        self.queue = queue.Queue(maxsize=max_events)
        self.stop = threading.Event()
        self.counts = {"submitted": 0, "persisted": 0, "spooled": 0, "spool_full": 0, "write_failures": 0, "dropped": 0, "corrupt": 0}
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="llm-cost-sink", daemon=True)
        self.thread.start()

    def count(self, key):
        with self.lock:
            self.counts[key] += 1
        try:
            from src.observability.metrics import counter

            counter("hdb.cost.sink.events", attributes={"status": key})
        except Exception:
            pass

    def emit(self, event):
        try:
            self.queue.put_nowait(event)
            self.count("submitted")
            return True
        except queue.Full:
            self.count("dropped")
            return False

    def snapshot(self):
        with self.lock:
            return {**self.counts, "queue_depth": self.queue.qsize(), "queue_capacity": self.queue.maxsize,
                    "pid": os.getpid(), "scope": "process_local", "durability": "async_spool"}

    def _write_spool(self, event):
        self.spool.mkdir(parents=True, exist_ok=True)
        files = [path for path in self.spool.glob("*") if path.is_file()]
        data = dumps(event).encode()
        if sum(p.stat().st_size for p in files) + len(data) > self.max_bytes:
            self.count("spool_full")
            return None
        path = self.spool / (sha256(event["event_id"]) + ".json")
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("xb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)
            fd = os.open(self.spool, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            tmp.unlink(missing_ok=True)
        self.count("spooled")
        return path

    def _replay_directory(self, directory):
        files = sorted(directory.glob("*.json"))[:100]
        if not files:
            return
        store = self.store_factory()
        for path in files:
            try:
                event = json.loads(path.read_text())
                store.ingest(event)
                path.unlink()
                self.count("persisted")
            except (ValueError, KeyError, CostConflict):
                path.replace(path.with_suffix(".corrupt"))
                self.count("corrupt")

    def _replay(self):
        self._replay_directory(self.spool)
        if fcntl and self.replay_root and self.replay_root.is_dir():
            for directory in self.replay_root.iterdir():
                if directory == self.spool or not directory.is_dir():
                    continue
                # Active consumers hold this lock for their entire lifetime.
                # Orphan directories can therefore be adopted across restarts
                # and replicas without racing a writer or deleting its files.
                with (directory / ".consumer.lock").open("a+b") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    self._replay_directory(directory)

    def _run(self):
        lock = None
        try:
            self.spool.mkdir(parents=True, exist_ok=True)
            lock = (self.spool / ".consumer.lock").open("a+b")
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if lock:
                lock.close()
            lock = None
            self.count("write_failures")
        try:
            self._consume()
        finally:
            if lock:
                lock.close()

    def _consume(self):
        next_replay = 0.0
        while not self.stop.is_set() or not self.queue.empty():
            event = None
            try:
                event = self.queue.get(timeout=0.1)
                try:
                    path = self._write_spool(event)
                except (OSError, ValueError):
                    self.count("write_failures")
                    path = None
                try:
                    self.store_factory().ingest(event)
                    if path:
                        path.unlink(missing_ok=True)
                    self.count("persisted")
                except Exception:
                    self.count("write_failures")
                    if path is None:
                        self.count("dropped")
            except queue.Empty:
                pass
            except Exception as exc:
                logging.getLogger(__name__).error("cost sink failure: %s", type(exc).__name__)
            finally:
                if event is not None:
                    self.queue.task_done()
            if time.monotonic() >= next_replay:
                try:
                    self._replay()
                except Exception:
                    self.count("write_failures")
                next_replay = time.monotonic() + 2

    def close(self, timeout=5):
        self.stop.set()
        self.thread.join(timeout)


_SINKS: dict[tuple, CostSink] = {}
_LOCK = threading.Lock()


def spool_root(backend, dsn, storage_dir):
    destination = dsn if backend == "postgres" else os.path.abspath(os.path.join(storage_dir, "llm-costs.db"))
    return Path(storage_dir) / "cost-spool" / sha256([backend, destination])[:24]


def get_sink() -> CostSink:
    key = (os.getpid(), settings.CONTROL_PLANE_BACKEND, settings.CONTROL_PLANE_DATABASE_URL, settings.STORAGE_DIR)
    with _LOCK:
        if key not in _SINKS:
            root = spool_root(key[1], key[2], key[3])
            factory = partial(CostStore, backend=key[1], dsn=key[2], db_path=str(Path(key[3]) / "llm-costs.db"))
            # A settings reload must not reroute already queued events to the
            # new storage target. Each sink keeps its immutable destination.
            _SINKS[key] = CostSink(factory, spool=root / f"{os.getpid()}-{uuid.uuid4().hex[:8]}", replay_root=root)
        return _SINKS[key]


def shutdown_sinks():
    with _LOCK:
        sinks = list(_SINKS.values())
        _SINKS.clear()
    for sink in sinks:
        sink.close()


def sink_snapshots():
    with _LOCK:
        return [sink.snapshot() for key, sink in _SINKS.items() if key[0] == os.getpid()]


atexit.register(shutdown_sinks)
