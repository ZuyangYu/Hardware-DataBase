"""API-boundary admission control: 429 + Retry-After before accepting work."""

from __future__ import annotations

from fastapi import HTTPException

from src.core.concurrency.admission import AdmissionRejected, get_admission_controller


def admission_model_name() -> str:
    """Configured agent model for the per-model admission dimension."""
    import src.settings as settings

    provider = str(getattr(settings, "AGENT_LLM_PROVIDER", "") or "").lower()
    if provider == "ollama":
        return str(getattr(settings, "AGENT_OLLAMA_MODEL", "") or "")
    return str(getattr(settings, "AGENT_CUSTOM_MODEL", "") or "")


def enforce_admission(
    user,
    tenant_id: int | str | None = None,
    *,
    resource: str = "chat",
    model: str = "",
) -> None:
    """Reject over-quota or queue-saturated requests before accepting them.

    ``resource`` selects the per-resource-type bucket (chat/deep/document/
    export/ocr/evaluation); ``model`` adds the optional per-model dimension.
    """
    try:
        get_admission_controller().check(
            user_id=getattr(user, "id", user),
            tenant_id=tenant_id,
            resource=resource,
            model=model,
        )
    except AdmissionRejected as exc:
        raise HTTPException(
            status_code=429,
            detail={"message": "请求过于频繁或系统繁忙，请稍后重试", "reason": exc.reason},
            headers={"Retry-After": str(max(1, int(round(exc.retry_after))))},
        ) from exc
