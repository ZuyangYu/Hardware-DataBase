"""T3: MCP input validation and the egress policy."""

from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from src.agents.mcp.validation import (
    McpValidationError,
    normalize_slug,
    redact_url,
    validate_department_scope,
    validate_headers,
    validate_name,
    validate_timeout,
    validate_transport,
    validate_url,
)


class McpValidationTests(unittest.TestCase):
    def test_slug_rules(self):
        self.assertEqual(normalize_slug("my-server_1"), "my-server_1")
        for bad in ("", "A", "-leading", "has space", "x" * 60, "中文"):
            with self.assertRaises(McpValidationError, msg=bad):
                normalize_slug(bad)

    def test_transport_rejects_stdio_and_unknown(self):
        self.assertEqual(validate_transport("streamable_http"), "streamable_http")
        self.assertEqual(validate_transport("SSE"), "sse")
        with self.assertRaises(McpValidationError) as ctx:
            validate_transport("stdio")
        self.assertIn("stdio", str(ctx.exception))
        with self.assertRaises(McpValidationError):
            validate_transport("websocket")

    def test_name_and_timeout_rules(self):
        self.assertEqual(validate_name(" 服务 "), "服务")
        with self.assertRaises(McpValidationError):
            validate_name("")
        self.assertIsNone(validate_timeout(None, field="timeout_s"))
        self.assertEqual(validate_timeout("30", field="timeout_s"), 30)
        with self.assertRaises(McpValidationError):
            validate_timeout("soon", field="timeout_s")
        with self.assertRaises(McpValidationError):
            validate_timeout(0, field="timeout_s")
        with self.assertRaises(McpValidationError):
            validate_timeout(99999, field="timeout_s")

    def test_headers_bounds(self):
        self.assertEqual(validate_headers({"Authorization": "Bearer x"}), {"Authorization": "Bearer x"})
        self.assertEqual(validate_headers(None), {})
        with self.assertRaises(McpValidationError):
            validate_headers(["nope"])
        with self.assertRaises(McpValidationError):
            validate_headers({f"k{i}": "v" for i in range(40)})
        with self.assertRaises(McpValidationError):
            validate_headers({"X-Big": "x" * 5000})
        with self.assertRaises(McpValidationError):
            validate_headers({"": "v"})

    def test_department_scope(self):
        self.assertEqual(validate_department_scope("none", [1, 2]), ("none", []))
        self.assertEqual(
            validate_department_scope("departments", [1, "2", 2]), ("departments", [1, 2])
        )
        self.assertEqual(validate_department_scope("all", None), ("all", []))
        with self.assertRaises(McpValidationError):
            validate_department_scope("departments", [])
        with self.assertRaises(McpValidationError):
            validate_department_scope("sometimes", [1])

    # ------------------------------------------------------------------
    def test_url_policy_accepts_public_https(self):
        with patch("src.agents.mcp.validation._resolve_host", return_value=["93.184.216.34"]):
            self.assertEqual(
                validate_url("https://mcp.example.com/mcp"),
                "https://mcp.example.com/mcp",
            )

    def test_url_policy_rejects_bad_schemes_and_userinfo(self):
        for bad in ("file:///etc/passwd", "gopher://x/1", "data:text/plain,x", ""):
            with self.assertRaises(McpValidationError, msg=bad):
                validate_url(bad)
        with self.assertRaises(McpValidationError) as ctx:
            validate_url("https://user:pass@mcp.example.com/mcp")
        self.assertIn("用户名或密码", str(ctx.exception))

    def test_url_policy_http_requires_explicit_flag(self):
        with patch("src.agents.mcp.validation._resolve_host", return_value=["93.184.216.34"]):
            with self.assertRaises(McpValidationError):
                validate_url("http://mcp.example.com/mcp")
            self.assertEqual(
                validate_url("http://mcp.example.com/mcp", allow_insecure_http=True),
                "http://mcp.example.com/mcp",
            )

    def test_url_policy_blocks_private_and_loopback(self):
        for bad in ("https://127.0.0.1:8000/mcp", "https://10.0.0.5/mcp", "https://[::1]/mcp"):
            with self.assertRaises(McpValidationError, msg=bad):
                validate_url(bad)
        self.assertEqual(
            validate_url("http://127.0.0.1:8000/mcp", allow_insecure_http=True, allow_private_hosts=True),
            "http://127.0.0.1:8000/mcp",
        )

    def test_url_policy_blocks_hostname_resolving_to_private(self):
        with patch("src.agents.mcp.validation._resolve_host", return_value=["10.0.0.7"]):
            with self.assertRaises(McpValidationError):
                validate_url("https://internal.example.com/mcp")

    def test_url_policy_rejects_unresolvable_host(self):
        with patch("src.agents.mcp.validation._resolve_host", side_effect=socket.gaierror("dns")):
            with self.assertRaises(McpValidationError):
                validate_url("https://missing.example.com/mcp")

    def test_redact_url_strips_credentials_query_and_fragment(self):
        self.assertEqual(
            redact_url("https://user:pass@mcp.example.com/mcp?token=secret#frag"),
            "https://mcp.example.com/mcp",
        )
        self.assertEqual(redact_url("not a url"), "<invalid-url>")


if __name__ == "__main__":
    unittest.main()
