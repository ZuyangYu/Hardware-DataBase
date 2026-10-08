"""SSE audit replay must retain IDs, multi-line JSON and terminal events."""

import pytest

from src.evaluation.http_events import iter_sse_events


def test_replay_handles_comments_multiline_payload_and_eof():
    lines = [b": keepalive", b"", b"id: 2", b"event: delta", b'data: {"text":',
             'data: "中文"}', "", "id: 3", "event: turn_end", 'data: {"status":"completed"}']
    assert list(iter_sse_events(lines)) == [
        {"seq": 2, "event": "delta", "payload": {"text": "中文"}},
        {"seq": 3, "event": "turn_end", "payload": {"status": "completed"}},
    ]


def test_invalid_event_payload_fails_instead_of_hiding_audit_corruption():
    with pytest.raises(ValueError):
        list(iter_sse_events(["id: 1", "event: tool_result", "data: not-json", ""]))
