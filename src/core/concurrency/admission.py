"""Admission control: per-user/per-tenant RPM and queue-depth backpressure.

Applied before a request is accepted.  Once a task is persisted, backpressure
is reported through task state instead of HTTP status changes (P0b).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from .rate_limiter import RateLimiter

_MAX_BUCKET_KEYS = 10_000


class AdmissionRejected(RuntimeError):
    def __init__(self, reason: str, *, retry_after: float = 1.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = max(0.1, float(retry_after))


class AdmissionController:
    def __init__(
        self,
        *,
        enabled: bool = True,
        chat_rpm_per_user: float = 30.0,
        chat_rpm_per_tenant: float = 120.0,
        heavy_rpm_per_user: float = 10.0,
        heavy_rpm_per_tenant: float = 40.0,
        queue_depth_limit: int = 256,
        queue_depth_provider=None,
        retry_after_seconds: float = 5.0,
        kind_limits: dict[str, tuple[float, float]] | None = None,
        model_rpm_per_user: float = 0.0,
        model_rpm_per_tenant: float = 0.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.queue_depth_limit = max(1, int(queue_depth_limit))
        self.queue_depth_provider = queue_depth_provider
        self.retry_after_seconds = max(0.1, float(retry_after_seconds))
        # Resource-type buckets (plan §4.5).  ``heavy`` stays a compatibility
        # alias for document/export/ocr-style work.
        self._limits: dict[str, tuple[float, float]] = {
            "chat": (max(0.001, float(chat_rpm_per_user)), max(0.001, float(chat_rpm_per_tenant))),
            "heavy": (max(0.001, float(heavy_rpm_per_user)), max(0.001, float(heavy_rpm_per_tenant))),
        }
        for kind, (user_rate, tenant_rate) in (kind_limits or {}).items():
            self._limits[str(kind)] = (
                max(0.001, float(user_rate)),
                max(0.001, float(tenant_rate)),
            )
        self._model_limits = (
            max(0.001, float(model_rpm_per_user)),
            max(0.001, float(model_rpm_per_tenant)),
        )
        self._model_limited = float(model_rpm_per_user) > 0 or float(model_rpm_per_tenant) > 0
        self._limiter = RateLimiter()
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, float] = OrderedDict()

    # -- public API ---------------------------------------------------------

    def check(
        self,
        *,
        user_id: int | str,
        tenant_id: int | str | None = None,
        resource: str = "chat",
        model: str = "",
    ) -> None:
        if not self.enabled:
            return
        kind = self._normalize_resource(resource)
        user_rate, tenant_rate = self._limits.get(
            kind, self._limits.get("chat", (30.0, 120.0))
        )
        now = time.monotonic()
        self._consume_bucket(f"user:{kind}:{user_id}", user_rate, now, reason="rate_limit_user")
        if tenant_id not in (None, ""):
            self._consume_bucket(
                f"tenant:{kind}:{tenant_id}", tenant_rate, now, reason="rate_limit_tenant"
            )
        model_key = str(model or "").strip()
        if model_key and self._model_limited:
            model_user_rate, model_tenant_rate = self._model_limits
            self._consume_bucket(
                f"model-user:{model_key}:{user_id}", model_user_rate, now, reason="rate_limit_model"
            )
            if tenant_id not in (None, ""):
                self._consume_bucket(
                    f"model-tenant:{model_key}:{tenant_id}",
                    model_tenant_rate,
                    now,
                    reason="rate_limit_model",
                )
        if kind in {"chat", "deep"} and self.queue_depth_provider is not None:
            try:
                depth = int(self.queue_depth_provider())
            except Exception:
                depth = 0
            if depth >= self.queue_depth_limit:
                raise AdmissionRejected("queue_full", retry_after=self.retry_after_seconds)

    @staticmethod
    def _normalize_resource(resource: str) -> str:
        kind = str(resource or "chat").strip().lower()
        if kind == "all":
            kind = "chat"
        return kind

    def _consume_bucket(self, key: str, rate: float, now: float, *, reason: str) -> None:
        self._touch(key, now, rate)
        if not self._limiter.wait(key, tokens=1.0, timeout=0.0):
            raise AdmissionRejected(reason, retry_after=self.retry_after_seconds)

    # -- internals ----------------------------------------------------------

    def _touch(self, key: str, now: float, rate: float) -> None:
        """Configure a bucket on first sight and prune the key cache."""
        with self._lock:
            bucket = self._limiter._buckets.get(key)  # noqa: SLF001 - same package
            if bucket is None:
                # Small bursts are legitimate (page-level fan-out); the refill
                # rate still enforces the sustained per-minute quota.
                burst = max(1.0, min(float(rate), 10.0))
                self._limiter.configure(key, rate_per_second=max(0.001, float(rate) / 60.0), burst=burst)
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > _MAX_BUCKET_KEYS:
                stale_key, _ = self._seen.popitem(last=False)
                self._limiter._buckets.pop(stale_key, None)  # noqa: SLF001


_CONTROLLER: AdmissionController | None = None
_CONTROLLER_LOCK = threading.Lock()


def _queue_depth_provider():
    from src.core.conversation import ConversationService

    return ConversationService().pending_turn_queue_state()[0]


def _optional_rate(value, fallback: float) -> float:
    text = str(value or "").strip()
    if not text:
        return float(fallback)
    try:
        return float(text)
    except ValueError:
        return float(fallback)


def get_admission_controller() -> AdmissionController:
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        if _CONTROLLER is None:
            import src.settings as settings

            chat_user = float(getattr(settings, "ADMISSION_CHAT_RPM_PER_USER", 30))
            chat_tenant = float(getattr(settings, "ADMISSION_CHAT_RPM_PER_TENANT", 120))
            heavy_user = float(getattr(settings, "ADMISSION_HEAVY_RPM_PER_USER", 10))
            heavy_tenant = float(getattr(settings, "ADMISSION_HEAVY_RPM_PER_TENANT", 40))
            kind_limits = {
                "deep": (
                    _optional_rate(getattr(settings, "ADMISSION_DEEP_RPM_PER_USER", ""), chat_user),
                    _optional_rate(getattr(settings, "ADMISSION_DEEP_RPM_PER_TENANT", ""), chat_tenant),
                ),
                "document": (
                    _optional_rate(getattr(settings, "ADMISSION_DOCUMENT_RPM_PER_USER", ""), heavy_user),
                    _optional_rate(getattr(settings, "ADMISSION_DOCUMENT_RPM_PER_TENANT", ""), heavy_tenant),
                ),
                "export": (
                    _optional_rate(getattr(settings, "ADMISSION_EXPORT_RPM_PER_USER", ""), heavy_user),
                    _optional_rate(getattr(settings, "ADMISSION_EXPORT_RPM_PER_TENANT", ""), heavy_tenant),
                ),
                "ocr": (
                    _optional_rate(getattr(settings, "ADMISSION_OCR_RPM_PER_USER", ""), heavy_user),
                    _optional_rate(getattr(settings, "ADMISSION_OCR_RPM_PER_TENANT", ""), heavy_tenant),
                ),
                "evaluation": (
                    _optional_rate(getattr(settings, "ADMISSION_EVALUATION_RPM_PER_USER", ""), heavy_user),
                    _optional_rate(getattr(settings, "ADMISSION_EVALUATION_RPM_PER_TENANT", ""), heavy_tenant),
                ),
            }
            _CONTROLLER = AdmissionController(
                enabled=bool(getattr(settings, "ADMISSION_CONTROL_ENABLED", True)),
                chat_rpm_per_user=chat_user,
                chat_rpm_per_tenant=chat_tenant,
                heavy_rpm_per_user=heavy_user,
                heavy_rpm_per_tenant=heavy_tenant,
                queue_depth_limit=int(getattr(settings, "RESOURCE_QUEUE_MAX_DEPTH", 256)),
                queue_depth_provider=_queue_depth_provider,
                retry_after_seconds=float(getattr(settings, "ADMISSION_RETRY_AFTER_SECONDS", 5)),
                kind_limits=kind_limits,
                model_rpm_per_user=_optional_rate(
                    getattr(settings, "ADMISSION_MODEL_RPM_PER_USER", ""), 0.0
                ),
                model_rpm_per_tenant=_optional_rate(
                    getattr(settings, "ADMISSION_MODEL_RPM_PER_TENANT", ""), 0.0
                ),
            )
        return _CONTROLLER


def reset_admission_controller() -> None:
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        _CONTROLLER = None
