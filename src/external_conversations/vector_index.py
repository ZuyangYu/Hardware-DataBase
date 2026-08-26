"""Per-KB embedding supplement over external conversation messages.

Mirrors ``src/circuit/vector_index.py``: construction is cheap, everything is
fail-open, and the index is a **no-op unless** (a) an embedding model is bound
on llama_index ``Settings`` and (b) ``chromadb`` is importable. In a pure
RAGFlow deployment both are absent, so this class costs nothing at runtime;
it activates automatically once either dependency appears.

Collection layout: one collection per KB named ``external_conv_kb_<kb>``.
Documents are individual turns/blocks; every metadata dict carries
``department_id`` so queries stay department-scoped even when same-named KBs
share a collection.
"""

from __future__ import annotations

import threading
from typing import Any

from src.core.logger import log
from src.external_conversations.models import ExternalConversation

_DOC_VERSION = "v1"


def _collection_name(kb_name: str) -> str:
    return f"external_conv_kb_{kb_name}"


def _doc_id(conversation_id: str, kind: str, index: int, department_id: Any) -> str:
    return f"{_DOC_VERSION}:{department_id}:{conversation_id}:{kind}{index}"


class ExternalConversationVectorIndex:
    def __init__(self, embed_fn=None, collection_factory=None):
        """Production use: ``ExternalConversationVectorIndex()`` — resolves the
        embed model from llama_index Settings and chromadb's PersistentClient
        lazily. Tests may inject ``embed_fn`` + ``collection_factory``."""
        self._embed_warning_logged = False
        self._inject_embed_fn = embed_fn
        self._inject_collection_factory = collection_factory
        self._lock = threading.RLock()

    # ── plumbing ──────────────────────────────────────────────────────────
    def _embed_model(self):
        if self._inject_embed_fn is not None:
            return self._inject_embed_fn
        try:
            from llama_index.core import Settings

            return getattr(Settings, "_embed_model", None)
        except Exception:
            return None

    def _collections(self):
        if self._inject_collection_factory is not None:
            return self._inject_collection_factory()
        try:
            import chromadb

            import config.settings

            client = chromadb.PersistentClient(path=str(config.settings.STORAGE_DIR) + "/chroma")
            return client
        except Exception:
            return None

    def is_available(self) -> bool:
        return self._embed_model() is not None and self._collections() is not None

    def _warn_once(self):
        if not self._embed_warning_logged:
            log("ExternalConversationVectorIndex: embed model 或 chromadb 未配置，语义补充为 no-op")
            self._embed_warning_logged = True

    # ── write path ────────────────────────────────────────────────────────
    def reindex_conversation(self, conversation: ExternalConversation) -> int:
        """Replace this conversation's vectors. Returns rows written; 0 on no-op.

        Fail-soft by contract: callers may ignore failures entirely."""
        embed_model = self._embed_model()
        client = self._collections()
        if embed_model is None or client is None:
            self._warn_once()
            return 0
        try:
            with self._lock:
                collection = client.get_or_create_collection(_collection_name(conversation.kb_name))
                try:
                    collection.delete(where={"$and": [
                        {"department_id": {"$eq": str(conversation.department_id)}},
                        {"conversation_id": {"$eq": conversation.conversation_id}},
                    ]})
                except Exception:
                    pass  # nothing indexed yet for this conversation
                docs: list[tuple[str, str, dict[str, Any]]] = []
                for i, turn in enumerate(conversation.turns):
                    docs.append((
                        _doc_id(conversation.conversation_id, "m", i, conversation.department_id),
                        turn.content,
                        {
                            "department_id": str(conversation.department_id),
                            "conversation_id": conversation.conversation_id,
                            "kind": "message",
                            "role": turn.role,
                            "turn_index": i,
                            "title": conversation.title,
                            "source_file": conversation.source_file,
                            "origin": conversation.origin,
                            "source_group": conversation.source_group,
                        },
                    ))
                for i, block in enumerate(conversation.blocks):
                    docs.append((
                        _doc_id(conversation.conversation_id, "b", i, conversation.department_id),
                        block,
                        {
                            "department_id": str(conversation.department_id),
                            "conversation_id": conversation.conversation_id,
                            "kind": "block",
                            "role": "document",
                            "turn_index": i,
                            "title": conversation.title,
                            "source_file": conversation.source_file,
                            "origin": conversation.origin,
                            "source_group": conversation.source_group,
                        },
                    ))
                if not docs:
                    return 0
                ids = [d[0] for d in docs]
                bodies = [d[1] for d in docs]
                embeddings = self._embed_batch(embed_model, bodies)
                collection.upsert(ids=ids, documents=bodies, metadatas=[d[2] for d in docs], embeddings=embeddings)
                return len(docs)
        except Exception as exc:
            log(f"ExternalConversationVectorIndex: reindex failed for {conversation.conversation_id}: {exc}")
            return 0

    def delete_conversation(self, kb_name: str, conversation_id: str, department_id: Any) -> bool:
        client = self._collections()
        if client is None:
            return False
        try:
            with self._lock:
                collection = client.get_or_create_collection(_collection_name(kb_name))
                collection.delete(where={"$and": [
                    {"department_id": {"$eq": str(department_id)}},
                    {"conversation_id": {"$eq": conversation_id}},
                ]})
            return True
        except Exception:
            return False

    # ── read path ─────────────────────────────────────────────────────────
    def semantic_search(self, kb_name: str, department_id: Any, query: str, top_k: int = 5) -> list[dict]:
        """Return rows shaped like query-engine keyword hits (plus a ``kind``)."""
        embed_model = self._embed_model()
        client = self._collections()
        if embed_model is None or client is None:
            return []
        try:
            with self._lock:
                collection = client.get_or_create_collection(_collection_name(kb_name))
                vector = self._embed_batch(embed_model, [query])[0]
                result = collection.query(
                    query_embeddings=[vector],
                    n_results=max(1, int(top_k)),
                    where={"department_id": {"$eq": str(department_id)}},
                )
        except Exception:
            return []

        rows: list[dict] = []
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        for doc_id, content, meta, distance in zip(ids, documents, metadatas, distances):
            meta = meta or {}
            rows.append({
                "message_id": -1,
                "conversation_id": meta.get("conversation_id", ""),
                "role": meta.get("role", ""),
                "turn_index": meta.get("turn_index", -1),
                "content": content or "",
                "ts": "",
                "start_offset": -1,
                "title": meta.get("title", ""),
                "source_file": meta.get("source_file", ""),
                "origin": meta.get("origin", ""),
                "source_group": meta.get("source_group", ""),
                "kind": meta.get("kind", ""),
                "vector_distance": float(distance) if distance is not None else 1.0,
            })
        return rows

    # ── embedding ─────────────────────────────────────────────────────────
    @staticmethod
    def _embed_batch(embed_model, texts: list[str]) -> list[list[float]]:
        if hasattr(embed_model, "get_text_embedding_batch"):
            return embed_model.get_text_embedding_batch(texts)
        return [list(map(float, embed_model.get_text_embedding(t))) for t in texts]


default_external_conversation_vector_index = ExternalConversationVectorIndex()
