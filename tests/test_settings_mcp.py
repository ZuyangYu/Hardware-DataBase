"""T1: MCP settings, config validation and the dedicated governor resource."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import src.settings as settings
from src.core.concurrency.resource_governor import _limits_from_settings


_MCP_KEYS = (
    "MCP_ENABLED",
    "MCP_ALLOW_INSECURE_HTTP",
    "MCP_ALLOW_PRIVATE_HOSTS",
    "MCP_CONNECT_TIMEOUT_SECONDS",
    "MCP_CALL_TIMEOUT_SECONDS",
    "MCP_SSE_READ_TIMEOUT_SECONDS",
    "MCP_MAX_RESULT_CHARS",
    "MCP_MAX_RESULT_BYTES",
    "MCP_MAX_ARGUMENT_CHARS",
    "MCP_MAX_TOOLS_PER_SERVER",
    "MCP_MAX_SERVERS_PER_RUN",
    "MCP_CALLS_PER_TURN",
    "MCP_FAILURE_COOLDOWN_SECONDS",
    "MCP_WRITE_ENABLED",
    "MCP_WRITE_CONFIRM_TTL_SECONDS",
    "MCP_MAX_ARTIFACT_BYTES",
    "MCP_ARTIFACT_ALLOWED_EXTENSIONS",
    "MCP_CREDENTIALS_KEY",
    "RESOURCE_MCP_MAX_CONCURRENT",
    "RESOURCE_MCP_RESERVED_INTERACTIVE",
)

_ENV_TOUCHED = _MCP_KEYS + (
    "CONVERSATION_UNIFIED_TOOLS_ENABLED",
    "RESOURCE_GOVERNOR_ENABLED",
)


class McpSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {key: os.environ.get(key) for key in _ENV_TOUCHED}

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        with patch("src.settings.load_dotenv"):
            settings.reload_settings()

    def _reload(self, values: dict[str, str]) -> None:
        with patch.dict(os.environ, values), patch("src.settings.load_dotenv"):
            settings.reload_settings()

    def test_default_values_include_mcp_keys_and_keep_credentials_env_only(self):
        for key in _MCP_KEYS:
            if key == "MCP_CREDENTIALS_KEY":
                continue
            self.assertIn(key, settings.DEFAULT_VALUES, key)
        # Built-in feature: the master switch defaults on and acts as an
        # emergency kill switch; risk gates (write/insecure/private) stay off.
        self.assertEqual(settings.DEFAULT_VALUES["MCP_ENABLED"], "true")
        self.assertEqual(settings.DEFAULT_VALUES["MCP_WRITE_ENABLED"], "false")
        self.assertEqual(settings.DEFAULT_VALUES["OBS_CAPTURE_MCP_CONTENT"], "false")
        # The credentials key must stay environment-only: returning it through
        # GET /config or letting PUT /config rotate it would defeat redaction.
        self.assertNotIn("MCP_CREDENTIALS_KEY", settings.DEFAULT_VALUES)

    def test_reload_reads_mcp_settings_from_env(self):
        startup_credentials_key = settings.MCP_CREDENTIALS_KEY
        self._reload(
            {
                "MCP_ENABLED": "true",
                "CONVERSATION_UNIFIED_TOOLS_ENABLED": "true",
                "RESOURCE_GOVERNOR_ENABLED": "true",
                "MCP_CALL_TIMEOUT_SECONDS": "7",
                "MCP_MAX_RESULT_CHARS": "1234",
                "MCP_MAX_RESULT_BYTES": "2048",
                "MCP_CALLS_PER_TURN": "3",
                "MCP_WRITE_ENABLED": "true",
                "MCP_WRITE_CONFIRM_TTL_SECONDS": "60",
                "MCP_MAX_ARTIFACT_BYTES": "1024",
                "MCP_ARTIFACT_ALLOWED_EXTENSIONS": "kicad_sch,zip",
                "MCP_CREDENTIALS_KEY": "test-key-material",
                "RESOURCE_MCP_MAX_CONCURRENT": "6",
                "RESOURCE_MCP_RESERVED_INTERACTIVE": "2",
            }
        )
        self.assertTrue(settings.MCP_ENABLED)
        self.assertEqual(settings.MCP_CALL_TIMEOUT_SECONDS, 7)
        self.assertEqual(settings.MCP_MAX_RESULT_CHARS, 1234)
        self.assertEqual(settings.MCP_MAX_RESULT_BYTES, 2048)
        self.assertEqual(settings.MCP_CALLS_PER_TURN, 3)
        self.assertTrue(settings.MCP_WRITE_ENABLED)
        self.assertEqual(settings.MCP_WRITE_CONFIRM_TTL_SECONDS, 60)
        self.assertEqual(settings.MCP_MAX_ARTIFACT_BYTES, 1024)
        self.assertEqual(settings.MCP_ARTIFACT_ALLOWED_EXTENSIONS, "kicad_sch,zip")
        # Credential rotation is construction-time wiring: hot settings reload
        # must keep the key used by existing encrypted stores and clients.
        self.assertEqual(settings.MCP_CREDENTIALS_KEY, startup_credentials_key)
        self.assertEqual(settings.RESOURCE_MCP_MAX_CONCURRENT, 6)
        self.assertEqual(settings.RESOURCE_MCP_RESERVED_INTERACTIVE, 2)

    def test_observability_is_not_part_of_the_user_config_api_surface(self):
        from src.api.routes.config import _config_surface_keys

        keys = _config_surface_keys()
        self.assertNotIn("OBS_ENABLED", keys)
        self.assertNotIn("OBS_TRACE_SAMPLE_RATIO", keys)
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", keys)
        self.assertNotIn("CONVERSATION_UNIFIED_TOOLS_ENABLED", keys)
        self.assertNotIn("RESOURCE_GOVERNOR_ENABLED", keys)
        self.assertIn("MCP_ENABLED", keys)

    def test_integer_mcp_keys_are_validated_before_persisting(self):
        for key in ("MCP_CALL_TIMEOUT_SECONDS", "MCP_MAX_ARTIFACT_BYTES", "RESOURCE_MCP_MAX_CONCURRENT"):
            self.assertIn(key, settings._INT_SETTING_KEYS, key)
        with self.assertRaises(ValueError) as ctx:
            settings.validate_settings_values({"MCP_CALL_TIMEOUT_SECONDS": "soon"})
        self.assertIn("MCP_CALL_TIMEOUT_SECONDS", str(ctx.exception))
        settings.validate_settings_values({"MCP_CALL_TIMEOUT_SECONDS": "30"})

    def test_mcp_repairs_legacy_platform_prerequisites(self):
        self._reload(
            {
                "MCP_ENABLED": "true",
                "CONVERSATION_UNIFIED_TOOLS_ENABLED": "false",
                "RESOURCE_GOVERNOR_ENABLED": "false",
            }
        )
        self.assertTrue(settings.CONVERSATION_UNIFIED_TOOLS_ENABLED)
        self.assertTrue(settings.RESOURCE_GOVERNOR_ENABLED)
        settings.validate_provider_configuration({"MCP_ENABLED": "true"})

    def test_config_route_maps_mcp_validation_to_400(self):
        """PUT /config surfaces the prerequisite error instead of a generic 500."""

        from fastapi import HTTPException

        from src.api.routes.config import update_config
        from src.api.schemas import UpdateConfigRequest

        class _BoomPipeline:
            @staticmethod
            def apply_settings(_settings):  # noqa: ANN001 - test stub
                raise ValueError(
                    "MCP_ENABLED=true 需要 RESOURCE_GOVERNOR_ENABLED=true，否则 MCP 调用没有独立配额"
                )

        with self.assertRaises(HTTPException) as ctx:
            update_config(
                UpdateConfigRequest(settings={"MCP_ENABLED": "true"}),
                _actor=object(),
                pipeline=_BoomPipeline(),
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("MCP_ENABLED", str(ctx.exception.detail))
        self.assertIn("RESOURCE_GOVERNOR_ENABLED", str(ctx.exception.detail))

        with self.assertRaises(HTTPException) as unknown:
            update_config(
                UpdateConfigRequest(settings={"NOT_A_SETTING": "1"}),
                _actor=object(),
                pipeline=_BoomPipeline(),
            )
        self.assertEqual(unknown.exception.status_code, 400)

    def test_governor_limits_include_mcp_resource(self):
        with (
            patch.object(settings, "RESOURCE_MCP_MAX_CONCURRENT", 6),
            patch.object(settings, "RESOURCE_MCP_RESERVED_INTERACTIVE", 2),
        ):
            limits = _limits_from_settings()
        self.assertEqual(limits["mcp"], (6, 2))

        with (
            patch.object(settings, "RESOURCE_MCP_MAX_CONCURRENT", 1),
            patch.object(settings, "RESOURCE_MCP_RESERVED_INTERACTIVE", 5),
        ):
            # reserved is clamped to capacity by the governor's _pair helper
            limits = _limits_from_settings()
        self.assertEqual(limits["mcp"], (1, 1))


if __name__ == "__main__":
    unittest.main()
