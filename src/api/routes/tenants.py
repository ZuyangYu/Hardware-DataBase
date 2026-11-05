"""Tenant provisioning API.

Tenants are the isolation boundary above users, departments, KBs and
workspaces.  Tenant creation is platform-operator only; tenant-scoped admins
can still see their own tenant.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import src.settings
from src.api.deps import get_auth_service, require_system_admin
from src.core.auth import AuthService, AuthUser

router = APIRouter(tags=["tenants"])


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = None


class TenantView(BaseModel):
    id: str
    name: str
    slug: str
    created_at: str = ""


@router.get("/tenants", response_model=list[TenantView])
def list_tenants(
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
) -> list[TenantView]:
    tenants = auth.list_tenants()
    if actor.username != src.settings.AUTH_DEFAULT_ADMIN_USERNAME:
        tenants = [item for item in tenants if item.id == actor.tenant_id]
    return [TenantView(id=item.id, name=item.name, slug=item.slug, created_at=item.created_at) for item in tenants]


@router.post("/tenants", response_model=TenantView, status_code=201)
def create_tenant(
    body: CreateTenantRequest,
    actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
) -> TenantView:
    if actor.username != src.settings.AUTH_DEFAULT_ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail="仅平台管理员可以创建租户")
    tenant = auth.create_tenant(body.name, body.slug)
    return TenantView(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        created_at=tenant.created_at,
    )
