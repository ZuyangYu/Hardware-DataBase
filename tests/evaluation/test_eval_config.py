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


if __name__ == "__main__":
    unittest.main()
