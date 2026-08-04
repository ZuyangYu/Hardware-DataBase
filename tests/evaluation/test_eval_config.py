import os
import unittest
from unittest.mock import patch

from src.evaluation.config import EvaluationConfig, EvaluationConfigurationError


class EvaluationConfigTests(unittest.TestCase):
    def test_eval_llm_falls_back_to_agent_custom_configuration(self):
        env = {
            "AGENT_LLM_PROVIDER": "custom",
            "AGENT_CUSTOM_BASE_URL": "https://example.test/v1",
            "AGENT_CUSTOM_API_KEY": "agent-key",
            "AGENT_CUSTOM_MODEL": "judge",
            "EVAL_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "EVAL_EMBEDDING_MODEL": "embed",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()

        self.assertEqual(config.llm_provider, "custom")
        self.assertEqual(config.llm_model, "judge")
        self.assertEqual(config.llm_base_url, "https://example.test/v1")

    def test_eval_llm_override_takes_precedence(self):
        env = {
            "AGENT_LLM_PROVIDER": "ollama",
            "AGENT_OLLAMA_BASE_URL": "http://agent:11434",
            "AGENT_OLLAMA_MODEL": "agent-model",
            "EVAL_LLM_PROVIDER": "custom",
            "EVAL_LLM_BASE_URL": "https://judge.test/v1",
            "EVAL_LLM_MODEL": "eval-model",
            "EVAL_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "EVAL_EMBEDDING_MODEL": "embed",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()

        self.assertEqual(config.llm_model, "eval-model")
        self.assertEqual(config.llm_base_url, "https://judge.test/v1")

    def test_ollama_fallback_uses_openai_compatible_v1_url(self):
        env = {
            "AGENT_LLM_PROVIDER": "ollama",
            "AGENT_OLLAMA_BASE_URL": "http://localhost:11434",
            "AGENT_OLLAMA_MODEL": "qwen",
            "EVAL_EMBEDDING_BASE_URL": "http://localhost:11434/v1",
            "EVAL_EMBEDDING_MODEL": "nomic-embed-text",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()

        self.assertEqual(config.llm_base_url, "http://localhost:11434/v1")

    def test_embedding_model_is_required(self):
        env = {
            "AGENT_LLM_PROVIDER": "ollama",
            "AGENT_OLLAMA_BASE_URL": "http://localhost:11434",
            "AGENT_OLLAMA_MODEL": "qwen",
            "EVAL_EMBEDDING_BASE_URL": "http://localhost:11434/v1",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(EvaluationConfigurationError, "EVAL_EMBEDDING_MODEL"):
                EvaluationConfig.from_environment()

    def test_scoring_budgets_have_safe_defaults(self):
        env = {
            "AGENT_LLM_PROVIDER": "custom",
            "AGENT_CUSTOM_BASE_URL": "https://judge.test/v1",
            "AGENT_CUSTOM_API_KEY": "agent-key",
            "AGENT_CUSTOM_MODEL": "judge",
            "EVAL_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "EVAL_EMBEDDING_MODEL": "embed",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()

        self.assertEqual(config.llm_max_tokens, 4096)
        self.assertEqual(config.max_contexts_per_sample, 4)
        self.assertEqual(config.max_context_chars, 2000)
        self.assertEqual(config.max_context_chars_per_item, 800)
        self.assertEqual(config.scoring_max_budget_attempts, 1)
        self.assertEqual(config.scoring_context_shrink_factor, 0.5)
        self.assertEqual(config.timeout_seconds, 60)
        self.assertEqual(config.max_retries, 0)
        self.assertEqual(
            config.public_metadata(),
            {
                "llm_provider": "custom",
                "llm_base_url": "https://judge.test/v1",
                "llm_model": "judge",
                "embedding_base_url": "https://embed.test/v1",
                "embedding_model": "embed",
                "llm_max_tokens": 4096,
                "max_contexts_per_sample": 4,
                "max_context_chars": 2000,
                "max_context_chars_per_item": 800,
                "scoring_max_budget_attempts": 1,
                "scoring_context_shrink_factor": 0.5,
                "timeout_seconds": 60,
                "max_workers": 4,
                "max_retries": 0,
            },
        )

    def test_evaluation_timeout_does_not_inherit_agent_timeout(self):
        env = {
            "AGENT_LLM_PROVIDER": "custom",
            "AGENT_CUSTOM_BASE_URL": "https://judge.test/v1",
            "AGENT_CUSTOM_API_KEY": "agent-key",
            "AGENT_CUSTOM_MODEL": "judge",
            "AGENT_TIMEOUT_SECONDS": "300",
            "EVAL_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "EVAL_EMBEDDING_MODEL": "embed",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()

        self.assertEqual(config.timeout_seconds, 60)

    def test_evaluation_runtime_overrides_remain_explicit(self):
        env = {
            "AGENT_LLM_PROVIDER": "custom",
            "AGENT_CUSTOM_BASE_URL": "https://judge.test/v1",
            "AGENT_CUSTOM_API_KEY": "agent-key",
            "AGENT_CUSTOM_MODEL": "judge",
            "EVAL_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "EVAL_EMBEDDING_MODEL": "embed",
            "EVAL_TIMEOUT_SECONDS": "90",
            "EVAL_MAX_RETRIES": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()

        self.assertEqual(config.timeout_seconds, 90)
        self.assertEqual(config.max_retries, 1)

    def test_scoring_budgets_use_environment_and_reject_non_positive_values(self):
        env = {
            "AGENT_LLM_PROVIDER": "custom",
            "AGENT_CUSTOM_BASE_URL": "https://judge.test/v1",
            "AGENT_CUSTOM_API_KEY": "agent-key",
            "AGENT_CUSTOM_MODEL": "judge",
            "EVAL_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "EVAL_EMBEDDING_MODEL": "embed",
            "EVAL_LLM_MAX_TOKENS": "2048",
            "EVAL_MAX_CONTEXTS_PER_SAMPLE": "3",
            "EVAL_MAX_CONTEXT_CHARS": "1000",
            "EVAL_MAX_CONTEXT_CHARS_PER_ITEM": "900",
            "EVAL_SCORING_MAX_BUDGET_ATTEMPTS": "4",
            "EVAL_SCORING_CONTEXT_SHRINK_FACTOR": "0.4",
        }
        with patch.dict(os.environ, env, clear=True):
            config = EvaluationConfig.from_environment()
            self.assertEqual(config.llm_max_tokens, 2048)
            self.assertEqual(config.max_contexts_per_sample, 3)
            self.assertEqual(config.max_context_chars, 1000)
            self.assertEqual(config.max_context_chars_per_item, 900)
            self.assertEqual(config.scoring_max_budget_attempts, 4)
            self.assertEqual(config.scoring_context_shrink_factor, 0.4)

        for name in (
            "EVAL_LLM_MAX_TOKENS",
            "EVAL_MAX_CONTEXTS_PER_SAMPLE",
            "EVAL_MAX_CONTEXT_CHARS",
            "EVAL_MAX_CONTEXT_CHARS_PER_ITEM",
            "EVAL_SCORING_MAX_BUDGET_ATTEMPTS",
            "EVAL_TIMEOUT_SECONDS",
            "EVAL_MAX_WORKERS",
        ):
            invalid = dict(env)
            invalid[name] = "0"
            with patch.dict(os.environ, invalid, clear=True):
                with self.assertRaisesRegex(EvaluationConfigurationError, name):
                    EvaluationConfig.from_environment()

        for value in ("-1", "not-an-integer"):
            invalid = dict(env)
            invalid["EVAL_MAX_RETRIES"] = value
            with patch.dict(os.environ, invalid, clear=True):
                with self.assertRaisesRegex(EvaluationConfigurationError, "EVAL_MAX_RETRIES"):
                    EvaluationConfig.from_environment()

        for value in ("0", "1", "not-a-number"):
            invalid = dict(env)
            invalid["EVAL_SCORING_CONTEXT_SHRINK_FACTOR"] = value
            with patch.dict(os.environ, invalid, clear=True):
                with self.assertRaisesRegex(
                    EvaluationConfigurationError,
                    "EVAL_SCORING_CONTEXT_SHRINK_FACTOR",
                ):
                    EvaluationConfig.from_environment()


if __name__ == "__main__":
    unittest.main()
