import unittest
from unittest.mock import patch

from src.core.llm_client import LLMClient, LLMClientConfig, _iter_sse_data_events
import config.settings as settings


class _FakeStreamResponse:
    def __init__(self, lines):
        self.lines = lines

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        yield from self.lines


class LLMClientStreamTests(unittest.TestCase):
    def test_sse_parser_buffers_json_split_inside_chinese_string(self):
        events = list(
            _iter_sse_data_events(
                [
                    'data: {"choices":[{"delta":{"content":"这是一段',
                    '很长中文"}}]}',
                    "",
                    "data: [DONE]",
                ]
            )
        )

        self.assertEqual(
            events,
            [
                '{"choices":[{"delta":{"content":"这是一段很长中文"}}]}',
                "[DONE]",
            ],
        )

    def test_openai_compatible_stream_accepts_split_sse_json(self):
        config = LLMClientConfig(
            provider=settings.Provider.CUSTOM,
            base_url="https://example.test/v1",
            model="fake-model",
            api_key="token",
        )
        response = _FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"content":"第一段',
                '中文"}}]}',
                "",
                'data: {"choices":[{"delta":{"content":"，第二段"}}]}',
                "",
                "data: [DONE]",
            ]
        )

        with patch("src.core.llm_client.requests.post", return_value=response):
            chunks = list(LLMClient(config).stream_chat([{"role": "user", "content": "hi"}]))

        self.assertEqual(chunks, ["第一段中文", "，第二段"])


if __name__ == "__main__":
    unittest.main()
