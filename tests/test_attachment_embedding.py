"""Dedicated attachment embedding gateway contract tests."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import src.settings
from src.attachments.embedding import (
    OpenAICompatibleEmbeddingGateway,
    default_embedding_gateway,
)


class AttachmentEmbeddingGatewayTests(unittest.TestCase):
    def setUp(self):
        self._old = {
            name: getattr(src.settings, name, None)
            for name in (
                "CHAT_ATTACHMENT_EMBEDDING_BASE_URL",
                "CHAT_ATTACHMENT_EMBEDDING_API_KEY",
                "CHAT_ATTACHMENT_EMBEDDING_MODEL",
                "MEMORY_EMBEDDING_API_KEY",
            )
        }

    def tearDown(self):
        for name, value in self._old.items():
            setattr(src.settings, name, value)

    def test_unconfigured_gateway_does_not_use_memory_key_or_make_network_call(self):
        src.settings.CHAT_ATTACHMENT_EMBEDDING_BASE_URL = ""
        src.settings.CHAT_ATTACHMENT_EMBEDDING_API_KEY = ""
        src.settings.CHAT_ATTACHMENT_EMBEDDING_MODEL = ""
        src.settings.MEMORY_EMBEDDING_API_KEY = "memory-secret"

        with patch("src.attachments.embedding.httpx.post") as post:
            self.assertIsNone(default_embedding_gateway())

        post.assert_not_called()

    def test_openai_compatible_gateway_orders_shuffled_response_indexes(self):
        response = Mock()
        response.json.return_value = {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        }
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.example/v1",
            api_key="attachment-secret",
            model="attachment-model",
        )

        with patch("src.attachments.embedding.httpx.post", return_value=response) as post:
            vectors = gateway.embed(["first", "second"])

        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer attachment-secret")

    def test_openai_compatible_gateway_rejects_duplicate_response_indexes(self):
        response = Mock()
        response.json.return_value = {
            "data": [
                {"index": 0, "embedding": [1.0, 0.0]},
                {"index": 0, "embedding": [0.0, 1.0]},
            ]
        }
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.example/v1",
            api_key="attachment-secret",
            model="attachment-model",
        )

        with patch("src.attachments.embedding.httpx.post", return_value=response):
            with self.assertRaisesRegex(ValueError, "index"):
                gateway.embed(["first", "second"])


if __name__ == "__main__":
    unittest.main()
