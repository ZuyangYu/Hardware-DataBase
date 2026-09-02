"""Fenced LangGraph checkpointers for authoring runs.

LangGraph owns graph state and interrupts; the AuthoringStore remains the
authority for leases and business facts.  This wrapper adds the fencing token
to every checkpoint and rejects stale workers before delegating to the
official in-memory or SQLite saver.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Send

from src.document_authoring.harness.langgraph_state import normalize_graph_state


STALE_FENCING_ERROR = "stale_fencing_token"
CHECKPOINTER_BACKEND_SQLITE = "sqlite"
CHECKPOINTER_BACKEND_MEMORY = "memory"


class StaleFencingToken(RuntimeError):
    """Raised when an old worker tries to write or resume a checkpoint."""

    error_code = STALE_FENCING_ERROR

    def __init__(self, message: str = "checkpoint fencing token is stale") -> None:
        super().__init__(f"{self.error_code}: {message}")


FencingTokenProvider = Callable[[str], int | None]


def _message_content_summary(value: Any) -> dict[str, Any]:
    """Describe message content without persisting prompt or evidence text."""
    if isinstance(value, str):
        return {"kind": "text", "length": len(value)}
    if isinstance(value, bytes):
        return {"kind": "bytes", "length": len(value)}
    if isinstance(value, list):
        return {"kind": "list", "items": len(value)}
    if isinstance(value, dict):
        return {"kind": "object", "keys": sorted(str(key) for key in value)[:64]}
    if value is None:
        return {"kind": "null", "length": 0}
    return {"kind": type(value).__name__}


def _message_channel_projection(value: Any, *, depth: int = 0) -> Any:
    """Make a bounded JSON-safe validation view for LangChain messages.

    ``FencedCheckpointer`` delegates actual message serialization to
    LangGraph's official saver.  This projection is only a size/type gate, so
    it deliberately keeps content metadata rather than content itself.
    Unknown objects in ordinary authoring channels remain rejected by
    ``normalize_graph_state``; only values nested under the message channel
    receive this adapter treatment.
    """
    if depth > 8:
        return {"kind": "nested_value", "depth": depth}
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"kind": "bytes", "length": len(value)}
    if isinstance(value, (list, tuple)):
        return [
            _message_channel_projection(item, depth=depth + 1)
            for item in list(value)[:256]
        ]
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in list(value.items())[:256]:
            normalized_key = str(key)
            if normalized_key in {"content", "text"}:
                projected[normalized_key] = _message_content_summary(item)
            else:
                projected[normalized_key] = _message_channel_projection(
                    item, depth=depth + 1,
                )
        return projected

    # LangChain BaseMessage and message-like objects expose these attributes.
    # Avoid a hard dependency on a particular LangChain message class so the
    # checkpointer remains usable with the supported LangGraph versions.
    message_type = getattr(value, "type", None)
    if message_type is not None and hasattr(value, "content"):
        tool_calls = getattr(value, "tool_calls", ()) or ()
        return {
            "message_type": str(message_type),
            "message_id": str(getattr(value, "id", "") or "")[:200],
            "name": str(getattr(value, "name", "") or "")[:200],
            "content": _message_content_summary(getattr(value, "content", None)),
            "tool_call_count": len(tool_calls) if isinstance(tool_calls, (list, tuple)) else 0,
        }
    return {"kind": "message_value", "type": type(value).__name__}


def _contains_scheduler_send(value: Any) -> bool:
    """Identify LangGraph's ephemeral conditional-edge instructions."""
    if isinstance(value, Send):
        return True
    if isinstance(value, (list, tuple, set)):
        return any(_contains_scheduler_send(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_scheduler_send(item) for item in value.values())
    return False


class FencedCheckpointer(BaseCheckpointSaver):
    """Compose an official LangGraph saver with state and fencing checks."""

    def __init__(
        self,
        delegate: BaseCheckpointSaver,
        *,
        fencing_token_provider: FencingTokenProvider | None = None,
        fencing_token: int | None = None,
    ) -> None:
        super().__init__(serde=getattr(delegate, "serde", None))
        self.delegate = delegate
        self.fencing_token_provider = fencing_token_provider
        self._fixed_fencing_token = fencing_token
        self._latest_tokens: dict[str, int] = {}
        self._lock = threading.RLock()

    @property
    def config_specs(self):
        return getattr(self.delegate, "config_specs", [])

    @staticmethod
    def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
        return dict((config or {}).get("configurable") or {})

    @classmethod
    def _thread_id(cls, config: RunnableConfig | None) -> str:
        thread_id = cls._configurable(config).get("thread_id")
        if not thread_id:
            raise ValueError("LangGraph authoring checkpoints require configurable.thread_id")
        return str(thread_id)

    def _requested_token(
        self,
        config: RunnableConfig | None,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        configurable = self._configurable(config)
        value = configurable.get("fencing_token")
        if value is None:
            value = self._fixed_fencing_token
        if value is None and metadata is not None:
            value = metadata.get("fencing_token")
        return int(value) if value is not None else None

    def _current_token(self, thread_id: str) -> int | None:
        if self.fencing_token_provider is not None:
            value = self.fencing_token_provider(thread_id)
            if value is not None:
                return int(value)
        return self._fixed_fencing_token

    def _assert_write_fence(
        self,
        config: RunnableConfig,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, int | None, dict[str, Any]]:
        thread_id = self._thread_id(config)
        token = self._requested_token(config, metadata)
        current = self._current_token(thread_id)
        with self._lock:
            latest = self._latest_tokens.get(thread_id)
        expected = current if current is not None else latest
        if expected is not None and token != expected:
            raise StaleFencingToken(
                f"thread={thread_id} supplied={token!r} expected={expected!r}"
            )
        if token is not None and latest is not None and token < latest:
            raise StaleFencingToken(
                f"thread={thread_id} supplied={token!r} latest={latest!r}"
            )
        revised = dict(metadata or {})
        if token is not None:
            revised["fencing_token"] = token
            with self._lock:
                self._latest_tokens[thread_id] = max(token, self._latest_tokens.get(thread_id, token))
        return thread_id, token, revised

    @staticmethod
    def _checkpoint_state(checkpoint: Checkpoint) -> dict[str, Any]:
        channels = checkpoint.get("channel_values") or {}
        if not isinstance(channels, dict):
            raise ValueError("graph_state must be a channel-value object")
        # LangGraph's message channel contains ``BaseMessage`` instances.  The
        # document-authoring graph itself is ID-only, but the deep-agent
        # adapter legitimately has a message channel that the official saver
        # serializes.  Validate a redacted projection of that one channel;
        # never feed raw messages through the business-state serializer.
        # Conditional ``Send`` edges are represented by internal branch
        # channels (for example ``branch:to:retrieve_evidence``). They are
        # scheduler instructions, not business state, and the official saver
        # owns their serialization. Do not mistake them for arbitrary domain
        # objects while validating the authoring channels below.
        validation_channels = {
            key: (
                _message_channel_projection(value)
                if key in {"messages", "message"}
                else value
            )
            for key, value in channels.items()
            if not str(key).startswith("branch:")
            and not _contains_scheduler_send(value)
        }
        normalize_graph_state(validation_channels)
        return channels

    def _is_stale(self, item: CheckpointTuple | None) -> bool:
        if item is None:
            return False
        thread_id = self._thread_id(item.config)
        current = self._current_token(thread_id)
        if current is None:
            return False
        stored = (item.metadata or {}).get("fencing_token")
        return stored is not None and int(stored) != current

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with self._lock:
            item = self.delegate.get_tuple(config)
            if not self._is_stale(item):
                return item
            # A stale head can remain in a backend after a lease race.  Resume
            # the newest checkpoint carrying the current token when possible.
            if self._configurable(config).get("checkpoint_id"):
                return None
            for candidate in self.delegate.list(config):
                if not self._is_stale(candidate):
                    return candidate
            return None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        count = 0
        for item in self.delegate.list(config, filter=filter, before=before, limit=limit):
            if self._is_stale(item):
                continue
            yield item
            count += 1
            if limit is not None and count >= limit:
                return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._checkpoint_state(checkpoint)
        _, token, revised_metadata = self._assert_write_fence(config, dict(metadata))
        with self._lock:
            result = self.delegate.put(config, checkpoint, revised_metadata, new_versions)
        # LangGraph uses the returned config as the parent for the next write;
        # preserve the worker's token in that config across checkpoint IDs.
        if token is not None:
            configurable = dict((result or {}).get("configurable") or {})
            configurable["fencing_token"] = token
            result = {**(result or {}), "configurable": configurable}
        return result

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._assert_write_fence(config)
        with self._lock:
            self.delegate.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            self.delegate.delete_thread(thread_id)
            self._latest_tokens.pop(thread_id, None)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        item = await self.delegate.aget_tuple(config)
        if self._is_stale(item):
            return None
        return item

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        count = 0
        async for item in self.delegate.alist(config, filter=filter, before=before, limit=limit):
            if self._is_stale(item):
                continue
            yield item
            count += 1
            if limit is not None and count >= limit:
                return

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._checkpoint_state(checkpoint)
        _, token, revised_metadata = self._assert_write_fence(config, dict(metadata))
        result = await self.delegate.aput(config, checkpoint, revised_metadata, new_versions)
        if token is not None:
            configurable = dict((result or {}).get("configurable") or {})
            configurable["fencing_token"] = token
            result = {**(result or {}), "configurable": configurable}
        return result

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._assert_write_fence(config)
        await self.delegate.aput_writes(config, writes, task_id, task_path)

    def close(self) -> None:
        close = getattr(self.delegate, "conn", None)
        if close is not None and hasattr(close, "close"):
            close.close()


AuthoringCheckpointer = FencedCheckpointer


def create_sqlite_checkpointer(
    path: str | Path | None = None,
    *,
    connection: sqlite3.Connection | None = None,
    fencing_token_provider: FencingTokenProvider | None = None,
) -> FencedCheckpointer:
    """Create the single-process persistent checkpointer."""
    conn = connection
    if conn is None:
        if path is None:
            raise ValueError("sqlite checkpointer requires a path or connection")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    delegate = SqliteSaver(conn)
    delegate.setup()
    return FencedCheckpointer(delegate, fencing_token_provider=fencing_token_provider)


def create_memory_checkpointer(
    *,
    fencing_token_provider: FencingTokenProvider | None = None,
) -> FencedCheckpointer:
    """Create the test-only in-memory saver."""
    return FencedCheckpointer(InMemorySaver(), fencing_token_provider=fencing_token_provider)


def build_checkpointer(
    backend: str = CHECKPOINTER_BACKEND_MEMORY,
    *,
    sqlite_path: str | Path | None = None,
    connection: sqlite3.Connection | None = None,
    fencing_token_provider: FencingTokenProvider | None = None,
) -> FencedCheckpointer:
    normalized = str(backend).strip().casefold()
    if normalized in {CHECKPOINTER_BACKEND_MEMORY, "in_memory", "test"}:
        return create_memory_checkpointer(fencing_token_provider=fencing_token_provider)
    if normalized == CHECKPOINTER_BACKEND_SQLITE:
        return create_sqlite_checkpointer(
            sqlite_path,
            connection=connection,
            fencing_token_provider=fencing_token_provider,
        )
    raise ValueError(
        "unsupported authoring checkpointer backend; use 'memory' for tests or 'sqlite' for single-process"
    )


__all__ = [
    "AuthoringCheckpointer", "CHECKPOINTER_BACKEND_MEMORY", "CHECKPOINTER_BACKEND_SQLITE",
    "FencedCheckpointer", "InMemorySaver", "SqliteSaver", "STALE_FENCING_ERROR",
    "StaleFencingToken", "build_checkpointer", "create_memory_checkpointer",
    "create_sqlite_checkpointer",
]
