"""Employee-owned API Key management.

This router deliberately depends on the normal Session channel.  An External
API key can never be used to create, list, inspect or revoke another key.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.core.auth import AuthService, AuthUser
from src.api.deps import get_auth_service, require_employee
from src.api.schemas import OkResponse


router = APIRouter(prefix="/me/api-keys", tags=["api-keys"])


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: list[Literal["read", "write", "invoke"]] = Field(min_length=1, max_length=3)
    kb_access_mode: Literal["selected_kbs", "all_current_accessible_kbs"] = "selected_kbs"
    kb_refs: list[str] = Field(default_factory=list, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class ApiKeyView(BaseModel):
    key_id: str
    name: str
    scopes: list[str]
    kb_access_mode: str
    kb_refs: list[str]
    created_at: str
    expires_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None
    status: str
    masked_key: str


class CreateApiKeyResponse(ApiKeyView):
    api_key: str


class RotateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class RotateApiKeyResponse(CreateApiKeyResponse):
    rotated_from: str


class NarrowApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_refs: list[str] = Field(min_length=1, max_length=100)


class ApiKeyKnowledgeBaseOption(BaseModel):
    kb_ref: str
    name: str
    department_name: str | None = None


def _session_only(
    x_hdb_api_key: str | None = Header(default=None, alias="X-HDB-API-Key"),
    user: AuthUser = Depends(require_employee),
) -> AuthUser:
    if x_hdb_api_key and x_hdb_api_key.strip():
        raise HTTPException(status_code=401, detail="API Key 管理仅接受员工 Session")
    return user


@router.post("", response_model=CreateApiKeyResponse, status_code=201)
def create_api_key(
    body: CreateApiKeyRequest,
    user: AuthUser = Depends(_session_only),
    auth: AuthService = Depends(get_auth_service),
) -> CreateApiKeyResponse:
    try:
        result = auth.create_employee_api_key(
            user,
            name=body.name,
            scopes=body.scopes,
            kb_access_mode=body.kb_access_mode,
            kb_refs=body.kb_refs,
            expires_in_days=body.expires_in_days,
        )
    except ValueError as exc:
        if str(exc) == "KEY_LIMIT_REACHED":
            raise HTTPException(status_code=409, detail="KEY_LIMIT_REACHED") from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreateApiKeyResponse(**result)


@router.get("/available-kbs", response_model=list[ApiKeyKnowledgeBaseOption])
def list_available_kbs_for_api_key(
    user: AuthUser = Depends(_session_only),
    auth: AuthService = Depends(get_auth_service),
) -> list[ApiKeyKnowledgeBaseOption]:
    """Issue the opaque KB options needed before the first Key exists."""
    options: list[ApiKeyKnowledgeBaseOption] = []
    for identity in auth.list_accessible_knowledge_base_identities(user):
        ref = auth.ensure_external_kb_ref(identity.kb_id)
        options.append(
            ApiKeyKnowledgeBaseOption(
                kb_ref=ref.kb_ref,
                name=ref.kb_name,
                department_name=ref.department_name,
            )
        )
    return options


@router.get("", response_model=list[ApiKeyView])
def list_api_keys(
    user: AuthUser = Depends(_session_only),
    auth: AuthService = Depends(get_auth_service),
) -> list[ApiKeyView]:
    return [ApiKeyView(**item) for item in auth.list_employee_api_keys(user)]


@router.get("/{key_id}", response_model=ApiKeyView)
def get_api_key(
    key_id: str,
    user: AuthUser = Depends(_session_only),
    auth: AuthService = Depends(get_auth_service),
) -> ApiKeyView:
    item = auth.get_employee_api_key(user, key_id)
    if item is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return ApiKeyView(**item)


@router.post("/{key_id}/rotate", response_model=RotateApiKeyResponse)
def rotate_api_key(
    key_id: str,
    body: RotateApiKeyRequest,
    user: AuthUser = Depends(_session_only),
    auth: AuthService = Depends(get_auth_service),
) -> RotateApiKeyResponse:
    try:
        result = auth.rotate_employee_api_key(
            user,
            key_id,
            expires_in_days=body.expires_in_days,
        )
    except ValueError as exc:
        if str(exc) == "KEY_LIMIT_REACHED":
            raise HTTPException(status_code=409, detail="KEY_LIMIT_REACHED") from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return RotateApiKeyResponse(**result)


@router.patch("/{key_id}", response_model=ApiKeyView)
def narrow_api_key_kbs(
    key_id: str,
    body: NarrowApiKeyRequest,
    user: AuthUser = Depends(_session_only),
    auth: AuthService = Depends(get_auth_service),
) -> ApiKeyView:
    try:
        result = auth.narrow_employee_api_key_kbs(user, key_id, body.kb_refs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return ApiKeyView(**result)


@router.delete("/{key_id}", response_model=OkResponse)
def revoke_api_key(
    key_id: str,
    user: AuthUser = Depends(_session_only),
    auth: AuthService = Depends(get_auth_service),
) -> OkResponse:
    if not auth.revoke_employee_api_key(user, key_id):
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return OkResponse(ok=True, message="API Key 已撤销")
