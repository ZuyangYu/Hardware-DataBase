"""Redis 硬依赖契约：探针状态 + 启动门禁 + 就绪门禁。

``REDIS_REQUIRED=true``（默认）时 Redis 是硬依赖：未配置/不可达 → 拒绝启动、
``/health/ready`` 判 not_ready，不提供"降级成 DB 轮询"的运行形态。
``REDIS_REQUIRED=false``（仅测试隔离）时探针只作诊断，不参与就绪判定。
"""

from __future__ import annotations

import unittest
from unittest import mock

import src.settings
from src.observability import health


class RedisProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._url = src.settings.REDIS_URL
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._url)

    def test_empty_url_reports_not_configured(self):
        src.settings.REDIS_URL = ""
        self.assertEqual(health._check_redis()["status"], "not_configured")

    def test_reachable_redis_reports_up(self):
        src.settings.REDIS_URL = "redis://:secret@127.0.0.1:6380/0"
        client = mock.Mock()
        with mock.patch("redis.Redis.from_url", return_value=client) as from_url:
            result = health._check_redis()
        self.assertEqual(result["status"], "up")
        self.assertIn("latency_ms", result)
        client.ping.assert_called_once_with()
        client.close.assert_called_once_with()
        kwargs = from_url.call_args.kwargs
        self.assertEqual(
            kwargs["socket_timeout"], src.settings.OBS_DEPENDENCY_TIMEOUT_SECONDS
        )
        self.assertEqual(
            kwargs["socket_connect_timeout"], src.settings.OBS_DEPENDENCY_TIMEOUT_SECONDS
        )

    def test_unreachable_redis_is_degraded_not_down(self):
        src.settings.REDIS_URL = "redis://:secret@127.0.0.1:6380/0"
        with mock.patch("redis.Redis.from_url", side_effect=ConnectionError("refused")):
            result = health._check_redis()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error"], "ConnectionError")

    def test_ping_failure_closes_client_and_degrades(self):
        src.settings.REDIS_URL = "redis://:secret@127.0.0.1:6380/0"
        client = mock.Mock()
        client.ping.side_effect = TimeoutError("slow")
        with mock.patch("redis.Redis.from_url", return_value=client):
            result = health._check_redis()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error"], "TimeoutError")
        client.close.assert_called_once_with()


class DependencyAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._url = src.settings.REDIS_URL
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._url)
        patchers = (
            mock.patch.object(health, "list_workers", return_value=[{"worker_id": "test"}]),
            mock.patch.object(health, "_check_sqlite", return_value={"status": "up"}),
            mock.patch.object(health, "_check_storage", return_value={"status": "up"}),
            mock.patch.object(health, "check_control_plane", return_value={"status": "up"}),
            mock.patch.object(health, "_probe_http", return_value={"status": "up"}),
            # MCP 由独立开关控制，本套件不依赖本机 .env 是否启用该特性。
            mock.patch.object(health, "_check_mcp", return_value={"status": "not_configured"}),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_dependencies_include_redis_state(self):
        src.settings.REDIS_URL = ""
        result = health.check_dependencies()
        self.assertEqual(result["dependencies"]["redis"]["status"], "not_configured")
        self.assertEqual(result["status"], "up")

        src.settings.REDIS_URL = "redis://:secret@127.0.0.1:6380/0"
        with mock.patch("redis.Redis.from_url", side_effect=ConnectionError("refused")):
            result = health.check_dependencies()
        self.assertEqual(result["dependencies"]["redis"]["status"], "degraded")
        self.assertEqual(result["status"], "degraded")

    def test_readiness_requires_redis_when_required(self):
        self._old_required = src.settings.REDIS_REQUIRED
        self.addCleanup(setattr, src.settings, "REDIS_REQUIRED", self._old_required)

        src.settings.REDIS_URL = "redis://:secret@127.0.0.1:6380/0"
        src.settings.REDIS_REQUIRED = True
        with mock.patch("redis.Redis.from_url", side_effect=ConnectionError("refused")):
            ready = health.check_ready()
        self.assertEqual(ready["status"], "not_ready")
        self.assertEqual(ready["dependencies"]["redis"]["status"], "degraded")

        # 测试隔离形态：不参与就绪判定
        src.settings.REDIS_REQUIRED = False
        health.reset_readiness_cache()  # 就绪结果有 1 秒 TTL，换契约后要清
        with mock.patch("redis.Redis.from_url", side_effect=ConnectionError("refused")):
            ready = health.check_ready()
        self.assertEqual(ready["status"], "ready")
        self.assertNotIn("redis", ready["dependencies"])


class RedisHardRequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._url = src.settings.REDIS_URL
        self._required = src.settings.REDIS_REQUIRED
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._url)
        self.addCleanup(setattr, src.settings, "REDIS_REQUIRED", self._required)

    def test_missing_url_refuses_to_start(self):
        from src.core.redis_client import RedisUnavailable, assert_redis_ready

        src.settings.REDIS_REQUIRED = True
        src.settings.REDIS_URL = ""
        with self.assertRaises(RedisUnavailable) as ctx:
            assert_redis_ready()
        self.assertIn("REDIS_URL", str(ctx.exception))

    def test_unreachable_redis_refuses_to_start(self):
        from src.core.redis_client import RedisUnavailable, assert_redis_ready

        src.settings.REDIS_REQUIRED = True
        src.settings.REDIS_URL = "redis://:secret@127.0.0.1:6380/0"
        with mock.patch("redis.Redis.from_url", side_effect=ConnectionError("refused")):
            with self.assertRaises(RedisUnavailable):
                assert_redis_ready()

    def test_optional_mode_is_a_noop(self):
        from src.core.redis_client import assert_redis_ready

        src.settings.REDIS_REQUIRED = False
        src.settings.REDIS_URL = ""
        assert_redis_ready()  # 不抛异常


if __name__ == "__main__":
    unittest.main()
