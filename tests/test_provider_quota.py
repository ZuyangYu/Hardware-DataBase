"""Provider RPM quota counted at the model boundary (v3 §9.3).

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
from src.core.concurrency.provider_limiter import allow_provider_call
from src.core.llm_governor import ProviderQuotaExceeded, get_llm_governor

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


class ProviderQuotaDisabledTests(unittest.TestCase):
    def setUp(self):
        self._original_limit = src.settings.PROVIDER_RPM_LIMIT
        self._original_url = src.settings.REDIS_URL
        self.addCleanup(setattr, src.settings, "PROVIDER_RPM_LIMIT", self._original_limit)
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._original_url)
        self.addCleanup(redis_client.reset_client)

    def test_disabled_limit_never_touches_redis(self):
        src.settings.PROVIDER_RPM_LIMIT = 0
        src.settings.PROVIDER_TPM_LIMIT = 0
        src.settings.REDIS_URL = ""
        redis_client.reset_client()
        self.assertIs(allow_provider_call("p", "m"), True)

    def test_disabled_tpm_limit_never_touches_redis(self):
        from src.core.concurrency.provider_limiter import allow_provider_tokens

        src.settings.PROVIDER_TPM_LIMIT = 0
        src.settings.REDIS_URL = ""
        redis_client.reset_client()
        self.assertIs(allow_provider_tokens("p", "m", 10), True)


@unittest.skipUnless(REACHABLE, "local Redis is not reachable")
class ProviderQuotaRedisTests(unittest.TestCase):
    def setUp(self):
        self._original_limit = src.settings.PROVIDER_RPM_LIMIT
        self._original_url = src.settings.REDIS_URL
        self.addCleanup(setattr, src.settings, "PROVIDER_RPM_LIMIT", self._original_limit)
        self.addCleanup(setattr, src.settings, "REDIS_URL", self._original_url)
        src.settings.REDIS_URL = _TEST_URL
        src.settings.PROVIDER_RPM_LIMIT = 0
        redis_client.reset_client()
        self.addCleanup(redis_client.reset_client)
        self.provider = f"prov-{uuid.uuid4().hex[:6]}"
        self.model = f"model-{uuid.uuid4().hex[:6]}"

    def _key(self) -> str:
        return f"hdb:quota:provider:{self.provider}:{self.model}"

    def test_limit_allows_then_denies_per_provider_model(self):
        src.settings.PROVIDER_RPM_LIMIT = 2
        self.assertIs(allow_provider_call(self.provider, self.model), True)
        self.assertIs(allow_provider_call(self.provider, self.model), True)
        self.assertIs(allow_provider_call(self.provider, self.model), False)
        # A different model has its own bucket.
        self.assertIs(
            allow_provider_call(self.provider, f"{self.model}-other"), True
        )

    def test_unavailable_limiter_returns_none(self):
        src.settings.PROVIDER_RPM_LIMIT = 2
        original = distributed_limiter.allow
        distributed_limiter.allow = lambda *args, **kwargs: None
        try:
            self.assertIsNone(allow_provider_call(self.provider, self.model))
        finally:
            distributed_limiter.allow = original

    def test_tpm_limit_consumes_requested_tokens(self):
        from src.core.concurrency.provider_limiter import allow_provider_tokens

        src.settings.PROVIDER_TPM_LIMIT = 10
        self.assertIs(allow_provider_tokens(self.provider, self.model, 6), True)
        self.assertIs(allow_provider_tokens(self.provider, self.model, 5), False)
        self.assertIs(allow_provider_tokens(self.provider, f"{self.model}-other", 10), True)

    def test_tpm_unavailable_returns_none(self):
        from src.core.concurrency.provider_limiter import allow_provider_tokens

        src.settings.PROVIDER_TPM_LIMIT = 10
        original = distributed_limiter.allow_tokens
        distributed_limiter.allow_tokens = lambda *args, **kwargs: None
        try:
            self.assertIsNone(allow_provider_tokens(self.provider, self.model, 1))
        finally:
            distributed_limiter.allow_tokens = original

    def test_governor_fails_closed_on_provider_quota(self):
        src.settings.PROVIDER_RPM_LIMIT = 1
        governor = get_llm_governor()
        with governor.slot(provider=self.provider, model=self.model):
            pass
        with self.assertRaises(ProviderQuotaExceeded):
            with governor.slot(provider=self.provider, model=self.model):
                pass  # pragma: no cover - must raise before the body
        # The failed attempt released its local slot.
        snapshot = governor.snapshot()
        self.assertEqual(snapshot.get("active", 0), 0)
        redis_client.get_redis_client().delete(self._key())
        with governor.slot(provider=self.provider, model=self.model):
            pass

    def test_governor_rejects_when_limiter_unavailable(self):
        src.settings.PROVIDER_RPM_LIMIT = 2
        original = distributed_limiter.allow
        distributed_limiter.allow = lambda *args, **kwargs: None
        try:
            governor = get_llm_governor()
            with self.assertRaises(ProviderQuotaExceeded):
                with governor.slot(provider=self.provider, model=self.model):
                    pass  # pragma: no cover - must raise
        finally:
            distributed_limiter.allow = original


if __name__ == "__main__":
    unittest.main()
