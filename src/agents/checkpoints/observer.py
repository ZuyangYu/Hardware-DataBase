"""Checkpoint-write observation for recoverable pointers (P0-003).

The conversation authority records a ``recoverable_checkpoint_ref`` after every
successful graph-state write so a crash take-over forks from the latest durable
checkpoint instead of the last business commit.  The pointer is written by the
authority transaction (CAS on owner/token/generation); this module only reports
what the saver persisted.

The observer patches one saver instance and filters by the attempt thread so
concurrent turns on the shared saver never observe each other.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

ObserverCallback = Callable[[dict[str, Any]], None]


def _record_write_latency(seconds: float) -> None:
    try:
        from src.observability.metrics import histogram

        histogram(
            "hdb.checkpoint.write_latency",
            max(0.0, float(seconds)),
            description="Agent checkpoint write latency per graph step",
        )
    except Exception:
        pass


class CheckpointObserverHandle:
    """Installed wrappers; ``uninstall`` restores the original saver methods."""

    def __init__(self, saver: Any, originals: dict[str, Any]) -> None:
        self._saver = saver
        self._originals = dict(originals)
        self._lock = threading.Lock()

    def uninstall(self) -> None:
        with self._lock:
            for name, original in self._originals.items():
                setattr(self._saver, name, original)
            self._originals.clear()

    def __enter__(self) -> "CheckpointObserverHandle":
        return self

    def __exit__(self, *_args) -> None:
        self.uninstall()


def _config_parts(config: Any) -> tuple[str, str, str]:
    if not isinstance(config, dict):
        return "", "", ""
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return "", "", ""
    return (
        str(configurable.get("thread_id") or ""),
        str(configurable.get("checkpoint_ns") or ""),
        str(configurable.get("checkpoint_id") or ""),
    )


def install_checkpoint_observer(
    saver: Any,
    callback: ObserverCallback,
    *,
    thread_filter: str = "",
    namespace_filter: str = "",
) -> CheckpointObserverHandle:
    """Patch ``put``/``aput`` on ``saver`` to report persisted checkpoints.

    Only calls matching ``thread_filter``/``namespace_filter`` are reported.
    Callback failures are swallowed: observation must never break a graph run.
    """
    originals: dict[str, Any] = {}

    def _report(saved_config: Any) -> None:
        try:
            thread_id, checkpoint_ns, checkpoint_id = _config_parts(saved_config)
            if not checkpoint_id:
                return
            if thread_filter and thread_id != thread_filter:
                return
            if namespace_filter and checkpoint_ns != namespace_filter:
                return
            callback(
                {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            )
        except Exception:
            pass

    original_put = getattr(saver, "put", None)
    if original_put is not None:
        originals["put"] = original_put

        def put(config, checkpoint, metadata, new_versions):
            started = time.monotonic()
            saved = original_put(config, checkpoint, metadata, new_versions)
            _record_write_latency(time.monotonic() - started)
            _report(saved)
            return saved

        saver.put = put

    original_aput = getattr(saver, "aput", None)
    if original_aput is not None:
        originals["aput"] = original_aput

        async def aput(config, checkpoint, metadata, new_versions):
            started = time.monotonic()
            saved = await original_aput(config, checkpoint, metadata, new_versions)
            _record_write_latency(time.monotonic() - started)
            _report(saved)
            return saved

        saver.aput = aput

    return CheckpointObserverHandle(saver, originals)


__all__ = ["CheckpointObserverHandle", "install_checkpoint_observer"]
