"""P0-008: provider configuration guards at save and startup."""

from __future__ import annotations

import unittest

import src.settings
from src.core.app_pipeline import AppPipeline


class ProviderConfigGuardTests(unittest.TestCase):
    def test_postgres_agent_checkpoint_requires_database_url(self):
        with self.assertRaises(ValueError) as ctx:
            src.settings.validate_provider_configuration(
                {"AGENT_CHECKPOINT_BACKEND": "postgres", "AGENT_CHECKPOINT_DATABASE_URL": ""}
            )
        self.assertIn("AGENT_CHECKPOINT_DATABASE_URL", str(ctx.exception))

    def test_postgres_agent_checkpoint_with_url_passes(self):
        src.settings.validate_provider_configuration(
            {
                "AGENT_CHECKPOINT_BACKEND": "postgres",
                "AGENT_CHECKPOINT_DATABASE_URL": "postgresql://user:pw@db:5432/hdb",
            }
        )


    def test_postgres_governor_requires_database_url(self):
        with self.assertRaises(ValueError) as ctx:
            src.settings.validate_provider_configuration(
                {
                    "RESOURCE_GOVERNOR_BACKEND": "postgres",
                    "RESOURCE_GOVERNOR_SCOPE": "deployment",
                    "RESOURCE_GOVERNOR_DATABASE_URL": "",
                }
            )
        self.assertIn("RESOURCE_GOVERNOR_DATABASE_URL", str(ctx.exception))

    def test_postgres_governor_requires_deployment_scope(self):
        with self.assertRaises(ValueError) as ctx:
            src.settings.validate_provider_configuration(
                {
                    "RESOURCE_GOVERNOR_BACKEND": "postgres",
                    "RESOURCE_GOVERNOR_SCOPE": "process",
                    "RESOURCE_GOVERNOR_DATABASE_URL": "postgresql://user:pw@db:5432/hdb",
                }
            )
        self.assertIn("RESOURCE_GOVERNOR_SCOPE", str(ctx.exception))

    def test_postgres_memory_store_requires_database_url(self):
        with self.assertRaises(ValueError) as ctx:
            src.settings.validate_provider_configuration(
                {"MEMORY_STORE_BACKEND": "postgres", "MEMORY_DATABASE_URL": ""}
            )
        self.assertIn("MEMORY_DATABASE_URL", str(ctx.exception))

    def test_unknown_backend_values_are_rejected(self):
        for key, value in (
            ("AGENT_CHECKPOINT_BACKEND", "redis"),
            ("MEMORY_STORE_BACKEND", "redis"),
            ("RESOURCE_GOVERNOR_BACKEND", "redis"),
        ):
            with self.assertRaises(ValueError):
                src.settings.validate_provider_configuration({key: value})

    def test_apply_settings_rejects_unsupported_provider_before_saving(self):
        writes: list[dict] = []
        original_validate = src.settings.validate_settings_values
        original_save = src.settings.save_settings_to_env
        original_reload = src.settings.reload_settings
        src.settings.validate_settings_values = lambda values: None
        src.settings.save_settings_to_env = lambda values: writes.append(dict(values))
        src.settings.reload_settings = lambda: None
        try:
            with self.assertRaises(ValueError):
                AppPipeline.apply_settings({"AGENT_CHECKPOINT_BACKEND": "redis"})
        finally:
            src.settings.validate_settings_values = original_validate
            src.settings.save_settings_to_env = original_save
            src.settings.reload_settings = original_reload
        self.assertEqual(writes, [], "invalid provider config must not be persisted")


if __name__ == "__main__":
    unittest.main()
