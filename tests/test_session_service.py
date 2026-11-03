"""Session preparation owns checkpointer and initial-message selection."""

from __future__ import annotations

from src.agents.session_service import prepare_session


class _Checkpointer:
    def __init__(self, has_state: bool):
        self.has_state = has_state
        self.seen_config = None

    def get_tuple(self, config):
        self.seen_config = config
        return object() if self.has_state else None


def test_stateless_turn_seeds_history_without_checkpointer():
    session = prepare_session(
        thread_id="t1",
        persist_thread=False,
        recursion_limit=48,
        history=[("上一轮问题", "上一轮回答")],
        query="本轮问题",
    )

    assert session.checkpointer is None
    assert session.config == {"recursion_limit": 48}
    assert session.messages[-1] == {"role": "user", "content": "本轮问题"}
    assert any(item["content"] == "上一轮问题" for item in session.messages)


def test_checkpointed_thread_with_state_only_sends_current_query():
    checkpointer = _Checkpointer(has_state=True)
    session = prepare_session(
        thread_id="t1",
        persist_thread=True,
        recursion_limit=72,
        history=[("旧问题", "旧回答")],
        query="本轮问题",
        checkpointer_factory=lambda: checkpointer,
    )

    assert session.checkpointer is checkpointer
    assert session.config["configurable"]["thread_id"] == "t1"
    assert session.messages == [{"role": "user", "content": "本轮问题"}]


def test_new_checkpointed_thread_without_state_seeds_history():
    checkpointer = _Checkpointer(has_state=False)
    session = prepare_session(
        thread_id="t1",
        persist_thread=True,
        recursion_limit=72,
        history=[("旧问题", "旧回答")],
        query="本轮问题",
        checkpointer_factory=lambda: checkpointer,
    )

    assert session.checkpointer is checkpointer
    assert session.messages[-1] == {"role": "user", "content": "本轮问题"}
    assert any(item["content"] == "旧问题" for item in session.messages)


def test_blank_thread_id_stays_stateless_even_when_persist_requested():
    session = prepare_session(
        thread_id="",
        persist_thread=True,
        recursion_limit=48,
        history=[],
        query="问题",
        checkpointer_factory=lambda: _Checkpointer(True),
    )

    assert session.checkpointer is None
    assert session.messages == [{"role": "user", "content": "问题"}]
