import importlib
import os
import unittest
from unittest.mock import patch


class SettingsEnvLoadingTests(unittest.TestCase):
    def test_reload_settings_reads_llm_rate_limit_configuration(self):
        import config.settings as settings

        values = {
            "AGENT_RATE_LIMIT_MAX_RETRIES": "2",
            "AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS": "0.25",
            "AGENT_RATE_LIMIT_MAX_DELAY_SECONDS": "3",
        }
        with patch.dict(os.environ, values), patch("config.settings.load_dotenv"):
            settings.reload_settings()

        self.assertEqual(settings.AGENT_RATE_LIMIT_MAX_RETRIES, 2)
        self.assertEqual(settings.AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS, 0.25)
        self.assertEqual(settings.AGENT_RATE_LIMIT_MAX_DELAY_SECONDS, 3.0)

    def test_loads_project_root_env_file_on_import_and_reload(self):
        with patch("dotenv.load_dotenv") as load_dotenv:
            import config.settings as settings

            settings = importlib.reload(settings)
            settings.reload_settings()

        expected_path = os.path.join(settings.BASE_DIR, ".env")
        expected_kwargs = {
            "dotenv_path": expected_path,
            "encoding": settings.ENV_FILE_ENCODING,
        }
        self.assertIn(expected_kwargs, [call.kwargs for call in load_dotenv.call_args_list])
        self.assertIn(
            {**expected_kwargs, "override": True},
            [call.kwargs for call in load_dotenv.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
