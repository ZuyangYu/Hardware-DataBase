"""A failed fork must finalize safely without ever invoking the agent."""

from unittest.mock import Mock

import src.settings as settings
from src.api.routes import query
from src.core.conversation import ConversationService
from tests._api_stub import make_auth


def test_failed_attempt_initialization_does_not_execute_canonical_thread(tmp_path, monkeypatch):
    path = str(tmp_path / "auth.db")
    monkeypatch.setattr(settings, "AUTH_DB_PATH", path)
    monkeypatch.setattr(settings, "CONVERSATION_SERIALIZE_SAME_THREAD", True)
    monkeypatch.setattr(settings, "RESOURCE_GOVERNOR_ENABLED", False)
    _, _, _, user = make_auth(path)
    conv = ConversationService(path)
    session = conv.create_session(user.id, "shared")
    turn = conv.create_turn(user_id=user.id, session_id=session.id, query="q")
    def fail(*args, **kwargs):
        raise OSError("simulated unavailable checkpoint store")
    monkeypatch.setattr("src.agents.checkpoints.prepare_attempt", fail)
    monkeypatch.setattr("src.agents.checkpoints.create_checkpointer", lambda: Mock())
    generator = Mock()
    monkeypatch.setattr(query, "_query_generator", generator)
    query._run_turn(turn_id=turn.id, user=user, ctx=Mock(), pipeline=Mock())
    generator.assert_not_called()
    final = conv.get_turn(user.id, turn.id)
    assert final.status == "failed"
    events = conv.list_turn_events(user.id, turn.id)
    assert [event.event_type for event in events][-2:] == ["error", "turn_end"]
    assert "检查点初始化失败" in final.error_message
    assert conv.get_checkpoint_refs(session.id)["committed"] is None
