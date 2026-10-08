"""Attempt-scoped checkpoint namespace copying.

Verified behavior (P0-002b):

- ``BaseCheckpointSaver.put`` copies a checkpoint into a new thread/namespace
  while preserving its id and wiring the target parent chain;
- pending writes are stored separately from the checkpoint and are NOT copied
  by ``put`` — take-over must copy them explicitly or they are lost;
- resuming a copied namespace with new input continues the copied conversation
  history instead of starting empty.
- delta channels require ancestor checkpoints and writes through their nearest
  snapshots (or the chain root), including on successful-turn commits.

These helpers intentionally operate on the official saver API and never
reimplement the checkpoint protocol.
"""

from __future__ import annotations

from typing import Any

RunnableConfig = dict[str, Any]


def pending_write_parts(write: Any) -> tuple[str, str, Any]:
    """Normalize a pending write to ``(task_id, channel, value)``.

    LangGraph stores pending writes as plain ``(task_id, channel, value)``
    tuples; the attribute form is tolerated for forward compatibility.
    """
    if isinstance(write, tuple) and len(write) >= 3:
        return str(write[0]), str(write[1]), write[2]
    return (
        str(getattr(write, "task_id", "") or ""),
        str(getattr(write, "channel", "") or ""),
        getattr(write, "value", None),
    )


def copy_checkpoint_namespace(
    saver: Any,
    source_config: RunnableConfig,
    target_thread_id: str,
    *,
    target_checkpoint_ns: str = "",
    copy_pending_writes: bool = True,
) -> RunnableConfig | None:
    """Copy the checkpoint addressed by ``source_config`` into another thread.

    Returns the saved target config (including the new ``checkpoint_id``), or
    ``None`` when the source checkpoint does not exist.  The source thread is
    left untouched.
    """
    source_tuple = saver.get_tuple(source_config)
    if source_tuple is None:
        return None
    # DeepAgents uses DeltaChannel for messages/files. A head checkpoint may
    # contain no value at all: its state lives in ancestor pending writes.
    # Copy the on-path chain through every required snapshot (or the root),
    # using the official saver API so no reducer/storage format is guessed.
    chain = [source_tuple]
    remaining = set((source_tuple.metadata or {}).get("counters_since_delta_snapshot", {}))
    remaining.difference_update(source_tuple.checkpoint.get("channel_values", {}))
    cursor = source_tuple
    seen: set[str] = set()
    while remaining and cursor.parent_config is not None:
        parent_key = repr(cursor.parent_config.get("configurable", {}))
        if parent_key in seen:
            raise RuntimeError("cyclic delta checkpoint ancestry")
        seen.add(parent_key)
        parent = saver.get_tuple(cursor.parent_config)
        if parent is None:
            raise RuntimeError("missing delta checkpoint ancestor")
        chain.append(parent)
        remaining.difference_update(parent.checkpoint.get("channel_values", {}))
        cursor = parent

    target_config: RunnableConfig = {
        "configurable": {
            "thread_id": str(target_thread_id),
            "checkpoint_ns": str(target_checkpoint_ns or ""),
        }
    }
    for checkpoint_tuple in reversed(chain):
        checkpoint = checkpoint_tuple.checkpoint
        new_versions = checkpoint.get("channel_versions", {})
        saved = saver.put(target_config, checkpoint, checkpoint_tuple.metadata, new_versions)
        # Ancestor writes are committed history, even when the caller asks not
        # to replay the head's still-pending writes.
        if copy_pending_writes or checkpoint_tuple is not source_tuple:
            grouped: dict[str, list[tuple[str, Any]]] = {}
            for write in checkpoint_tuple.pending_writes or []:
                task_id, channel, value = pending_write_parts(write)
                grouped.setdefault(task_id, []).append((channel, value))
            for task_id, writes in grouped.items():
                saver.put_writes(saved, writes, task_id=task_id)
        target_config = saved
    return saved


def copy_latest_checkpoint(
    saver: Any,
    source_thread_id: str,
    target_thread_id: str,
    *,
    copy_pending_writes: bool = True,
) -> RunnableConfig | None:
    """Copy the latest root-namespace checkpoint of a conversation thread.

    Ordinary channels need only the head. Delta channels also need on-path
    ancestors through their nearest snapshots; non-root namespaces are not
    cloned. Superseded attempts remain isolated and can be safely purged once
    the target owns its independent reconstruction chain.
    """
    source_config: RunnableConfig = {"configurable": {"thread_id": str(source_thread_id), "checkpoint_ns": ""}}
    return copy_checkpoint_namespace(
        saver,
        source_config,
        target_thread_id,
        target_checkpoint_ns="",
        copy_pending_writes=copy_pending_writes,
    )
