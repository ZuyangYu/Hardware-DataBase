import unittest
from unittest.mock import patch

import requests

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


class _FakeJsonResponse:
    def __init__(self, payload):
        self.payload = payload
        self.encoding = None

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _RejectStreamOptionsResponse:
    status_code = 400
    text = "unknown field stream_options"
    encoding = None

    def raise_for_status(self):
        raise requests.HTTPError("400 Client Error")


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

    def test_openai_compatible_chat_records_usage(self):
        config = LLMClientConfig(
            provider=settings.Provider.CUSTOM,
            base_url="https://example.test/v1",
            model="fake-model",
        )
        response = _FakeJsonResponse(
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        )
        client = LLMClient(config)

        with patch("src.core.llm_client.requests.post", return_value=response):
            answer = client.chat([{"role": "user", "content": "hi"}], usage_stage="router")

        self.assertEqual(answer, "answer")
        summary = client.get_usage_summary()
        self.assertEqual(summary.prompt_tokens, 11)
        self.assertEqual(summary.completion_tokens, 7)
        self.assertEqual(summary.total_tokens, 18)
        self.assertEqual(summary.by_stage["router"].total_tokens, 18)

    def test_openai_compatible_stream_records_final_usage_event(self):
        config = LLMClientConfig(
            provider=settings.Provider.CUSTOM,
            base_url="https://example.test/v1",
            model="fake-model",
        )
        response = _FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"content":"hel"}}]}',
                "",
                'data: {"choices":[{"delta":{"content":"lo"}}]}',
                "",
                'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}',
                "",
                "data: [DONE]",
            ]
        )
        client = LLMClient(config)

        with patch("src.core.llm_client.requests.post", return_value=response) as post:
            chunks = list(client.stream_chat([{"role": "user", "content": "hi"}], usage_stage="final_answer"))

        self.assertEqual(chunks, ["hel", "lo"])
        self.assertTrue(post.call_args.kwargs["json"]["stream_options"]["include_usage"])
        summary = client.get_usage_summary()
        self.assertEqual(summary.total_tokens, 7)
        self.assertEqual(summary.by_stage["final_answer"].completion_tokens, 2)

    def test_openai_compatible_stream_retries_without_stream_options_when_rejected(self):
        config = LLMClientConfig(
            provider=settings.Provider.CUSTOM,
            base_url="https://example.test/v1",
            model="fake-model",
        )
        success = _FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"content":"ok"}}]}',
                "",
                "data: [DONE]",
            ]
        )
        client = LLMClient(config)

        with patch("src.core.llm_client.requests.post", side_effect=[_RejectStreamOptionsResponse(), success]) as post:
            chunks = list(client.stream_chat([{"role": "user", "content": "hi"}], usage_stage="final_answer"))

        self.assertEqual(chunks, ["ok"])
        self.assertIn("stream_options", post.call_args_list[0].kwargs["json"])
        self.assertNotIn("stream_options", post.call_args_list[1].kwargs["json"])
        summary = client.get_usage_summary()
        self.assertEqual(summary.call_count, 1)
        self.assertEqual(summary.usage_returned_count, 0)

    def test_ollama_stream_records_done_usage(self):
        config = LLMClientConfig(
            provider=settings.Provider.OLLAMA,
            base_url="http://ollama.test",
            model="fake-model",
        )
        response = _FakeStreamResponse(
            [
                '{"message":{"content":"a"},"done":false}',
                '{"message":{"content":"b"},"done":false}',
                '{"done":true,"prompt_eval_count":9,"eval_count":3}',
            ]
        )
        client = LLMClient(config)

        with patch("src.core.llm_client.requests.post", return_value=response):
            chunks = list(client.stream_chat([{"role": "user", "content": "hi"}], usage_stage="ollama_answer"))

        self.assertEqual(chunks, ["a", "b"])
        summary = client.get_usage_summary()
        self.assertEqual(summary.prompt_tokens, 9)
        self.assertEqual(summary.completion_tokens, 3)
        self.assertEqual(summary.total_tokens, 12)
        self.assertEqual(summary.by_stage["ollama_answer"].provider, "ollama")


if __name__ == "__main__":
    unittest.main()
