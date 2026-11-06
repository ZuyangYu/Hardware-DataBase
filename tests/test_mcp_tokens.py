from __future__ import annotations

import pytest

from src.core import mcp_tokens


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_TOKEN_DB_PATH", str(tmp_path / "mcp_tokens.db"))
    mcp_tokens._INITIALIZED.clear()
    yield
    mcp_tokens._INITIALIZED.clear()


def test_create_status_resolve_and_revoke():
    created = mcp_tokens.create_or_rotate(4, name="test")
    token = created["token"]
    assert token.startswith("hdbmcp_")
    assert created["expires_at"]
    assert mcp_tokens.get_status(4)["has_token"] is True
    assert mcp_tokens.get_status(99)["has_token"] is False
    assert mcp_tokens.resolve(token) == (4, "ok")
    assert mcp_tokens.resolve("not-a-token") == (None, "invalid")
    assert mcp_tokens.revoke(4) == 1
    assert mcp_tokens.get_status(4)["has_token"] is False
    assert mcp_tokens.resolve(token) == (None, "revoked")


def test_rotate_revokes_previous_token():
    first = mcp_tokens.create_or_rotate(4)
    second = mcp_tokens.create_or_rotate(4)
    assert first["token"] != second["token"]
    assert mcp_tokens.resolve(first["token"]) == (None, "revoked")
    assert mcp_tokens.resolve(second["token"]) == (4, "ok")
