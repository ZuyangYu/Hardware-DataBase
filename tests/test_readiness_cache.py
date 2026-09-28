"""``/health/ready`` 的短 TTL 缓存（Yuxi 同款探针优化）。

K8s / 负载均衡每隔几秒探一次就绪；无缓存时每次探针都会真查
SQLite + PostgreSQL + storage，探针风暴会把依赖负载放大 N 倍。
这里验证三件事：

- TTL 内的重复探针只做一次真实检查；
- TTL 过期后重新检查；
- 配置版本变化（切后端等）立即重探，不吃缓存。
"""

from __future__ import annotations

import time
import unittest

import src.settings as app_settings
from src.observability import health


class ReadinessCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_readiness_cache()
        self._old_ttl = app_settings.OBS_READINESS_CACHE_SECONDS
        self.addCleanup(
            setattr, app_settings, "OBS_READINESS_CACHE_SECONDS", self._old_ttl
        )
        self.addCleanup(health.reset_readiness_cache)
        self.calls = 0
        original = health.check_control_plane

        def counting(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)

        health.check_control_plane = counting
        self.addCleanup(setattr, health, "check_control_plane", original)

    def test_repeated_probes_within_ttl_hit_the_cache(self) -> None:
        app_settings.OBS_READINESS_CACHE_SECONDS = 60
        first = health.check_ready()
        second = health.check_ready()
        third = health.check_ready()

        self.assertEqual(self.calls, 1, "TTL 内应只做一次真实检查")
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertIn(first["status"], {"ready", "not_ready"})

    def test_probe_after_ttl_rechecks(self) -> None:
        app_settings.OBS_READINESS_CACHE_SECONDS = 0.05
        health.check_ready()
        time.sleep(0.08)
        health.check_ready()
        self.assertEqual(self.calls, 2)

    def test_config_revision_change_invalidates_cache(self) -> None:
        app_settings.OBS_READINESS_CACHE_SECONDS = 60
        health.check_ready()
        # 配置保存会更新 CONFIG_REVISION：缓存键变了就该重探。
        original_revision = app_settings.CONFIG_REVISION
        try:
            app_settings.CONFIG_REVISION = "changed-revision"
            health.check_ready()
        finally:
            app_settings.CONFIG_REVISION = original_revision
        self.assertEqual(self.calls, 2)

    def test_zero_ttl_disables_cache(self) -> None:
        app_settings.OBS_READINESS_CACHE_SECONDS = 0
        health.check_ready()
        health.check_ready()
        self.assertEqual(self.calls, 2)


if __name__ == "__main__":
    unittest.main()
