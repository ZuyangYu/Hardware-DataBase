import unittest
from unittest.mock import patch

from src.core.llm_client import LLMClient, LLMClientConfig, _iter_sse_data_events
import config.settings as settings


class _FakeStreamResponse:
    def __init__(self, lines, encoding=None):
        self.lines = lines
        self.encoding = encoding

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        for line in self.lines:
            if isinstance(line, bytes) and decode_unicode:
                yield line.decode(self.encoding or "utf-8")
            else:
                yield line


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

    def test_openai_compatible_stream_overrides_text_event_stream_latin1_encoding(self):
        config = LLMClientConfig(
            provider=settings.Provider.CUSTOM,
            base_url="https://example.test/v1",
            model="fake-model",
        )
        response = _FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"content":"你好"}}]}'.encode("utf-8"),
                b"",
                b"data: [DONE]",
            ],
            encoding="ISO-8859-1",
        )

        with patch("src.core.llm_client.requests.post", return_value=response):
            chunks = list(LLMClient(config).stream_chat([{"role": "user", "content": "hi"}]))

        self.assertEqual(response.encoding, "utf-8")
        self.assertEqual(chunks, ["你好"])


if __name__ == "__main__":
    unittest.main()
