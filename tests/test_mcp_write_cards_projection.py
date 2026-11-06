"""Persisted MCP write cards stay attached to their assistant turn."""

from __future__ import annotations

from types import SimpleNamespace

from src.api.routes.conversations import _mcp_write_cards_from_events


def test_write_cards_merge_latest_proposal_state_and_turn_id():
    events = [
        SimpleNamespace(
            event_type="mcp_write_proposal",
            payload={
                "card": {
                    "kind": "mcp_write_proposal",
                    "proposal_id": "p-1",
                    "server": "drawio",
                    "server_name": "draw.io 图表",
                    "tool": "create_drawio_file",
                    "artifact_hint": "content,filename",
                    "arguments_digest": "abc123",
                    "status": "proposed",
                    "expires_at": "2026-11-05T12:00:00Z",
                }
            },
        )
    ]
    cards = _mcp_write_cards_from_events(
        events,
        {
            "p-1": {
                "proposal_id": "p-1",
                "status": "completed",
                "artifact_ref": "4/p-1/optimized.drawio",
                "artifact_filename": "optimized.drawio",
                "artifact_sha256": "f" * 64,
                "error_code": "",
                "expires_at": "2026-11-05T12:00:00Z",
                "updated_at": "2026-11-05T10:00:00Z",
            }
        },
        turn_id="turn-1",
    )

    assert len(cards) == 1
    card = cards[0]
    assert card["proposal_id"] == "p-1"
    assert card["turn_id"] == "turn-1"
    assert card["status"] == "completed"
    assert card["artifact_filename"] == "optimized.drawio"
    assert card["artifact_sha256"] == "f" * 64


def test_write_cards_ignore_unrelated_turn_events():
    events = [
        SimpleNamespace(event_type="tool_started", payload={"tool_name": "x"}),
        SimpleNamespace(event_type="mcp_write_proposal", payload={"card": {}}),
    ]
    assert _mcp_write_cards_from_events(events, {}, turn_id="turn-2") == []
