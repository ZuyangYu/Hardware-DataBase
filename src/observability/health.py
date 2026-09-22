"""Small dependency checks for probes and the admin System Status page."""

from __future__ import annotations

import os
import sqlite3
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

    Redis never gates readiness: the durable flags/DB replay are authoritative,
    so an unreachable Redis is reported as ``degraded`` (fail-soft) rather than
    taking the deployment down.  ``not_configured`` means ``REDIS_URL`` is empty.
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
        )
        if response.status_code < 500:
            return {"status": "up", "http_status": response.status_code}
        return {"status": "down", "http_status": response.status_code}
    except Exception as exc:
        return {"status": "down", "error": type(exc).__name__}


def check_live() -> dict[str, Any]:
    return {"status": "live", "service": settings.OTEL_SERVICE_NAME}


def check_ready() -> dict[str, Any]:
    dependencies = {
        "database": _check_sqlite(),
        "control_plane": check_control_plane(),
        "storage": _check_storage(),
    }
    ready = all(item.get("status") == "up" for item in dependencies.values())
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


def _config_revision() -> str:
    try:
        return str(settings.CONFIG_REVISION or "")
    except Exception:  # noqa: BLE001 - readiness must not fail on metadata
        return ""


def _check_mcp() -> dict[str, Any]:
    """Three-state MCP component for the System Status page.

    ``not_configured`` when the global switch is off; ``degraded`` when the
    feature is on but unusable (prerequisites missing, store error, broken
    credentials on an enabled server, or no enabled server); ``up`` otherwise.
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
            payload["status"] = "degraded"
            payload["error"] = "no_enabled_server"
            return payload
        payload["status"] = "up"
        return payload
    except Exception as exc:  # noqa: BLE001 - status page must never raise
        return {"status": "degraded", "error": type(exc).__name__}


def check_dependencies() -> dict[str, Any]:
    workers = list_workers()
    worker_status = "up" if workers else "degraded"
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
        llm = _probe_http("llm", f"{settings.AGENT_CUSTOM_BASE_URL.rstrip('/')}/models")
    else:
        llm = {"status": "not_configured"}

    dependencies = {
        "database": _check_sqlite(),
        "control_plane": check_control_plane(),
        "storage": _check_storage(),
        "worker": {"status": worker_status, "count": len(workers), "instances": workers},
        "redis": _check_redis(),
        "ragflow": ragflow,
        "llm": llm,
        "mcp": _check_mcp(),
    }
    failed = [name for name, item in dependencies.items() if item.get("status") == "down"]
    degraded = [name for name, item in dependencies.items() if item.get("status") == "degraded"]
    return {
        "status": "down" if failed else ("degraded" if degraded else "up"),
        "dependencies": dependencies,
    }
