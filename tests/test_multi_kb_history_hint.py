"""Runner reads only the previous completed turn's server target summary."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.runner import MultiSourceAgentRunner


class FakeConversationService:
    def list_recent_turns(self, user_id, session_id, limit=5):
        assert (user_id, session_id, limit) == (2, 11, 5)
        return [
            SimpleNamespace(id="current", status="streaming", summary={}),
            SimpleNamespace(id="previous", status="completed", summary={
                "retrieval_targets": {"targets": [{"kb_id": 5, "record_id": 101}]},
            }),
        ]


def test_runner_history_hint_skips_current_turn(monkeypatch):
    monkeypatch.setattr("src.core.conversation.ConversationService", FakeConversationService)
    runner = object.__new__(MultiSourceAgentRunner)
    hint = runner._history_target_hint(
        action_scope={"turn_id": "current"}, user_id=2, session_id=11,
    )
    assert hint == [{"kb_id": 5, "record_id": 101}]


def test_runner_history_hint_fails_closed_without_ids(monkeypatch):
    runner = object.__new__(MultiSourceAgentRunner)
    assert runner._history_target_hint(action_scope={}, user_id=None, session_id=11) == []
