"""Distributed admission: shared Redis quota across API replicas (v3 §14).

Runs against the local Redis when reachable (``HDB_TEST_REDIS_URL`` or the
default ``redis://127.0.0.1:6379/15``); skipped otherwise.
"""

from __future__ import annotations

import os
import unittest
import uuid

import src.settings
from src.core import redis_client
from src.core.concurrency import distributed_limiter
from src.core.concurrency.admission import AdmissionController, AdmissionRejected

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


class DistributedAdmissionLocalTests(unittest.TestCase):
    def test_queue_state_failure_fails_closed(self):
        def _broken():
            raise RuntimeError("db down")

        controller = AdmissionController(
            enabled=True,
            chat_rpm_per_user=600,
            chat_rpm_per_tenant=600,
            queue_depth_limit=10,
            queue_depth_provider=_broken,
        )
        with self.assertRaises(AdmissionRejected) as ctx:
            controller.check(user_id="queue-user", resource="chat")
        self.assertEqual(ctx.exception.reason, "queue_state_unavailable")

    def test_local_only_controller_does_not_touch_redis(self):
        controller = AdmissionController(
            enabled=True, chat_rpm_per_user=600, chat_rpm_per_tenant=600
        )
        controller.check(user_id="local-user", resource="chat")


@unittest.skipUnless(REACHABLE, "local Redis is not reachable")
class DistributedAdmissionRedisTests(unittest.TestCase):
    def setUp(self):
        self._original_url = src.settings.REDIS_URL
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._original_url)
        src.settings.REDIS_URL = _TEST_URL
        redis_client.reset_client()
        self.addCleanup(redis_client.reset_client)

    def _controller(self, **overrides) -> AdmissionController:
        defaults = dict(
            enabled=True,
            chat_rpm_per_user=30,
            chat_rpm_per_tenant=120,
            queue_depth_limit=10_000,
            queue_depth_provider=lambda: 0,
            retry_after_seconds=1,
            distributed=True,
            distributed_ttl_seconds=30,
        )
        defaults.update(overrides)
        return AdmissionController(**defaults)

    def test_limiter_burst_then_refill(self):
        key = f"hdb:quota:test:{uuid.uuid4().hex[:8]}"
        self.assertIs(distributed_limiter.allow(key, rate_per_minute=60, burst=3), True)
        self.assertIs(distributed_limiter.allow(key, rate_per_minute=60, burst=3), True)
        self.assertIs(distributed_limiter.allow(key, rate_per_minute=60, burst=3), True)
        self.assertIs(distributed_limiter.allow(key, rate_per_minute=60, burst=3), False)
        redis_client.get_redis_client().delete(key)
        self.assertIs(distributed_limiter.allow(key, rate_per_minute=60, burst=3), True)

    def test_two_replicas_share_one_user_quota(self):
        user = f"shared-{uuid.uuid4().hex[:8]}"
        replica_a = self._controller()
        replica_b = self._controller()
        allowed = 0
        for index in range(20):
            controller = replica_a if index % 2 == 0 else replica_b
            try:
                controller.check(user_id=user, tenant_id=f"t-{user}", resource="chat")
                allowed += 1
            except AdmissionRejected as exc:
                self.assertEqual(exc.reason, "rate_limit_user")
                break
        # L2 burst is min(rate, 10) = 10 shared tokens across both replicas.
        self.assertEqual(allowed, 10)

    def test_tenant_quota_is_shared_across_users(self):
        tenant = f"tenant-{uuid.uuid4().hex[:8]}"
        controller = self._controller(chat_rpm_per_user=600, chat_rpm_per_tenant=4)
        controller.check(user_id="user-a", tenant_id=tenant, resource="chat")
        controller.check(user_id="user-b", tenant_id=tenant, resource="chat")
        controller.check(user_id="user-c", tenant_id=tenant, resource="chat")
        controller.check(user_id="user-d", tenant_id=tenant, resource="chat")
        with self.assertRaises(AdmissionRejected) as ctx:
            controller.check(user_id="user-e", tenant_id=tenant, resource="chat")
        self.assertEqual(ctx.exception.reason, "rate_limit_tenant")

    def test_redis_unavailable_fails_closed_when_enabled(self):
        controller = self._controller()
        original = distributed_limiter.allow
        distributed_limiter.allow = lambda *args, **kwargs: None
        try:
            with self.assertRaises(AdmissionRejected) as ctx:
                controller.check(user_id="no-redis", resource="chat")
            self.assertEqual(ctx.exception.reason, "admission_unavailable")
        finally:
            distributed_limiter.allow = original

        tolerant = self._controller(distributed=False)
        distributed_limiter.allow = lambda *args, **kwargs: None
        try:
            tolerant.check(user_id="no-redis-local", resource="chat")
        finally:
            distributed_limiter.allow = original


if __name__ == "__main__":
    unittest.main()
