"""Redis turn-event transport: publish/read/clear with replay-truth semantics.

Runs against the local Redis when reachable (``HDB_TEST_REDIS_URL`` or the
default ``redis://127.0.0.1:6379/15``); skipped otherwise.
"""

from __future__ import annotations

import os
import unittest
import uuid

import src.settings
from src.core import redis_client, redis_events

DEFAULT_TEST_URL = "redis://127.0.0.1:6379/15"
_TEST_URL = os.environ.get("HDB_TEST_REDIS_URL", DEFAULT_TEST_URL)


def _reachable(url: str) -> bool:
    try:
        import redis
    except ImportError:
        return False
    try:
        client = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0
        )
        client.ping()
        client.close()
        return True
    except Exception:
        return False


REACHABLE = _reachable(_TEST_URL)


class RedisEventsDisabledTests(unittest.TestCase):
    def setUp(self):
        self._original_url = src.settings.REDIS_URL
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._original_url)
        self.addCleanup(redis_client.reset_client)

    def test_empty_url_disables_transport(self):
        src.settings.REDIS_URL = ""
        redis_client.reset_client()
        self.assertFalse(redis_events.events_transport_enabled())
        self.assertFalse(redis_events.publish_turn_event("t1", 1, "delta", {"text": "x"}))
        events, cursor = redis_events.read_turn_events("t1")
        self.assertEqual(events, [])
        self.assertEqual(cursor, "0-0")

    def test_client_errors_are_fail_soft(self):
        src.settings.REDIS_URL = _TEST_URL
        redis_client.reset_client()

        class _Broken:
            def pipeline(self, *args, **kwargs):
                raise RuntimeError("down")

            def xrange(self, *args, **kwargs):
                raise RuntimeError("down")

            def delete(self, *args, **kwargs):
                raise RuntimeError("down")

        original = redis_events.get_redis_client
        redis_events.get_redis_client = lambda: _Broken()
        try:
            self.assertFalse(
                redis_events.publish_turn_event("t1", 1, "delta", {"text": "x"})
            )
            events, _ = redis_events.read_turn_events("t1")
            self.assertEqual(events, [])
            self.assertFalse(redis_events.clear_turn_events("t1"))
        finally:
            redis_events.get_redis_client = original


@unittest.skipUnless(REACHABLE, "local Redis is not reachable")
class RedisEventsTests(unittest.TestCase):
    def setUp(self):
        self._original_url = src.settings.REDIS_URL
        self._original_ttl = src.settings.TURN_EVENT_STREAM_TTL_SECONDS
        self._original_maxlen = src.settings.TURN_EVENT_STREAM_MAXLEN
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._original_url)
        self.addCleanup(
            setattr, src.settings, "TURN_EVENT_STREAM_TTL_SECONDS", self._original_ttl
        )
        self.addCleanup(
            setattr, src.settings, "TURN_EVENT_STREAM_MAXLEN", self._original_maxlen
        )
        src.settings.REDIS_URL = _TEST_URL
        src.settings.TURN_EVENT_STREAM_TTL_SECONDS = 120
        src.settings.TURN_EVENT_STREAM_MAXLEN = 5000
        redis_client.reset_client()
        self.addCleanup(redis_client.reset_client)
        self.turn_id = f"turn-{uuid.uuid4().hex[:10]}"
        self.addCleanup(redis_events.clear_turn_events, self.turn_id)

    def test_publish_read_clear_round_trip(self):
        self.assertTrue(
            redis_events.publish_turn_event(self.turn_id, 1, "delta", {"text": "a"})
        )
        self.assertTrue(
            redis_events.publish_turn_event(self.turn_id, 2, "done", {"answer": "b"})
        )
        events, cursor = redis_events.read_turn_events(self.turn_id)
        self.assertEqual([event["seq"] for event in events], [1, 2])
        self.assertEqual([event["event"] for event in events], ["delta", "done"])
        self.assertEqual(events[1]["payload"], {"answer": "b"})
        self.assertNotEqual(cursor, "0-0")

        more, cursor2 = redis_events.read_turn_events(self.turn_id, after_id=cursor)
        self.assertEqual(more, [])
        self.assertEqual(cursor2, cursor)

        client = redis_client.get_redis_client()
        ttl = client.ttl(redis_events.stream_key(self.turn_id))
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 120)

        self.assertTrue(redis_events.clear_turn_events(self.turn_id))
        events, _ = redis_events.read_turn_events(self.turn_id)
        self.assertEqual(events, [])

    def test_turns_are_isolated(self):
        self.assertTrue(
            redis_events.publish_turn_event(self.turn_id, 1, "delta", {"text": "a"})
        )
        other, _ = redis_events.read_turn_events("turn-other")
        self.assertEqual(other, [])


if __name__ == "__main__":
    unittest.main()
