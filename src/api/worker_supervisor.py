"""Supervise optional same-host workers; Docker deploys them as services."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass


@dataclass
class Child:
    module: str
    process: subprocess.Popen | None = None
    failures: int = 0
    next_start: float = 0
    started_at: float = 0


class WorkerSupervisor:
    def __init__(self, modules: list[str], cwd: str):
        self.children = [Child(module) for module in modules]
        self.cwd = cwd
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        for child in self.children:
            now = time.monotonic()
            if child.process is not None:
                code = child.process.poll()
                if code is None:
                    continue
                child.process.wait()
                if now - child.started_at > 60:
                    child.failures = 0
                child.failures += 1
                child.next_start = now + min(60, 2 ** min(child.failures, 6))
                child.process = None
                logging.getLogger("RAG").error("Worker %s exited (%s); restarting with backoff", child.module, code)
            if not self._stop.is_set() and now >= child.next_start:
                # Inherit stdout/stderr: the host/container log driver owns rotation.
                child.process = subprocess.Popen([sys.executable, "-m", child.module], cwd=self.cwd)
                child.started_at = now

    def start(self) -> None:
        self.tick()
        self._thread = threading.Thread(target=self._run, name="worker-supervisor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(1):
            try:
                self.tick()
            except OSError:
                logging.getLogger("RAG").exception("Worker restart failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        for child in self.children:
            if child.process is not None and child.process.poll() is None:
                child.process.terminate()
        deadline = time.monotonic() + 20
        for child in self.children:
            if child.process is not None:
                try:
                    child.process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.process.kill()
                    child.process.wait()
