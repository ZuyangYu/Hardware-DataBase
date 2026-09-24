"""Phase 2 主动上下文压缩回归测试：

- create_chat_model 注入 max_input_tokens profile（未知中转才注入，已知 profile 让位）；
- 注入后 deepagents 的 SummarizationMiddleware 阈值从被动 170k 切换为
  fraction 主动压缩（85% 触发 / 保留 10%）。
"""

import unittest

import src.settings as app_settings
from src.core import model_factory
from src.core.model_factory import create_chat_model


class ModelProfileTests(unittest.TestCase):
    def setUp(self):
        create_chat_model.cache_clear()
        self._original = (
            app_settings.AGENT_LLM_PROVIDER,
            app_settings.AGENT_CUSTOM_BASE_URL,
            app_settings.AGENT_CUSTOM_API_KEY,
            app_settings.AGENT_CUSTOM_MODEL,
            app_settings.AGENT_CUSTOM_MAX_TOKENS,
            app_settings.AGENT_MODEL_MAX_INPUT_TOKENS,
        )
        app_settings.AGENT_LLM_PROVIDER = "custom"
        app_settings.AGENT_CUSTOM_BASE_URL = "http://localhost:1/v1"
        app_settings.AGENT_CUSTOM_API_KEY = "test-key"
        app_settings.AGENT_CUSTOM_MODEL = "test-model"
        app_settings.AGENT_CUSTOM_MAX_TOKENS = "1024"

    def tearDown(self):
        (
            app_settings.AGENT_LLM_PROVIDER,
            app_settings.AGENT_CUSTOM_BASE_URL,
            app_settings.AGENT_CUSTOM_API_KEY,
            app_settings.AGENT_CUSTOM_MODEL,
            app_settings.AGENT_CUSTOM_MAX_TOKENS,
            app_settings.AGENT_MODEL_MAX_INPUT_TOKENS,
        ) = self._original
        create_chat_model.cache_clear()

    def test_profile_injected_when_declared(self):
        app_settings.AGENT_MODEL_MAX_INPUT_TOKENS = 65536

        model = create_chat_model(provider="custom", model="test-model")

        self.assertIsInstance(model.profile, dict)
        self.assertEqual(model.profile["max_input_tokens"], 65536)

    def test_apply_model_profile_respects_existing_declaration(self):
        class _Model:
            profile: dict | None = {"max_input_tokens": 400000, "max_output_tokens": 64000}

        model = _Model()
        app_settings.AGENT_MODEL_MAX_INPUT_TOKENS = 65536
        model_factory._apply_model_profile(model)
        # provider/注册表已声明的窗口不被覆盖
        self.assertEqual(model.profile["max_input_tokens"], 400000)

    def test_apply_model_profile_fills_missing(self):
        class _Model:
            profile: dict | None = None

        model = _Model()
        app_settings.AGENT_MODEL_MAX_INPUT_TOKENS = 65536
        model_factory._apply_model_profile(model)
        self.assertEqual(model.profile["max_input_tokens"], 65536)

    def test_apply_model_profile_noop_when_zero(self):
        class _Model:
            profile: dict | None = None

        model = _Model()
        app_settings.AGENT_MODEL_MAX_INPUT_TOKENS = 0
        model_factory._apply_model_profile(model)
        self.assertIsNone(model.profile)

    def test_profile_not_injected_when_zero(self):
        app_settings.AGENT_MODEL_MAX_INPUT_TOKENS = 0

        model = create_chat_model(provider="custom", model="test-model")

        self.assertTrue(not model.profile or not model.profile.get("max_input_tokens"))


class SummarizationDefaultsTests(unittest.TestCase):
    def test_fraction_thresholds_activated_by_profile(self):
        from deepagents.middleware.summarization import compute_summarization_defaults

        class _ProfiledModel:
            profile = {"max_input_tokens": 65536}

        defaults = compute_summarization_defaults(_ProfiledModel())
        self.assertEqual(defaults["trigger"], ("fraction", 0.85))
        self.assertEqual(defaults["keep"], ("fraction", 0.10))

    def test_fixed_fallback_without_profile(self):
        from deepagents.middleware.summarization import compute_summarization_defaults

        class _BareModel:
            profile = None

        defaults = compute_summarization_defaults(_BareModel())
        self.assertEqual(defaults["trigger"], ("tokens", 170000))


if __name__ == "__main__":
    unittest.main()
