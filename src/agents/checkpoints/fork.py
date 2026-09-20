"""Attempt-scoped checkpoint namespace copying.

Verified behavior (P0-002b):

- ``BaseCheckpointSaver.put`` copies one checkpoint into a new thread/namespace
  and assigns a fresh ``checkpoint_id``;
- pending writes are stored separately from the checkpoint and are NOT copied
  by ``put`` — take-over must copy them explicitly or they are lost;
- resuming a copied namespace with new input continues the copied conversation
  history instead of starting empty.

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
    checkpoint = source_tuple.checkpoint
    new_versions = checkpoint.get("channel_versions", {}) if isinstance(checkpoint, dict) else {}
    target_config: RunnableConfig = {
        "configurable": {
            "thread_id": str(target_thread_id),
            "checkpoint_ns": str(target_checkpoint_ns or ""),
        }
    }
    saved = saver.put(target_config, checkpoint, source_tuple.metadata, new_versions)
    if copy_pending_writes:
        grouped: dict[str, list[tuple[str, Any]]] = {}
        order: list[str] = []
        for write in source_tuple.pending_writes or []:
            task_id, channel, value = pending_write_parts(write)
            if task_id not in grouped:
                order.append(task_id)
                grouped[task_id] = []
            grouped[task_id].append((channel, value))
        for task_id in order:
            saver.put_writes(saved, grouped[task_id], task_id=task_id)
    return saved


def copy_latest_checkpoint(
    saver: Any,
    source_thread_id: str,
    target_thread_id: str,
    *,
    copy_pending_writes: bool = True,
) -> RunnableConfig | None:
    """Copy the latest root-namespace checkpoint of a conversation thread.

    Only the single checkpoint needed to continue the conversation is copied;
    older revisions and non-root namespaces are not cloned into the target
    attempt.  History/time-travel inside a superseded attempt namespace stays
    isolated and is cleaned up by the retention task.
    """
    source_config: RunnableConfig = {"configurable": {"thread_id": str(source_thread_id), "checkpoint_ns": ""}}
    return copy_checkpoint_namespace(
        saver,
        source_config,
        target_thread_id,
        target_checkpoint_ns="",
        copy_pending_writes=copy_pending_writes,
    )
