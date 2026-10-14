"""Small dependency checks for probes and the admin System Status page."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

import requests

import src.settings as settings

from .worker_registry import list_workers

from src.storage.control_plane.health import check_control_plane


def _check_sqlite() -> dict[str, Any]:
    started = time.monotonic()
    path = settings.AUTH_DB_PATH
    try:
        with sqlite3.connect(path, timeout=settings.OBS_DEPENDENCY_TIMEOUT_SECONDS) as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "up", "latency_ms": round((time.monotonic() - started) * 1000, 2)}
    except Exception as exc:
        return {"status": "down", "error": type(exc).__name__}


def _check_storage() -> dict[str, Any]:
    path = settings.STORAGE_DIR
    if not os.path.isdir(path):
        return {"status": "down", "error": "storage directory missing"}
    if not os.access(path, os.R_OK | os.W_OK):
        return {"status": "down", "error": "storage directory not writable"}
    return {"status": "up", "path": path}


def _check_redis() -> dict[str, Any]:
    """Probe the optional realtime plane.

    ``REDIS_REQUIRED=true``（默认）时 Redis 是硬依赖：不可用会让 ``check_ready``
    判为 not_ready（编排/负载均衡据此摘流量）。置 false（仅测试隔离）时它只作
    诊断信息，不参与就绪判定。``not_configured`` 表示 ``REDIS_URL`` 为空。
    """
    url = str(getattr(settings, "REDIS_URL", "") or "").strip()
    if not url:
        return {"status": "not_configured"}
    started = time.monotonic()
    client = None
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=settings.OBS_DEPENDENCY_TIMEOUT_SECONDS,
            socket_connect_timeout=settings.OBS_DEPENDENCY_TIMEOUT_SECONDS,
        )
        client.ping()
        return {"status": "up", "latency_ms": round((time.monotonic() - started) * 1000, 2)}
    except ImportError:
        return {"status": "degraded", "error": "redis package not installed"}
    except Exception as exc:  # noqa: BLE001 - fail-soft dependency probe
        return {"status": "degraded", "error": type(exc).__name__}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing must not fail the probe
                pass


def _probe_http(name: str, url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    if not url:
        return {"status": "not_configured"}
    try:
        response = requests.get(
            url.rstrip("/"),
            headers=headers or {},
            timeout=settings.OBS_DEPENDENCY_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if 200 <= response.status_code < 300:
            return {"status": "up", "http_status": response.status_code}
        return {"status": "down", "http_status": response.status_code}
    except Exception as exc:
        return {"status": "down", "error": type(exc).__name__}


def check_live() -> dict[str, Any]:
    return {"status": "live", "service": settings.OTEL_SERVICE_NAME}


_READY_CACHE_LOCK = threading.Lock()
# (monotonic 时间戳, 配置版本, 结果)
_READY_CACHE: tuple[float, str, dict[str, Any]] | None = None


def check_ready() -> dict[str, Any]:
    """就绪探针（带短 TTL 缓存）。

    K8s / 负载均衡每隔几秒探一次，无缓存时每次都会真查 SQLite + PG + storage，
    探针风暴会放大依赖负载。TTL 把一轮密集探针收敛成一次真实检查，同时保证
    真实故障在 ~1 秒内暴露。缓存以配置版本为键：改配置（如切后端）立即重探。
    """
    global _READY_CACHE
    ttl = float(getattr(settings, "OBS_READINESS_CACHE_SECONDS", 1.0) or 0.0)
    revision = _config_revision()
    if ttl > 0:
        with _READY_CACHE_LOCK:
            cached = _READY_CACHE
        if cached is not None:
            cached_at, cached_revision, payload = cached
            if cached_revision == revision and (time.monotonic() - cached_at) < ttl:
                return payload

    payload = _compute_ready()
    if ttl > 0:
        with _READY_CACHE_LOCK:
            _READY_CACHE = (time.monotonic(), revision, payload)
    return payload


def reset_readiness_cache() -> None:
    """清空就绪缓存（测试/配置变更后强制重探）。"""
    global _READY_CACHE
    with _READY_CACHE_LOCK:
        _READY_CACHE = None


def _compute_ready() -> dict[str, Any]:
    dependencies = {
        "database": _check_sqlite(),
        "control_plane": check_control_plane(),
        "storage": _check_storage(),
        "sandbox": _check_sandbox(),
    }
    if required_worker_roles():
        dependencies["worker"] = _check_workers()
    if bool(getattr(settings, "REDIS_REQUIRED", True)):
        # Redis 是硬依赖：不可用即 not_ready（由编排/负载均衡摘流量）。
        dependencies["redis"] = _check_redis()
    # A deliberately disabled optional extension is healthy by absence. An
    # enabled-but-unconfigured Sandbox is not ready and must not silently look
    # like a working execution backend. Other dependencies retain their
    # deployment-required semantics (including Redis when REDIS_REQUIRED=true).
    sandbox_ready = dependencies["sandbox"].get("status") in {"up", "not_configured"}
    required_ready = all(
        item.get("status") == "up"
        for name, item in dependencies.items()
        if name != "sandbox"
    )
    ready = sandbox_ready and required_ready
    return {
        "status": "ready" if ready else "not_ready",
        # v3 §11.5: the live content revision plus the settings that still need
        # a restart, so a config change is traceable from the readiness probe.
        "config_revision": _config_revision(),
        "restart_required_settings": sorted(
            getattr(settings, "RESTART_REQUIRED_SETTINGS", ())
        ),
        "dependencies": dependencies,
    }


def required_worker_roles() -> set[str]:
    value = os.getenv("HDB_REQUIRED_WORKER_ROLES", "chat,authoring,parse,export")
    return {role.strip() for role in value.split(",") if role.strip()}


def _check_workers() -> dict[str, Any]:
    workers = list_workers()
    available = {str(row.get("worker_type", "")) for row in workers}
    if "all" in available or "hardware-worker" in available:
        available.update({"chat", "authoring", "parse"})
    missing = sorted(required_worker_roles() - available)
    return {
        "status": "degraded" if missing or not workers else "up",
        "count": len(workers),
        "missing_roles": missing,
        "instances": workers,
    }


def _config_revision() -> str:
    try:
        return str(settings.CONFIG_REVISION or "")
    except Exception:  # noqa: BLE001 - readiness must not fail on metadata
        return ""


def _check_mcp() -> dict[str, Any]:
    """MCP component for the System Status page.

    ``not_configured`` when the emergency kill switch is off; ``idle`` when
    the switch is on but no server is enabled yet; ``degraded`` when an
    enabled server is unusable (prerequisites missing, store error, broken
    credentials); ``up`` otherwise.
    """

    if not bool(getattr(settings, "MCP_ENABLED", False)):
        return {"status": "not_configured"}
    try:
        from src.agents.mcp.policy import mcp_prerequisites_met
        from src.agents.mcp.store import build_mcp_store

        met, reason = mcp_prerequisites_met()
        if not met:
            return {"status": "degraded", "error": reason}
        store = build_mcp_store()
        servers = store.list_all()
        enabled = [record for record in servers if record.enabled]
        credentials_broken = [record.slug for record in enabled if not record.credentials_ok]
        if credentials_broken:
            return {
                "status": "degraded",
                "error": "credentials_unavailable",
                "enabled": len(enabled),
                "servers": len(servers),
            }
        payload: dict[str, Any] = {
            "enabled": len(enabled),
            "servers": len(servers),
            "subscriptions": store.count_subscriptions(),
            "write_enabled": bool(getattr(settings, "MCP_WRITE_ENABLED", False)),
        }
        if not enabled:
            # Built in but idle: no enabled server means nothing connects, so
            # this is not a degradation.  The master switch remains available
            # as an emergency kill switch (that state reports not_configured).
            payload["status"] = "idle"
            payload["error"] = ""
            return payload
        payload["status"] = "up"
        return payload
    except Exception as exc:  # noqa: BLE001 - status page must never raise
        return {"status": "degraded", "error": type(exc).__name__}


def _check_sandbox() -> dict[str, Any]:
    """Report the HDB control-plane boundary without claiming runtime safety.

    This probe never executes a task and does not make a remote health call.
    ``up`` means only that the deployment supplied an authenticated client and
    the local fail-closed gates are coherent; independent Runner/runtime
    acceptance evidence remains an operator/deployment responsibility.
    """

    enabled = bool(getattr(settings, "SANDBOX_ENABLED", False))
    gateway_enabled = bool(getattr(settings, "SANDBOX_GATEWAY_ENABLED", False))
    if not enabled and not gateway_enabled:
        return {
            "status": "not_configured",
            "backend": str(getattr(settings, "SANDBOX_BACKEND", "disabled") or "disabled"),
        }
    try:
        from src.sandbox.policy import require_execution_enabled
        from src.sandbox.dependencies import get_service_client

        if enabled:
            require_execution_enabled(operation="sandbox readiness")
        elif str(getattr(settings, "SANDBOX_BACKEND", "disabled")).lower() == "disabled":
            return {"status": "degraded", "error": "gateway_backend_not_configured"}
        client = get_service_client()
        if client is None:
            return {"status": "degraded", "error": "authenticated_service_client_unavailable"}
        if enabled and not client.runner_url:
            return {"status": "degraded", "error": "runner_endpoint_unavailable"}
        if gateway_enabled and not client.gateway_url:
            return {"status": "degraded", "error": "gateway_endpoint_unavailable"}
        return {
            "status": "up",
            "backend": str(getattr(settings, "SANDBOX_BACKEND", "disabled") or "disabled"),
            "gateway_enabled": gateway_enabled,
            "runtime_attestation_ref_present": bool(
                str(getattr(settings, "SANDBOX_RUNTIME_ATTESTATION_REF", "") or "").strip()
            ),
        }
    except Exception as exc:  # noqa: BLE001 - health must remain bounded
        return {"status": "degraded", "error": type(exc).__name__}


def check_dependencies() -> dict[str, Any]:
    ragflow_headers = {"Authorization": f"Bearer {settings.RAGFLOW_API_KEY}"} if settings.RAGFLOW_API_KEY else {}
    if settings.RAGFLOW_BASE_URL:
        ragflow = _probe_http(
            "ragflow",
            f"{settings.RAGFLOW_BASE_URL.rstrip('/')}/api/v1/datasets",
            headers=ragflow_headers,
        )
    else:
        ragflow = {"status": "not_configured"}

    if settings.AGENT_LLM_PROVIDER == settings.Provider.OLLAMA:
        llm = _probe_http("llm", f"{settings.AGENT_OLLAMA_BASE_URL.rstrip('/')}/api/tags")
    elif settings.AGENT_CUSTOM_BASE_URL:
        llm_headers = {"Authorization": f"Bearer {settings.AGENT_CUSTOM_API_KEY}"} if settings.AGENT_CUSTOM_API_KEY else {}
        llm = _probe_http("llm", f"{settings.AGENT_CUSTOM_BASE_URL.rstrip('/')}/models", headers=llm_headers)
    else:
        llm = {"status": "not_configured"}

    dependencies = {
        "database": _check_sqlite(),
        "control_plane": check_control_plane(),
        "storage": _check_storage(),
        "worker": _check_workers(),
        "redis": _check_redis(),
        "ragflow": ragflow,
        "llm": llm,
        "mcp": _check_mcp(),
        "sandbox": _check_sandbox(),
    }
    failed = [name for name, item in dependencies.items() if item.get("status") == "down"]
    degraded = [name for name, item in dependencies.items() if item.get("status") == "degraded"]
    return {
        "status": "down" if failed else ("degraded" if degraded else "up"),
        "dependencies": dependencies,
    }
