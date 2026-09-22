"""T4: external_tool.invoke capability deployment and authorization."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import src.settings as settings
from src.agents.orchestration.capabilities import (
    CapabilityResolver,
    authorized_capabilities_for_conversation,
    compute_effective_capabilities,
    deployed_capabilities_from_settings,
)


def _conversation_kwargs(**overrides):
    kwargs = {
        "has_kb_read": True,
        "has_kb_write": False,
        "has_attachments": False,
        "has_document_context": False,
        "has_task": False,
    }
    kwargs.update(overrides)
    return kwargs


class McpCapabilityTests(unittest.TestCase):
    def _deployed(self, *, mcp: bool, unified: bool, governor: bool) -> set[str]:
        with (
            patch.object(settings, "MCP_ENABLED", mcp),
            patch.object(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", unified),
            patch.object(settings, "RESOURCE_GOVERNOR_ENABLED", governor),
        ):
            return deployed_capabilities_from_settings()

    def test_deployed_requires_all_three_switches(self):
        self.assertNotIn("external_tool.invoke", self._deployed(mcp=False, unified=True, governor=True))
        self.assertNotIn("external_tool.invoke", self._deployed(mcp=True, unified=False, governor=True))
        self.assertNotIn("external_tool.invoke", self._deployed(mcp=True, unified=True, governor=False))
        self.assertIn("external_tool.invoke", self._deployed(mcp=True, unified=True, governor=True))

    def test_authorized_only_with_authenticated_mcp_access(self):
        without = authorized_capabilities_for_conversation(**_conversation_kwargs())
        self.assertNotIn("external_tool.invoke", without)
        with_access = authorized_capabilities_for_conversation(
            **_conversation_kwargs(has_mcp_access=True)
        )
        self.assertIn("external_tool.invoke", with_access)

    def test_effective_is_intersection_and_constraints_do_not_touch_it(self):
        effective = compute_effective_capabilities(
            deployed={"external_tool.invoke", "evidence.search"},
            authorized={"external_tool.invoke", "evidence.search"},
            accepted_constraints=[{"kind": "forbid_action", "value": "export"}],
        )
        self.assertIn("external_tool.invoke", effective)

        missing = compute_effective_capabilities(
            deployed={"evidence.search"},
            authorized={"external_tool.invoke"},
        )
        self.assertNotIn("external_tool.invoke", missing)

    def test_resolver_describes_external_kind(self):
        resolver = CapabilityResolver(deployed={"external_tool.invoke"})
        described = resolver.describe(
            authorized={"external_tool.invoke"},
        )
        entry = next(item for item in described if item.capability_id == "external_tool.invoke")
        self.assertEqual(entry.kind, "external")
        self.assertEqual(entry.status, "available")


if __name__ == "__main__":
    unittest.main()
