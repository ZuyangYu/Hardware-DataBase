"""Vector index over circuit structured data (Stage 2).

This sits in front of (and complements) Stage 1's keyword OR matcher. For
each ``CircuitDesign`` saved by ``CircuitStore``, we project its modules,
instances and nets into short natural-language docs and write them to a
Chroma collection. Queries then do a semantic similarity search and the
results are merged with the Stage 1 keyword hits.

Why a separate collection per KB and not the main RAG collection?
- Different lifecycle: circuit data is overwritten in-place every time the
  EDF/PDF is re-parsed; mixing it into the main RAG index would force
  re-ingest there too.
- Different schema: id is `<kind>:<design>:<refdes|name>` so we can fan-out
  back to the structured row on hit, instead of returning prose chunks.
- Different reranker: we want our LLM-rerank stage, not the RAG reranker.

Failure mode: if no embedding model is configured (Settings.embed_model is
None — e.g. RAGFlow mode without USE_OLLAMA_EMBEDDING) the index becomes a
no-op. Indexing skips silently; semantic_search returns an empty list. The
caller never has to special-case "is the vector index up?"
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from src.circuit.models import CircuitDesign
from src.circuit.parsers.edf_power import classify_net_name
from src.core.logger import error, log, warn


# Doc-kind discriminator. Persisted into metadata so we can fan back to the
# right structured-row shape after vector retrieval.
KIND_MODULE = "module"
KIND_INSTANCE = "instance"
KIND_NET = "net"


@dataclass(frozen=True)
class CircuitVectorHit:
    kind: str
    design_id: str
    natural_id: str            # module_id / refdes / net_name
    score: float               # 1 - distance, higher is more similar
    metadata: dict[str, Any]
    document: str


def _collection_name(kb_name: str) -> str:
    # Keep separate from the main `kb_<kb>` collection so deletion / reindex
    # cycles don't disturb the RAG index.
    return f"circuit_kb_{kb_name}"


def _module_doc(design: CircuitDesign, module) -> tuple[str, dict[str, Any]]:
    """Build a natural-language doc for a module.

    Inputs to the embedding model should read like a human description, not
    a structured record. We concatenate: name, role hints, instance roster,
    and the analyzer-generated connectivity description if present.
    """
    instance_summary = []
    by_refdes = {inst.refdes: inst for inst in design.instances}
    for refdes in module.instances[:30]:
        inst = by_refdes.get(refdes)
        if inst is None:
            instance_summary.append(refdes)
            continue
        parts = [inst.refdes]
        if inst.library_cell:
            parts.append(inst.library_cell)
        if inst.part_number:
            parts.append(inst.part_number)
        instance_summary.append(" ".join(parts))
    description = (
        module.merged_description
        or module.connectivity_description
        or module.visual_description
        or ""
    )
    net_by_name = {net.name: net for net in design.nets}
    power_nets: list[str] = []
    ground_nets: list[str] = []
    for net_name in module.nets:
        net = net_by_name.get(net_name)
        net_type = net.net_type if net else "signal"
        if net_type not in {"power", "ground"}:
            net_type = classify_net_name(net_name)
        if net_type == "power":
            power_nets.append(net_name)
        elif net_type == "ground":
            ground_nets.append(net_name)
    body_lines = [
        f"模块: {module.name}",
        f"模块ID: {module.module_id}",
        f"包含器件: {', '.join(instance_summary) or '无'}",
        f"涉及网络: {', '.join(module.nets[:20]) or '无'}",
        f"供电网络 Power nets: {', '.join(power_nets[:20]) or '无'}",
        f"地网络 Ground nets: {', '.join(ground_nets[:20]) or '无'}",
    ]
    if description:
        body_lines.append(f"描述: {description}")
    body = "\n".join(body_lines)
    metadata = {
        "kind": KIND_MODULE,
        "design_id": design.design_id,
        "kb_name": design.kb_name,
        "natural_id": module.module_id,
        "module_name": module.name,
        "instance_count": len(module.instances),
        "net_count": len(module.nets),
    }
    return body, metadata


def _instance_doc(design: CircuitDesign, inst) -> tuple[str, dict[str, Any]]:
    parts = [
        f"器件位号: {inst.refdes}",
        f"Library Cell: {inst.library_cell or '-'}",
        f"零件号: {inst.part_number or '-'}",
        f"封装: {inst.footprint or '-'}",
    ]
    if inst.value:
        parts.append(f"参数: {inst.value}")
    body = "\n".join(parts)
    metadata = {
        "kind": KIND_INSTANCE,
        "design_id": design.design_id,
        "kb_name": design.kb_name,
        "natural_id": inst.refdes,
        "library_cell": inst.library_cell or "",
        "part_number": inst.part_number or "",
    }
    return body, metadata


def _net_doc(design: CircuitDesign, net) -> tuple[str, dict[str, Any]]:
    body = "\n".join(
        [
            f"网络: {net.name}",
            f"类型: {net.net_type}",
            f"连接数: {len(net.connections)}",
            f"连接示例: {', '.join(f'{conn.refdes}.{conn.pin}' for conn in net.connections[:10]) or '无'}",
        ]
    )
    metadata = {
        "kind": KIND_NET,
        "design_id": design.design_id,
        "kb_name": design.kb_name,
        "natural_id": net.name,
        "net_type": net.net_type,
        "connection_count": len(net.connections),
    }
    return body, metadata


class CircuitVectorIndex:
    """Per-KB embedding index over circuit structured data.

    Construction is cheap (lazy chromadb collection). All real cost happens
    inside `reindex_design` (one embedding call per row) and `semantic_search`
    (one embedding call for the query).
    """

    # Singleton-ish: callers normally just import `default_circuit_vector_index`.
    _lock = threading.RLock()

    def __init__(self):
        # Log "no embed model / no chroma" exactly once per process so test
        # runs and RAGFlow-mode deploys don't spam the audit log.
        self._embed_warning_logged = False
        self._chroma_warning_logged = False
        self._client_cache = None
        self._client_resolved = False

    # ── plumbing ──────────────────────────────────────────────────────────

    def _embed_model(self):
        """Resolve the user-configured embedding model.

        Mirror `_resolve_llm`: read the private slot to avoid llama_index's
        OpenAI fallback if nothing has been bound.
        """
        try:
            from llama_index.core import Settings
        except Exception:
            return None
        return getattr(Settings, "_embed_model", None)

    def _chroma_client(self):
        """Resolve a Chroma client, or None when the vector stack is absent.

        The local chroma stack was removed from the runtime dependencies
        (retrieval is RAGFlow-only now), so in a default deployment this
        returns None and the whole index degrades to an explicit no-op —
        instead of raising ModuleNotFoundError from deep inside a query.
        Precedence: legacy ``src.core.resource_manager`` singleton if someone
        restores it, else a persistent client under storage/ when chromadb
        happens to be installed.
        """
        if self._client_resolved:
            return self._client_cache
        self._client_resolved = True
        client = None
        try:
            from src.core.resource_manager import resource_manager  # type: ignore

            client = getattr(resource_manager, "chroma_client", None)
        except ImportError:
            try:
                import chromadb

                import config.settings as _settings

                path = os.path.join(_settings.STORAGE_DIR, "circuit_vector_index")
                os.makedirs(path, exist_ok=True)
                client = chromadb.PersistentClient(path=path)
            except ImportError:
                pass
            except Exception as exc:
                warn(f"CircuitVectorIndex: chroma client init failed: {exc}")
        except Exception as exc:
            warn(f"CircuitVectorIndex: resource_manager unavailable: {exc}")
        if client is None and not self._chroma_warning_logged:
            log("CircuitVectorIndex: 本地向量栈未安装（chromadb），电路语义索引保持停用")
            self._chroma_warning_logged = True
        self._client_cache = client
        return client

    def _chroma_collection(self, kb_name: str):
        client = self._chroma_client()
        if client is None:
            raise RuntimeError("chroma vector stack is not available")
        # Cosine space so distances are cosine distances in [0,2] and
        # `score = max(0, 1 - distance)` is a real similarity in [0,1]
        # comparable with keyword-match scores downstream.
        return client.get_or_create_collection(
            _collection_name(kb_name),
            metadata={"hnsw:space": "cosine"},
        )

    def is_available(self) -> bool:
        """True only when BOTH an embedding model and a chroma client are
        available. Otherwise the index is an explicit no-op and callers skip
        it entirely."""
        return self._embed_model() is not None and self._chroma_client() is not None

    # ── write path ────────────────────────────────────────────────────────

    def reindex_design(self, design: CircuitDesign) -> int:
        """Re-embed a single design. Returns the row count written.

        Strategy: nuke this design's existing entries first, then bulk-write
        the new ones. The collection is per-KB but we scope deletion by
        ``design_id`` so concurrent designs in the same KB don't trample
        each other.
        """
        embed_model = self._embed_model()
        if embed_model is None:
            if not self._embed_warning_logged:
                log("CircuitVectorIndex: embed model 未配置，跳过 circuit 向量索引")
                self._embed_warning_logged = True
            return 0
        if self._chroma_client() is None:
            return 0

        docs: list[tuple[str, str, dict[str, Any]]] = []
        # Collect (id, body, metadata) triples for everything we want indexed.
        for module in design.modules:
            body, meta = _module_doc(design, module)
            doc_id = f"{KIND_MODULE}:{design.design_id}:{module.module_id}"
            docs.append((doc_id, body, meta))
        for inst in design.instances:
            body, meta = _instance_doc(design, inst)
            doc_id = f"{KIND_INSTANCE}:{design.design_id}:{inst.refdes}"
            docs.append((doc_id, body, meta))
        for net in design.nets:
            body, meta = _net_doc(design, net)
            doc_id = f"{KIND_NET}:{design.design_id}:{net.name}"
            docs.append((doc_id, body, meta))

        if not docs:
            # No content to index → still remove stale rows for this design.
            self._delete_design(design.kb_name, design.design_id)
            return 0

        with self._lock:
            self._delete_design(design.kb_name, design.design_id)
            collection = self._chroma_collection(design.kb_name)
            try:
                ids = [d[0] for d in docs]
                bodies = [d[1] for d in docs]
                metas = [d[2] for d in docs]
                embeddings = self._embed_batch(embed_model, bodies)
                if embeddings is None:
                    return 0
                collection.add(
                    ids=ids,
                    documents=bodies,
                    embeddings=embeddings,
                    metadatas=metas,
                )
                log(
                    f"CircuitVectorIndex: reindexed {design.kb_name}/{design.design_id} — "
                    f"{len(docs)} docs"
                )
                return len(docs)
            except Exception as exc:
                error(
                    f"CircuitVectorIndex: reindex_design failed for "
                    f"{design.kb_name}/{design.design_id}: {exc}"
                )
                return 0

    def _delete_design(self, kb_name: str, design_id: str) -> None:
        """Remove all rows for a design. Safe when the collection is empty,
        doesn't exist yet, or the vector stack is unavailable."""
        try:
            collection = self._chroma_collection(kb_name)
            collection.delete(where={"design_id": design_id})
        except Exception as exc:
            warn(f"CircuitVectorIndex: delete_design ({kb_name}/{design_id}) failed: {exc}")

    def drop_kb(self, kb_name: str) -> None:
        """Drop the entire circuit vector collection for a KB."""
        client = self._chroma_client()
        if client is None:
            return
        try:
            client.delete_collection(name=_collection_name(kb_name))
        except Exception:
            # Either the collection didn't exist (fine) or chroma raised
            # something exotic — log and move on; callers expect best-effort.
            pass

    # ── read path ─────────────────────────────────────────────────────────

    def semantic_search(
        self,
        kb_name: str,
        query: str,
        top_k: int = 20,
        kinds: Iterable[str] | None = None,
    ) -> list[CircuitVectorHit]:
        """Top-K nearest docs. Returns [] if no embed model, no collection,
        or chroma errors out — callers must treat this as a best-effort
        signal that complements keyword matching.
        """
        if not query.strip():
            return []
        embed_model = self._embed_model()
        if embed_model is None or self._chroma_client() is None:
            return []
        try:
            query_embedding = self._embed_batch(embed_model, [query])
            if not query_embedding:
                return []
            collection = self._chroma_collection(kb_name)
            where: dict[str, Any] | None = None
            if kinds:
                where = {"kind": {"$in": list(kinds)}}
            res = collection.query(
                query_embeddings=query_embedding,
                n_results=top_k,
                where=where,
            )
        except Exception as exc:
            warn(f"CircuitVectorIndex: semantic_search failed: {exc}")
            return []

        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]

        hits: list[CircuitVectorHit] = []
        for i, doc_id in enumerate(ids):
            meta = metas[i] if i < len(metas) and metas[i] else {}
            # Chroma's distance is cosine distance in [0,2]; flip into a
            # similarity score so the rest of the pipeline can sort
            # descending like every other retriever.
            distance = float(dists[i]) if i < len(dists) and dists[i] is not None else 0.0
            score = max(0.0, 1.0 - distance)
            hits.append(
                CircuitVectorHit(
                    kind=str(meta.get("kind") or ""),
                    design_id=str(meta.get("design_id") or ""),
                    natural_id=str(meta.get("natural_id") or doc_id),
                    score=score,
                    metadata=dict(meta),
                    document=docs[i] if i < len(docs) and docs[i] else "",
                )
            )
        return hits

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _embed_batch(embed_model, texts: list[str]) -> list[list[float]] | None:
        """Embed a batch. Handles llama_index's `get_text_embedding_batch`
        when present, otherwise falls back to per-row `get_text_embedding`.
        """
        try:
            batch_fn = getattr(embed_model, "get_text_embedding_batch", None)
            if callable(batch_fn):
                return list(batch_fn(texts, show_progress=False))
            single_fn = getattr(embed_model, "get_text_embedding", None)
            if not callable(single_fn):
                warn("CircuitVectorIndex: embed model lacks get_text_embedding{,_batch}")
                return None
            return [single_fn(text) for text in texts]
        except Exception as exc:
            error(f"CircuitVectorIndex: embedding call failed: {exc}")
            return None


# Process-wide handle. Construction is cheap (no I/O) so a singleton is fine.
default_circuit_vector_index = CircuitVectorIndex()
