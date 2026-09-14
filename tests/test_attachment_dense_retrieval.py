"""Optional dense/hybrid retrieval tests for chat attachments."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

import src.settings
from src.attachments.index import AttachmentIndex
from src.attachments.models import AttachmentPart
from src.attachments.retrieval import AttachmentRetrievalService
from src.attachments.store import AttachmentStore
from src.attachments.dense import DenseAttachmentRetriever


class FakeEmbeddingGateway:
    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors
        self.calls: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        return [self.vectors[text] for text in texts]


class DenseAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch("CHAT_ATTACHMENT_INDEX_DB_PATH", os.path.join(self.tmp.name, "att.db"))
        self._patch("CHAT_ATTACHMENT_STORAGE_DIR", os.path.join(self.tmp.name, "storage"))
        self._patch("CHAT_ATTACHMENT_DENSE_ENABLED", True)
        self._patch("CHAT_ATTACHMENT_RETRIEVAL_MODE", "hybrid")
        self.store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
        self.asset, _ = self.store.create_asset(
            session_id=1,
            user_id=1,
            sha256=hashlib.sha256(b"asset").hexdigest(),
            media_type="text/plain",
            extension=".txt",
            size_bytes=5,
            storage_key="sources/1/asset/source.txt",
        )
        self.parts = [
            AttachmentPart(
                part_id="part-power",
                asset_id=self.asset.asset_id,
                ordinal=0,
                part_type="text",
                text_content="power regulator",
            ),
            AttachmentPart(
                part_id="part-mechanical",
                asset_id=self.asset.asset_id,
                ordinal=1,
                part_type="text",
                text_content="mechanical enclosure",
            ),
        ]
        self.store.replace_parts(self.asset.asset_id, self.parts)

    def _patch(self, name, value):
        old = getattr(src.settings, name, None)
        setattr(src.settings, name, value)
        self.addCleanup(setattr, src.settings, name, old)

    def test_dense_disabled_does_not_call_embedding_gateway(self):
        gateway = FakeEmbeddingGateway({"query": [1.0, 0.0]})
        self._patch("CHAT_ATTACHMENT_DENSE_ENABLED", False)

        result = DenseAttachmentRetriever(store=self.store, gateway=gateway).search(
            "query", asset_ids=[self.asset.asset_id], limit=2
        )

        self.assertEqual(result, [])
        self.assertEqual(gateway.calls, [])

    def test_dense_retrieval_ranks_authorized_parts_by_cosine_similarity(self):
        gateway = FakeEmbeddingGateway(
            {
                "query": [1.0, 0.0],
                "power regulator": [1.0, 0.0],
                "mechanical enclosure": [0.0, 1.0],
            }
        )

        with patch("src.observability.metrics.record_attachment") as metric:
            hits = DenseAttachmentRetriever(store=self.store, gateway=gateway).search(
                "query", asset_ids=[self.asset.asset_id], limit=2
            )

        self.assertEqual([hit.part_id for hit in hits], ["part-power", "part-mechanical"])
        self.assertEqual(hits[0].backend, "dense")
        self.assertGreater(hits[0].score, hits[1].score)
        self.assertTrue(any(call.args[0] == "dense" for call in metric.call_args_list))

    def test_hybrid_retrieval_fuses_dense_hits_with_lexical_hits(self):
        gateway = FakeEmbeddingGateway(
            {
                "unseen query": [1.0, 0.0],
                "power regulator": [1.0, 0.0],
                "mechanical enclosure": [0.0, 1.0],
            }
        )
        dense = DenseAttachmentRetriever(store=self.store, gateway=gateway)
        index = AttachmentIndex(db_path=self.store.db_path)
        index.replace_asset(self.asset.asset_id, self.parts)

        result = AttachmentRetrievalService(
            index=index,
            dense_retriever=dense,
        ).search(
            "unseen query", asset_ids=[self.asset.asset_id], limit=2
        )

        self.assertTrue(result.chunks)
        self.assertIn("dense", result.matched_backends)
        self.assertEqual(result.chunks[0].part_id, "part-power")


if __name__ == "__main__":
    unittest.main()
