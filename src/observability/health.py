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
        "ragflow": ragflow,
        "llm": llm,
        # v3 §12.4: the turn-less adapter keeps no durable Turn; the durable
        # path is POST /conversations/{id}/turns.
        "legacy_query": {
            "status": "enabled"
            if bool(getattr(settings, "LEGACY_QUERY_ENABLED", True))
            else "disabled",
            "durable_turn": False,
            "note": "调试/兼容入口；生产聊天使用持久化 Turn",
        },
    }
    failed = [name for name, item in dependencies.items() if item.get("status") == "down"]
    degraded = [name for name, item in dependencies.items() if item.get("status") == "degraded"]
    return {
        "status": "down" if failed else ("degraded" if degraded else "up"),
        "dependencies": dependencies,
    }
