"""LangMem adapter used exclusively by the background reflection worker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from langmem import create_memory_store_manager

from src.core.chat_model_runtime import ChatModelLike, instrument_chat_model
from src.core.model_factory import create_chat_model, create_chat_model_for_settings
from src.memory.catalog import MemoryCatalogRepository, memory_content_hash
from src.memory.prompts import PROJECT_MEMORY_INSTRUCTIONS, USER_MEMORY_INSTRUCTIONS
from src.memory.schemas import ProjectMemory, UserMemory
from src.memory.store import CapturedDelete, CatalogAwareStore, MemoryStoreRuntime


class MemoryExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractedMemory:
    semantic: dict[str, Any]
    output_key: str | None
    output_item_hash: str


@dataclass(frozen=True)
class MemoryExtractionOutput:
    items: tuple[ExtractedMemory, ...]
    delete_keys: tuple[str, ...] = ()


def _memory_model(settings):
    """Build the reflection model through the central profile factory."""

    import src.settings as application_settings

    try:
        if settings is application_settings:
            return create_chat_model(profile="memory")
        # Isolated workers/tests may provide a settings object. Keep that
        # injection point inside the factory instead of rebuilding providers
        # in the memory domain module.
        return create_chat_model_for_settings(settings, profile="memory")
    except Exception as exc:
        raise MemoryExtractionError(str(exc)) from exc


def _message_for_manager(message: dict[str, Any]) -> dict[str, str]:
    """Attach server-owned provenance hints without asking the model to own IDs."""

    role = str(message.get("role") or "user")
    content = str(message.get("content") or "")
    message_id = message.get("message_id", message.get("id", ""))
    source = f"[source turn_id={message.get('turn_id', '')} message_id={message_id} role={role}]"
    return {"role": role, "content": f"{source}\n{content}"}


class LangMemAdapter:
    """Create one scoped Candidate manager and normalize its staging output."""

    def __init__(
        self,
        runtime: MemoryStoreRuntime,
        catalog: MemoryCatalogRepository,
        *,
        settings=None,
        manager_factory: Callable[..., Any] | None = None,
        model: ChatModelLike | None = None,
        model_factory: Callable[[], ChatModelLike] | None = None,
    ):
        self.runtime = runtime
        self.catalog = catalog
        if settings is None:
            import src.settings as settings_module

            settings = settings_module
        self.settings = settings
        self.manager_factory = manager_factory or create_memory_store_manager
        self.model = model
        self.model_factory = model_factory

    def _get_model(self) -> ChatModelLike:
        if self.model is None:
            self.model = (
                self.model_factory()
                if self.model_factory is not None
                else _memory_model(self.settings)
            )
        return self.model

    def _manager(self, scope: tuple[str, ...], *, user: bool) -> tuple[Any, CatalogAwareStore]:
        if scope[-1] != "candidate":
            raise MemoryExtractionError("LangMem manager may only use Candidate namespace")
        try:
            model = instrument_chat_model(
                self._get_model(),
                operation="memory_reflection",
                profile="memory",
            )
        except Exception as exc:
            if isinstance(exc, MemoryExtractionError):
                raise
            raise MemoryExtractionError(str(exc)) from exc
        store = CatalogAwareStore(
            self.runtime,
            self.catalog,
            scope,
            max_scan=int(getattr(self.settings, "MEMORY_STORE_MAX_SCAN", 100)),
            oversample_factor=int(getattr(self.settings, "MEMORY_STORE_OVERSAMPLE_FACTOR", 4)),
        )
        manager = self.manager_factory(
            model,
            schemas=[UserMemory if user else ProjectMemory],
            instructions=USER_MEMORY_INSTRUCTIONS if user else PROJECT_MEMORY_INSTRUCTIONS,
            enable_inserts=True,
            enable_deletes=False,
            namespace=scope,
            store=store,
        )
        return manager, store

    @staticmethod
    def _validate_semantic(value: Any, *, user: bool) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        try:
            model = (UserMemory if user else ProjectMemory).model_validate(value)
        except Exception:
            return None
        return model.model_dump(mode="json")

    def extract(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        scope: tuple[str, ...],
        user: bool = False,
    ) -> MemoryExtractionOutput:
        manager, store = self._manager(scope, user=user)
        store.clear_capture()
        normalized_messages = [_message_for_manager(message) for message in messages]
        if not normalized_messages:
            return MemoryExtractionOutput(())
        try:
            puts = manager.invoke({"messages": normalized_messages})
        except Exception as exc:
            raise MemoryExtractionError(f"LangMem reflection failed: {str(exc)[:500]}") from exc
        if not isinstance(puts, list):
            puts = []
        extracted: list[ExtractedMemory] = []
        for put in puts:
            if not isinstance(put, dict):
                continue
            value = put.get("value") if isinstance(put.get("value"), dict) else put
            semantic = value.get("content") if isinstance(value, dict) else None
            semantic = self._validate_semantic(semantic, user=user)
            if semantic is None:
                continue
            extracted.append(
                ExtractedMemory(
                    semantic=semantic,
                    output_key=str(put.get("key")) if put.get("key") not in (None, "") else None,
                    output_item_hash=memory_content_hash(semantic),
                )
            )
        # A test double or a future LangMem release may not return final_puts
        # even though it called the Store.  Capture the writes as a compatible
        # fallback, preserving the same schema validation gate.
        if not extracted:
            for put in list(store.captured_puts):
                semantic = self._validate_semantic(put.value.get("content"), user=user)
                if semantic is None:
                    continue
                extracted.append(
                    ExtractedMemory(
                        semantic=semantic,
                        output_key=put.key,
                        output_item_hash=memory_content_hash(semantic),
                    )
                )
        # LangMem is configured with enable_deletes=False. Keep this list for
        # compatibility and explicit audit if that invariant changes later.
        delete_keys = tuple(delete.key for delete in store.captured_deletes if isinstance(delete, CapturedDelete))
        dedup: dict[tuple[str | None, str], ExtractedMemory] = {}
        for item in extracted:
            dedup[(item.output_key, item.output_item_hash)] = item
        return MemoryExtractionOutput(tuple(dedup.values()), delete_keys)


def output_hash(output: MemoryExtractionOutput) -> str:
    payload = [
        {"key": item.output_key, "hash": item.output_item_hash, "semantic": item.semantic}
        for item in output.items
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = [
    "ExtractedMemory",
    "LangMemAdapter",
    "MemoryExtractionError",
    "MemoryExtractionOutput",
    "output_hash",
]
