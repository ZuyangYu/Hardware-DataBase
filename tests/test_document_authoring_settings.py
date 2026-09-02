import os
import subprocess
import sys
import unittest

import src.settings as settings


class DocumentAuthoringSettingsTests(unittest.TestCase):
    def test_document_authoring_flags_default_to_off_when_not_configured(self):
        env = os.environ.copy()
        env.pop("DOCUMENT_AUTHORING_AGENT_MODE_ENABLED", None)
        env.pop("AGENT_DOCUMENT_TOOLS_ENABLED", None)
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


if __name__ == "__main__":
    unittest.main()
