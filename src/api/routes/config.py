"""Configuration management endpoints (system_admin only).

- ``GET /config`` — read the current effective config (secrets redacted)
- ``PUT /config`` — write whitelisted keys to ``.env`` and hot-reload
- ``GET /health/ragflow`` — probe the configured RAGFlow instance

Only keys present in ``src.settings.DEFAULT_VALUES`` may be written — this
is the same whitelist the Streamlit settings panel uses, and it prevents
arbitrary environment variables from being injected via the API.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import src.settings as settings

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthUser

from src.api.deps import get_pipeline, require_system_admin, reset_pipeline
from src.api.schemas import (
    ConfigResponse,
    LlmHealthResponse,
    OkResponse,
    RagflowHealthResponse,
    UpdateConfigRequest,
)

router = APIRouter(tags=["config"])


# Keys whose values must not be returned in plaintext.
_SECRET_KEYS = {
    "RAGFLOW_API_KEY",
    "AGENT_CUSTOM_API_KEY",
    "AUTH_DEFAULT_ADMIN_PASSWORD",
}


def _redact(key: str, value: object) -> object:
    """Return the value with secrets replaced by a masking marker."""
    if key in _SECRET_KEYS and value:
        return "***"
    return value


def _current_config() -> dict[str, object]:
    """Snapshot of the effective settings, using DEFAULT_VALUES as the key set."""
    snapshot: dict[str, object] = {}
    for key in settings.DEFAULT_VALUES:
        # Read the live attribute rather than os.getenv so we reflect
        # runtime state after apply_settings + reload_settings.
        value = getattr(settings, key, settings.DEFAULT_VALUES.get(key, ""))
        # Provider is an Enum — surface its .value for JSON serialisation.
        if hasattr(value, "value"):
            value = value.value
        snapshot[key] = _redact(key, value)
    return snapshot


@router.get("/config", response_model=ConfigResponse)
def get_config(_actor: AuthUser = Depends(require_system_admin)):
    """Return the effective runtime config with secrets redacted."""
    return ConfigResponse(settings=_current_config())


@router.put("/config", response_model=OkResponse)
def update_config(
    body: UpdateConfigRequest,
    _actor: AuthUser = Depends(require_system_admin),
    pipeline: AppPipeline = Depends(get_pipeline),
):
    """Persist whitelisted keys to ``.env`` and hot-reload the runtime."""
    allowed = set(settings.DEFAULT_VALUES.keys())
    unknown = [k for k in body.settings if k not in allowed]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown or read-only config keys: {', '.join(unknown)}",
        )
    # apply_settings persists to .env then calls reload_settings.
    pipeline.apply_settings({k: str(v) if v is not None else "" for k, v in body.settings.items()})
    # Streamlit does init_pipeline.clear() so the next call rebuilds the pipeline
    # with the fresh settings; do the same on the API side.
    reset_pipeline()
    # Record the change in the audit log. apply_settings is a stateless
    # staticmethod with no actor, so the audit lives at the route layer (the
    # only caller besides the Streamlit settings panel, which records its own).
    try:
        from src.core.app_logs import AppLogService

        AppLogService().record_audit(
            action="change_settings",
            actor=_actor,
            target_type="system_settings",
            target_id="env",
            metadata={
                "keys": list(body.settings.keys()),
                "source": "api",
            },
        )
    except Exception:
        pass  # fail-soft: audit must not break the config update
    return OkResponse(ok=True, message="config updated")


@router.get("/health/ragflow", response_model=RagflowHealthResponse)
def health_ragflow(_actor: AuthUser = Depends(require_system_admin)):
    """Probe the configured RAGFlow instance and check dataset presence."""
    reachable, message, missing = AppPipeline.check_ragflow_connection(
        base_url=settings.RAGFLOW_BASE_URL,
        api_key=settings.RAGFLOW_API_KEY,
        dataset_names=[
            settings.RAGFLOW_GOVERNANCE_DATASET_NAME,
            settings.RAGFLOW_DESIGN_DATASET_NAME,
        ],
        timeout=settings.RAGFLOW_TIMEOUT_SECONDS,
    )
    return RagflowHealthResponse(reachable=reachable, message=message, missing_datasets=missing)


@router.get("/health/llm", response_model=LlmHealthResponse)
def health_llm(_actor: AuthUser = Depends(require_system_admin)):
    """Probe the configured LLM provider with a 1-token ping. Mirrors the
    Streamlit sidebar AI-status indicator."""
    from src.core.model_factory import create_chat_model

    provider_value = getattr(settings.AGENT_LLM_PROVIDER, "value", settings.AGENT_LLM_PROVIDER)
    try:
        create_chat_model().invoke([{"role": "user", "content": "ping"}], max_tokens=1)
        return LlmHealthResponse(reachable=True, message="LLM 连接正常", provider=str(provider_value))
    except Exception as exc:
        return LlmHealthResponse(reachable=False, message=f"LLM 连接失败: {exc}", provider=str(provider_value))