"""Environment-backed settings for the External capability surface.

Kept outside ``src.settings`` so the large legacy settings module does not grow
with every independently governed external capability.
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default).lower()).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


HDB_API_KEY_PEPPER = os.getenv("HDB_API_KEY_PEPPER", "")
HDB_API_KEY_MIN_TTL_DAYS = max(1, min(int(os.getenv("HDB_API_KEY_MIN_TTL_DAYS", "1")), 365))
# Keep the browser and API contract bounded even if an operator accidentally
# supplies an excessively large environment value.  A Key is a long-lived
# credential, so "no practical expiry" is not an acceptable default.
HDB_API_KEY_MAX_TTL_DAYS = max(
    HDB_API_KEY_MIN_TTL_DAYS,
    min(int(os.getenv("HDB_API_KEY_MAX_TTL_DAYS", "365")), 365),
)
HDB_API_KEY_DEFAULT_TTL_DAYS = min(
    max(int(os.getenv("HDB_API_KEY_DEFAULT_TTL_DAYS", "90")), HDB_API_KEY_MIN_TTL_DAYS),
    HDB_API_KEY_MAX_TTL_DAYS,
)
HDB_API_KEY_MAX_ACTIVE_PER_USER = int(os.getenv("HDB_API_KEY_MAX_ACTIVE_PER_USER", "10"))
HDB_EXTERNAL_ALL_KBS_MODE_ENABLED = _env_bool("HDB_EXTERNAL_ALL_KBS_MODE_ENABLED", False)
HDB_EXTERNAL_STRUCTURED_ENABLED = _env_bool("HDB_EXTERNAL_STRUCTURED_ENABLED", False)
HDB_EXTERNAL_RATE_LIMIT_BACKEND = os.getenv("HDB_EXTERNAL_RATE_LIMIT_BACKEND", "redis").strip().lower()
if HDB_EXTERNAL_RATE_LIMIT_BACKEND not in {"redis", "local"}:
    HDB_EXTERNAL_RATE_LIMIT_BACKEND = "redis"
HDB_EXTERNAL_RATE_LIMIT_PER_MINUTE = int(os.getenv("HDB_EXTERNAL_RATE_LIMIT_PER_MINUTE", "120"))
HDB_EXTERNAL_DAILY_QUOTA = int(os.getenv("HDB_EXTERNAL_DAILY_QUOTA", "10000"))
HDB_EXTERNAL_CONCURRENCY_LIMIT = int(os.getenv("HDB_EXTERNAL_CONCURRENCY_LIMIT", "8"))
HDB_EXTERNAL_TIMEOUT_SECONDS = float(os.getenv("HDB_EXTERNAL_TIMEOUT_SECONDS", "30"))
HDB_EXTERNAL_MAX_TOP_K = int(os.getenv("HDB_EXTERNAL_MAX_TOP_K", "20"))
HDB_EXTERNAL_MAX_FILE_REFS = int(os.getenv("HDB_EXTERNAL_MAX_FILE_REFS", "50"))
HDB_EXTERNAL_MAX_BATCH_ITEMS = int(os.getenv("HDB_EXTERNAL_MAX_BATCH_ITEMS", "20"))
HDB_EXTERNAL_MAX_RESPONSE_BYTES = int(
    os.getenv("HDB_EXTERNAL_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024))
)
HDB_EXTERNAL_MAX_REQUEST_BYTES = int(
    os.getenv("HDB_EXTERNAL_MAX_REQUEST_BYTES", str(1024 * 1024))
)
HDB_EXTERNAL_MAX_DOWNLOAD_BYTES = int(
    os.getenv("HDB_EXTERNAL_MAX_DOWNLOAD_BYTES", str(50 * 1024 * 1024))
)
HDB_EXTERNAL_MAX_UPLOAD_BYTES = int(
    os.getenv("HDB_EXTERNAL_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
)
HDB_EXTERNAL_MAX_UPLOAD_FILES = int(os.getenv("HDB_EXTERNAL_MAX_UPLOAD_FILES", "20"))
HDB_EXTERNAL_UPLOAD_IDEMPOTENCY_TTL_SECONDS = int(
    os.getenv("HDB_EXTERNAL_UPLOAD_IDEMPOTENCY_TTL_SECONDS", "3600")
)
HDB_EXTERNAL_SOURCE_SCOPE_TTL_SECONDS = int(
    os.getenv("HDB_EXTERNAL_SOURCE_SCOPE_TTL_SECONDS", "3600")
)
HDB_EXTERNAL_EVIDENCE_TTL_SECONDS = int(
    os.getenv("HDB_EXTERNAL_EVIDENCE_TTL_SECONDS", "3600")
)
HDB_EXTERNAL_CHUNK_PAGE_SIZE = int(os.getenv("HDB_EXTERNAL_CHUNK_PAGE_SIZE", "50"))
HDB_EXTERNAL_CHUNK_MAX_BYTES = int(
    os.getenv("HDB_EXTERNAL_CHUNK_MAX_BYTES", str(2 * 1024 * 1024))
)


def validate_external_settings() -> None:
    """Reject configurations that are unsafe in production.

    The service is fail-closed: a production deployment must not silently run
    with the POC all-KB mode or a single-process rate limiter.  The pepper is
    optional hardening; when absent, keys still use a high-entropy secret and
    a SHA-256 digest.
    """
    environment = os.getenv("OBS_ENVIRONMENT", "development").strip().lower()
    production = environment in {"prod", "production"}
    if production and HDB_EXTERNAL_ALL_KBS_MODE_ENABLED:
        raise RuntimeError(
            "生产环境禁止 HDB_EXTERNAL_ALL_KBS_MODE_ENABLED=true；"
            "外部 Key 必须使用 selected_kbs allowlist"
        )
    if production and HDB_EXTERNAL_RATE_LIMIT_BACKEND == "local":
        raise RuntimeError(
            "生产环境禁止 HDB_EXTERNAL_RATE_LIMIT_BACKEND=local；"
            "多实例限流必须使用 Redis 或集中式网关"
        )
    redis_required = os.getenv("REDIS_REQUIRED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if production and not redis_required:
        raise RuntimeError(
            "生产环境必须设置 REDIS_REQUIRED=true；禁止回落到单进程限流"
        )
