import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import src.settings as settings


class DocumentAuthoringSettingsTests(unittest.TestCase):
    def test_document_authoring_flags_default_to_off_when_not_configured(self):
        env = os.environ.copy()
        env.pop("DOCUMENT_AUTHORING_AGENT_MODE_ENABLED", None)
        env.pop("AGENT_DOCUMENT_TOOLS_ENABLED", None)
        # The assertion is about an unconfigured environment; do not let a
        # developer's repository-level .env turn the subprocess into a
        # configured deployment.
        env["PYTHON_DOTENV_DISABLED"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import src.settings as s; "
                    "assert s.DOCUMENT_AUTHORING_AGENT_MODE_ENABLED is False; "
                    "assert s.AGENT_DOCUMENT_TOOLS_ENABLED is False"
                ),
            ],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_document_authoring_batch_time_budget_has_ten_second_default(self):
        env = os.environ.copy()
        env.pop("DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS", None)
        env["PYTHON_DOTENV_DISABLED"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import src.settings as s; "
                    "assert s.DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS == 10; "
                    "assert s.DEFAULT_VALUES[\"DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS\"] == \"10\""
                ),
            ],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_new_numeric_settings_are_validated_before_env_persistence(self):
        settings.validate_settings_values(
            {"DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS": "10.5"}
        )
        with self.assertRaisesRegex(ValueError, "DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS"):
            settings.validate_settings_values(
                {"DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS": "not-a-number"}
            )
        with self.assertRaisesRegex(ValueError, "AGENT_MODEL_MAX_INPUT_TOKENS"):
            settings.validate_settings_values({"AGENT_MODEL_MAX_INPUT_TOKENS": "not-an-integer"})

    def test_document_task_rollout_flags_have_safe_defaults_and_reload(self):
        env = os.environ.copy()
        env.pop("DOCUMENT_TASK_WRITE_ENABLED", None)
        env.pop("DOCUMENT_TASK_READ_ENABLED", None)
        env.pop("DOCUMENT_TASK_ASSOCIATION_REQUIRED", None)
        env["PYTHON_DOTENV_DISABLED"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import src.settings as s; "
                    "assert s.DOCUMENT_TASK_WRITE_ENABLED is True; "
                    "assert s.DOCUMENT_TASK_READ_ENABLED is True; "
                    "assert s.DOCUMENT_TASK_ASSOCIATION_REQUIRED is False"
                ),
            ],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        values = {
            "DOCUMENT_TASK_WRITE_ENABLED": "false",
            "DOCUMENT_TASK_READ_ENABLED": "false",
            "DOCUMENT_TASK_ASSOCIATION_REQUIRED": "true",
        }
        previous = (
            settings.DOCUMENT_TASK_WRITE_ENABLED,
            settings.DOCUMENT_TASK_READ_ENABLED,
            settings.DOCUMENT_TASK_ASSOCIATION_REQUIRED,
        )
        try:
            with patch.dict(os.environ, values), patch("src.settings.load_dotenv"):
                settings.reload_settings()

            self.assertFalse(settings.DOCUMENT_TASK_WRITE_ENABLED)
            self.assertFalse(settings.DOCUMENT_TASK_READ_ENABLED)
            self.assertTrue(settings.DOCUMENT_TASK_ASSOCIATION_REQUIRED)
        finally:
            (
                settings.DOCUMENT_TASK_WRITE_ENABLED,
                settings.DOCUMENT_TASK_READ_ENABLED,
                settings.DOCUMENT_TASK_ASSOCIATION_REQUIRED,
            ) = previous

    def test_requirement_resolver_rollout_flag_defaults_off_and_reloads(self):
        env = os.environ.copy()
        env.pop("DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED", None)
        env["PYTHON_DOTENV_DISABLED"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import src.settings as s; "
                    "assert s.DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED is False; "
                    "assert s.DEFAULT_VALUES[\"DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED\"] == \"false\""
                ),
            ],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        previous = settings.DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED
        try:
            with patch.dict(os.environ, {"DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED": "true"}), patch(
                "src.settings.load_dotenv"
            ):
                settings.reload_settings()
            self.assertTrue(settings.DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED)
        finally:
            settings.DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED = previous

    def test_document_planning_shadow_flags_default_off_and_reload(self):
        env = os.environ.copy()
        env.pop("DOCUMENT_PLANNING_SHADOW_ENABLED", None)
        env.pop("DOCUMENT_PLANNING_V2_ENABLED", None)
        env["PYTHON_DOTENV_DISABLED"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import src.settings as s; "
                    "assert s.DOCUMENT_PLANNING_SHADOW_ENABLED is False; "
                    "assert s.DOCUMENT_PLANNING_V2_ENABLED is False; "
                    "assert s.DEFAULT_VALUES['DOCUMENT_PLANNING_SHADOW_ENABLED'] == 'false'; "
                    "assert s.DEFAULT_VALUES['DOCUMENT_PLANNING_V2_ENABLED'] == 'false'"
                ),
            ],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        previous = (
            settings.DOCUMENT_PLANNING_SHADOW_ENABLED,
            settings.DOCUMENT_PLANNING_V2_ENABLED,
        )
        try:
            with patch.dict(
                os.environ,
                {
                    "DOCUMENT_PLANNING_SHADOW_ENABLED": "true",
                    "DOCUMENT_PLANNING_V2_ENABLED": "true",
                },
            ), patch("src.settings.load_dotenv"):
                settings.reload_settings()
            self.assertTrue(settings.DOCUMENT_PLANNING_SHADOW_ENABLED)
            self.assertTrue(settings.DOCUMENT_PLANNING_V2_ENABLED)
        finally:
            (
                settings.DOCUMENT_PLANNING_SHADOW_ENABLED,
                settings.DOCUMENT_PLANNING_V2_ENABLED,
            ) = previous


if __name__ == "__main__":
    unittest.main()
