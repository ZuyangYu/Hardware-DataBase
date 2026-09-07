"""Selected-page visual analysis contracts for chat attachments."""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import src.settings
from src.agents.tools.attachment_tools import build_attachment_tools
from src.agents.tools.runtime import ToolRuntime
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore
from src.attachments.visual import AttachmentVisualAnalyzer
from src.core.multimodal_gateway import (
    VisualAnalysis,
    VolcengineArkGateway,
    default_multimodal_gateway,
)


class FakePageRenderer:
    def __init__(self, images: dict[int, bytes] | None = None):
        self.images = images or {}
        self.calls: list[tuple[str, int]] = []

    def render_page(self, source_path: str, page_number: int) -> bytes:
        self.calls.append((source_path, page_number))
        return self.images.get(page_number, f"PNG-{page_number}".encode())


class FakeVisualGateway:
    provider = "fake_visual"
    model = "fake-model"

    def __init__(self, result: VisualAnalysis | None = None, error: Exception | None = None):
        self.result = result or VisualAnalysis(
            text="检测到第 2 页包含 U2 与 VDD_3V3 的连接。",
            provider=self.provider,
            model=self.model,
            request_id="req-visual-1",
        )
        self.error = error
        self.calls: list[dict] = []

    def analyze(self, *, question: str, image_bytes: bytes, text_context: str = "") -> VisualAnalysis:
        self.calls.append({
            "question": question,
            "image_bytes": image_bytes,
            "text_context": text_context,
        })
        if self.error:
            raise self.error
        return self.result


class AttachmentVisualTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old: dict[str, object] = {}
        self._patch("CHAT_ATTACHMENT_INDEX_DB_PATH", os.path.join(self.tmp.name, "att.db"))
        self._patch("CHAT_ATTACHMENT_STORAGE_DIR", os.path.join(self.tmp.name, "storage"))
        self._patch("CHAT_ATTACHMENTS_ENABLED", True)
        self._patch("CHAT_ATTACHMENT_VISUAL_ENABLED", False)
        self._patch("CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED", False)
        self._patch("CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN", 3)
        self._patch("CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES", 1024)
        self._patch("CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS", 10)
        self._patch("CHAT_ATTACHMENT_VISUAL_PROVIDER", "volcengine_ark")
        self._patch("CHAT_ATTACHMENT_VISUAL_BASE_URL", "https://ark.example/api/v3")
        self._patch("CHAT_ATTACHMENT_VISUAL_MODEL", "doubao-model")
        self._patch("ARK_API_KEY", "")
        self._patch("MEMORY_EMBEDDING_API_KEY", "memory-secret")
        self.store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
        self.service = AttachmentService(store=self.store)
        self.record = self.service.upload(
            session_id=1,
            user_id=1,
            filename="schematic.pdf",
            stream=__import__("io").BytesIO(b"%PDF-fake-source"),
        )
        asset = self.store.get_asset(self.record.asset_id)
        assert asset is not None
        self.store.update_asset_status(
            asset.asset_id,
            parse_status="ready",
            manifest={"format": "pdf", "page_count": 4},
        )
        self.ref = self.service.build_refs([self.record])[0]

    def _patch(self, name: str, value: object):
        if name not in self._old:
            self._old[name] = getattr(src.settings, name, None)
        setattr(src.settings, name, value)

    def tearDown(self):
        for name, value in self._old.items():
            if value is None and not hasattr(src.settings, name):
                continue
            setattr(src.settings, name, value)


class AttachmentVisualAnalyzerTests(AttachmentVisualTestBase):
    def test_visual_disabled_does_not_call_gateway_or_renderer(self):
        gateway = FakeVisualGateway()
        renderer = FakePageRenderer()
        result = AttachmentVisualAnalyzer(
            store=self.store, gateway=gateway, renderer=renderer
        ).analyze(refs=[self.ref], question="分析原理图", page_numbers=[2])

        self.assertEqual(result.evidence, [])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(renderer.calls, [])
        self.assertEqual(result.degraded_reasons, [])

    def test_remote_visual_requires_both_feature_and_policy_flags(self):
        src.settings.CHAT_ATTACHMENT_VISUAL_ENABLED = True
        gateway = FakeVisualGateway()
        renderer = FakePageRenderer()
        result = AttachmentVisualAnalyzer(
            store=self.store, gateway=gateway, renderer=renderer
        ).analyze(refs=[self.ref], question="分析原理图", page_numbers=[2])

        self.assertEqual(result.evidence, [])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(renderer.calls, [])
        self.assertIn("visual_remote_disabled", result.degraded_reasons)

    def test_analyzer_sends_only_selected_page_and_returns_visual_evidence_metadata(self):
        src.settings.CHAT_ATTACHMENT_VISUAL_ENABLED = True
        src.settings.CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED = True
        gateway = FakeVisualGateway()
        renderer = FakePageRenderer({2: b"PNG-page-2-only"})
        result = AttachmentVisualAnalyzer(
            store=self.store, gateway=gateway, renderer=renderer
        ).analyze(refs=[self.ref], question="这个页面的网络连接是什么？", page_numbers=[2])

        self.assertEqual([call[1] for call in renderer.calls], [2])
        self.assertEqual(gateway.calls[0]["image_bytes"], b"PNG-page-2-only")
        self.assertNotIn(b"%PDF", gateway.calls[0]["image_bytes"])
        self.assertEqual(len(result.evidence), 1)
        evidence = result.evidence[0]
        self.assertEqual(evidence.content_kind, "visual_evidence")
        self.assertEqual(evidence.locator["page"], 2)
        self.assertEqual(evidence.metadata["provider"], "fake_visual")
        self.assertEqual(evidence.metadata["model"], "fake-model")
        self.assertEqual(evidence.metadata["request_id"], "req-visual-1")
        self.assertEqual(evidence.metadata["attachment_id"], self.ref.attachment_id)

    def test_analyzer_enforces_page_and_image_limits_without_network(self):
        src.settings.CHAT_ATTACHMENT_VISUAL_ENABLED = True
        src.settings.CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED = True
        src.settings.CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN = 2
        src.settings.CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES = 4
        gateway = FakeVisualGateway()
        renderer = FakePageRenderer({1: b"too-large", 2: b"ok", 3: b"ok"})
        result = AttachmentVisualAnalyzer(
            store=self.store, gateway=gateway, renderer=renderer
        ).analyze(refs=[self.ref], question="分析", page_numbers=[0, 1, 2, 3, 9])

        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual([call[1] for call in renderer.calls], [1, 2])
        self.assertEqual(result.evidence[0].locator["page"], 2)
        self.assertIn("visual_page_out_of_bounds", result.degraded_reasons)
        self.assertIn("visual_image_too_large", result.degraded_reasons)
        self.assertIn("visual_page_limit", result.degraded_reasons)

    def test_provider_failure_only_degrades_and_does_not_raise(self):
        src.settings.CHAT_ATTACHMENT_VISUAL_ENABLED = True
        src.settings.CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED = True
        gateway = FakeVisualGateway(error=TimeoutError("provider timed out"))
        result = AttachmentVisualAnalyzer(
            store=self.store, gateway=gateway, renderer=FakePageRenderer()
        ).analyze(refs=[self.ref], question="分析", page_numbers=[2])

        self.assertEqual(result.evidence, [])
        self.assertIn("visual_failed", result.degraded_reasons)

    def test_visual_tool_records_provider_failure_metric(self):
        from src.agents.tools.attachment_tools import make_attachment_visual_analyze

        src.settings.CHAT_ATTACHMENT_VISUAL_ENABLED = True
        src.settings.CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED = True
        runtime = ToolRuntime(
            kb_name="",
            ctx=None,
            attachment_refs=[self.ref],
            source_scope="attachment_only",
            attachment_service=self.service,
            attachment_visual_analyzer=AttachmentVisualAnalyzer(
                store=self.store,
                gateway=FakeVisualGateway(error=TimeoutError("timeout")),
                renderer=FakePageRenderer(),
            ),
            attachment_user_id=1,
            attachment_session_id=1,
        )
        with patch("src.observability.metrics.record_attachment") as metric:
            make_attachment_visual_analyze(runtime)("分析", page=2)

        self.assertTrue(
            any(call.args[0] == "visual_failure" for call in metric.call_args_list)
        )

    def test_visual_agent_tool_is_scoped_and_returns_shared_evidence(self):
        src.settings.CHAT_ATTACHMENT_VISUAL_ENABLED = True
        src.settings.CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED = True
        analyzer = AttachmentVisualAnalyzer(
            store=self.store,
            gateway=FakeVisualGateway(),
            renderer=FakePageRenderer({2: b"PNG-page-2-only"}),
        )
        runtime = ToolRuntime(
            kb_name="",
            ctx=None,
            attachment_refs=[self.ref],
            source_scope="attachment_only",
            attachment_service=self.service,
            attachment_visual_analyzer=analyzer,
            attachment_user_id=1,
            attachment_session_id=1,
        )

        tools = build_attachment_tools(runtime)
        visual_tool = next(tool for tool in tools if tool.__name__ == "attachment_visual_analyze")
        output = visual_tool("识别这一页的器件", page=2)

        self.assertIn("U2", output)
        self.assertEqual(len(runtime.evidence), 1)
        self.assertEqual(runtime.evidence[0].content_kind, "visual_evidence")

    def test_rebuilding_parts_invalidates_visual_cache(self):
        self.store.save_visual_cache(
            asset_id=self.record.asset_id,
            page_number=2,
            provider="fake_visual",
            model="fake-model",
            question_hash="hash-1",
            content="old visual result",
            request_id="req-old",
        )

        self.store.replace_parts(self.record.asset_id, [])

        self.assertIsNone(
            self.store.get_visual_cache(
                asset_id=self.record.asset_id,
                page_number=2,
                provider="fake_visual",
                model="fake-model",
                question_hash="hash-1",
            )
        )


class MultimodalGatewayTests(AttachmentVisualTestBase):
    def test_default_gateway_requires_ark_key_and_never_reads_memory_key(self):
        src.settings.CHAT_ATTACHMENT_VISUAL_ENABLED = True
        src.settings.CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED = True
        src.settings.ARK_API_KEY = ""
        with patch("src.core.multimodal_gateway.httpx.post") as post:
            self.assertIsNone(default_multimodal_gateway())
        post.assert_not_called()

        src.settings.ARK_API_KEY = "ark-secret"
        gateway = default_multimodal_gateway()
        self.assertIsNotNone(gateway)
        self.assertEqual(getattr(gateway, "api_key"), "ark-secret")

    def test_ark_gateway_sends_only_png_data_url_and_parses_response_id(self):
        response = Mock()
        response.json.return_value = {
            "id": "resp-123",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "图中有 U2。"}],
            }],
        }
        gateway = VolcengineArkGateway(
            base_url="https://ark.example/api/v3",
            api_key="ark-secret",
            model="doubao-model",
        )
        image = b"PNG-page-2-only"
        with patch("src.core.multimodal_gateway.httpx.post", return_value=response) as post:
            result = gateway.analyze(
                question="分析该页",
                image_bytes=image,
                text_context="最小文本上下文",
            )

        request = post.call_args.kwargs
        content = request["json"]["input"][0]["content"]
        image_item = next(item for item in content if item["type"] == "input_image")
        self.assertEqual(
            image_item["image_url"],
            "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
        )
        self.assertNotIn("%PDF", image_item["image_url"])
        self.assertEqual(result.text, "图中有 U2。")
        self.assertEqual(result.request_id, "resp-123")

    def test_ark_gateway_invalid_response_is_fail_soft_at_analyzer_boundary(self):
        response = Mock()
        response.json.return_value = {"id": "resp-empty", "output": []}
        gateway = VolcengineArkGateway(
            base_url="https://ark.example/api/v3",
            api_key="ark-secret",
            model="doubao-model",
        )
        with patch("src.core.multimodal_gateway.httpx.post", return_value=response):
            with self.assertRaisesRegex(ValueError, "text"):
                gateway.analyze(question="分析", image_bytes=b"PNG")


if __name__ == "__main__":
    unittest.main()
