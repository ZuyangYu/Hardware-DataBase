from __future__ import annotations

import sqlite3

import pytest

import src.settings as settings
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
    status = mcp_tokens.get_status(4)
    assert status["has_token"] is True
    assert status["token"] == token
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


def test_plaintext_token_is_recoverable_after_reload():
    created = mcp_tokens.create_or_rotate(7)
    assert mcp_tokens.get_status(7)["token"] == created["token"]

    with sqlite3.connect(mcp_tokens._db_path()) as conn:
        stored = conn.execute(
            "SELECT token_plain FROM mcp_tokens WHERE id = ?",
            (created["token_id"],),
        ).fetchone()[0]
    assert stored
    if settings.MCP_CREDENTIALS_KEY:
        assert stored.startswith("enc:v1:")
        assert created["token"] not in stored
    else:
        assert stored == created["token"]


def test_legacy_row_without_plaintext_returns_empty_token():
    created = mcp_tokens.create_or_rotate(8)
    with sqlite3.connect(mcp_tokens._db_path()) as conn:
        conn.execute(
            "UPDATE mcp_tokens SET token_plain = NULL WHERE id = ?",
            (created["token_id"],),
        )
        conn.commit()
    status = mcp_tokens.get_status(8)
    assert status["has_token"] is True
    assert status["token"] == ""
