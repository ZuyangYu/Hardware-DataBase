"""Personal MCP credentials and the short-lived session exchange.

The personal token is long-lived, per-user and revocable.  It is only accepted
by ``POST /mcp/session``; internal APIs keep using normal bearer sessions.
"""

from __future__ import annotations

import os
import shlex

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

import src.settings as settings
from src.core import mcp_tokens
from src.core.auth import AuthService, AuthUser
from src.api.deps import get_auth_service, require_employee

router = APIRouter(tags=["mcp"])


class McpTokenStatus(BaseModel):
    has_token: bool = False
    token_id: str = ""
    name: str = ""
    masked_token: str = ""
    created_at: str = ""
    expires_at: str = ""
    last_used_at: str = ""
    api_base: str = ""


class CreateMcpTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="个人 MCP", min_length=1, max_length=120)


class CreateMcpTokenResponse(McpTokenStatus):
    token: str = ""
    command: str = ""


class McpSessionResponse(BaseModel):
    token: str
    expires_at: str
    user: dict


def _api_base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/v1"


def _mcp_remote_url(request: Request) -> str:
    """Public URL of the hdb MCP bridge (streamable-http, remote mode).

    Defaults to the API host + :9100/mcp; HDB_MCP_HTTP_HOST/HDB_MCP_HTTP_PORT
    override when the bridge is deployed elsewhere (nginx, other box, …).
    """
    host = os.getenv("HDB_MCP_HTTP_HOST") or (request.url.hostname or "127.0.0.1")
    port = os.getenv("HDB_MCP_HTTP_PORT", "9100")
    scheme = os.getenv("HDB_MCP_HTTP_SCHEME", "http")
    return f"{scheme}://{host}:{port}/mcp"


def _command(request: Request, token: str) -> str:
    api_base = _api_base(request)
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
    wrapper = os.getenv("MCP_BRIDGE_WRAPPER", "").strip() or os.path.join(
        repo, "scripts", "hdb_mcp_stdio.sh"
    )
    token_file = "~/.config/hdb/mcp_token"
    return (
        "install -d -m 700 ~/.config/hdb && "
        f"printf '%s' {shlex.quote(token)} > {token_file} && "
        f"chmod 600 {token_file} && "
        "opencode mcp add hdb --global "
        f"--env {shlex.quote('HDB_API_BASE=' + api_base)} -- "
        f"{shlex.quote(wrapper)}"
    )


@router.get("/me/mcp-token", response_model=McpTokenStatus)
def get_my_mcp_token(
    request: Request,
    user: AuthUser = Depends(require_employee),
) -> McpTokenStatus:
    return McpTokenStatus(
        **mcp_tokens.get_status(user.id),
        api_base=_api_base(request),
        mcp_remote_url=_mcp_remote_url(request),
    )


@router.post("/me/mcp-token", response_model=CreateMcpTokenResponse)
def create_my_mcp_token(
    body: CreateMcpTokenRequest,
    request: Request,
    user: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
) -> CreateMcpTokenResponse:
    created = mcp_tokens.create_or_rotate(user.id, name=body.name)
    try:
        auth._audit(
            "mcp_token_create",
            actor=user,
            target_type="mcp_token",
            target_id=created["token_id"],
            success=True,
        )
    except Exception:
        pass
    return CreateMcpTokenResponse(
        has_token=True,
        **created,
        api_base=_api_base(request),
        mcp_remote_url=_mcp_remote_url(request),
        command=_command(request, created["token"]),
    )


@router.delete("/me/mcp-token")
def revoke_my_mcp_token(
    user: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    revoked = mcp_tokens.revoke(user.id)
    try:
        auth._audit(
            "mcp_token_revoke",
            actor=user,
            target_type="mcp_token",
            target_id=str(user.id),
            success=True,
            metadata={"revoked": revoked},
        )
    except Exception:
        pass
    return {"ok": True, "revoked": revoked}


@router.post("/mcp/session", response_model=McpSessionResponse)
def exchange_mcp_token(
    x_hdb_mcp_token: str | None = Header(default=None, alias="X-HDB-MCP-Token"),
    auth: AuthService = Depends(get_auth_service),
) -> McpSessionResponse:
    user_id, status = mcp_tokens.resolve(x_hdb_mcp_token or "")
    if user_id is None:
        try:
            auth._audit(
                "mcp_session_denied",
                target_type="mcp_token",
                success=False,
                error_message=status,
            )
        except Exception:
            pass
        raise HTTPException(status_code=401, detail=f"MCP token {status}")
    user = auth.get_user_by_id(user_id)
    if user is None or not user.is_active or user.role != "employee":
        raise HTTPException(status_code=401, detail="MCP token user is unavailable")
    session = auth.create_session_for_user(
        user.id,
        ttl_hours=settings.MCP_SESSION_TTL_HOURS,
        actor=user,
    )
    if session is None:
        raise HTTPException(status_code=401, detail="cannot issue MCP session")
    token, expires_at = session
    return McpSessionResponse(
        token=token,
        expires_at=expires_at,
        user={
            "username": user.username,
            "role": user.role,
            "department_id": user.department_id,
        },
    )


__all__ = ["router"]
