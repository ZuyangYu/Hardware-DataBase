"""Cancel fast path on Redis: publish/read/clear with fail-soft semantics.

Runs against the local Redis when reachable (``REDIS_URL`` or the default
``redis://127.0.0.1:6379/15``); skipped otherwise.
"""

from __future__ import annotations

import os
import unittest
import uuid

import src.settings
from src.core import redis_cancel

DEFAULT_TEST_URL = "redis://127.0.0.1:6379/15"


def _redis_reachable(url: str) -> bool:
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


_TEST_URL = os.environ.get("HDB_TEST_REDIS_URL", DEFAULT_TEST_URL)
REACHABLE = _redis_reachable(_TEST_URL)


class CancelFastPathDisabledTests(unittest.TestCase):
    def setUp(self):
        self._original_url = src.settings.REDIS_URL
        self._original_enabled = src.settings.CANCEL_FAST_PATH_ENABLED
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._original_url)
        self.addCleanup(
            setattr, src.settings, "CANCEL_FAST_PATH_ENABLED", self._original_enabled
        )
        self.addCleanup(redis_cancel.reset_client)

    def test_empty_url_disables_every_helper(self):
        src.settings.REDIS_URL = ""
        src.settings.CANCEL_FAST_PATH_ENABLED = True
        redis_cancel.reset_client()
        self.assertFalse(redis_cancel.fast_path_enabled())
        self.assertFalse(redis_cancel.publish_cancel("turn-1"))
        self.assertFalse(redis_cancel.is_cancel_requested("turn-1"))
        self.assertFalse(redis_cancel.clear_cancel("turn-1"))

    def test_flag_disables_every_helper(self):
        src.settings.REDIS_URL = _TEST_URL
        src.settings.CANCEL_FAST_PATH_ENABLED = False
        redis_cancel.reset_client()
        self.assertFalse(redis_cancel.fast_path_enabled())
        self.assertFalse(redis_cancel.publish_cancel("turn-1"))

    def test_client_errors_are_fail_soft(self):
        src.settings.REDIS_URL = _TEST_URL
        src.settings.CANCEL_FAST_PATH_ENABLED = True
        redis_cancel.reset_client()

        class _Broken:
            def set(self, *args, **kwargs):
                raise RuntimeError("down")

            def exists(self, *args, **kwargs):
                raise RuntimeError("down")

            def delete(self, *args, **kwargs):
                raise RuntimeError("down")

        original = redis_cancel.get_redis_client
        redis_cancel.get_redis_client = lambda: _Broken()
        try:
            self.assertFalse(redis_cancel.publish_cancel("turn-1"))
            self.assertFalse(redis_cancel.is_cancel_requested("turn-1"))
            self.assertFalse(redis_cancel.clear_cancel("turn-1"))
        finally:
            redis_cancel.get_redis_client = original


@unittest.skipUnless(REACHABLE, "local Redis is not reachable")
class CancelFastPathTests(unittest.TestCase):
    def setUp(self):
        self._original_url = src.settings.REDIS_URL
        self._original_enabled = src.settings.CANCEL_FAST_PATH_ENABLED
        self._original_ttl = src.settings.CANCEL_KEY_TTL_SECONDS
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._original_url)
        self.addCleanup(
            setattr, src.settings, "CANCEL_FAST_PATH_ENABLED", self._original_enabled
        )
        self.addCleanup(
            setattr, src.settings, "CANCEL_KEY_TTL_SECONDS", self._original_ttl
        )
        src.settings.REDIS_URL = _TEST_URL
        src.settings.CANCEL_FAST_PATH_ENABLED = True
        src.settings.CANCEL_KEY_TTL_SECONDS = 120
        redis_cancel.reset_client()
        self.addCleanup(redis_cancel.reset_client)
        self.turn_id = f"turn-{uuid.uuid4().hex[:10]}"
        self.addCleanup(redis_cancel.clear_cancel, self.turn_id)

    def test_publish_read_clear_round_trip_with_ttl(self):
        self.assertFalse(redis_cancel.is_cancel_requested(self.turn_id))
        self.assertTrue(redis_cancel.publish_cancel(self.turn_id))
        self.assertTrue(redis_cancel.is_cancel_requested(self.turn_id))
        client = redis_cancel.get_redis_client()
        ttl = client.ttl(redis_cancel._key(self.turn_id))
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 120)
        self.assertTrue(redis_cancel.clear_cancel(self.turn_id))
        self.assertFalse(redis_cancel.is_cancel_requested(self.turn_id))

    def test_other_turns_are_not_affected(self):
        self.assertTrue(redis_cancel.publish_cancel(self.turn_id))
        self.assertFalse(redis_cancel.is_cancel_requested("turn-other"))


if __name__ == "__main__":
    unittest.main()
