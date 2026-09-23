"""HDB-owned sandbox task admission and observation endpoints.

These routes manage a durable control-plane record only.  They never launch a
process, open a runtime socket, or accept a command/host path from a caller.
Actual execution is an explicitly configured external Runner concern.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any

import src.settings as settings
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, require_employee, require_system_admin
from src.api.schemas import (
    SandboxCancelRequest,
    SandboxCapabilityGrantRequest,
    SandboxCapabilityGrantResponse,
    SandboxTaskCreateRequest,
    SandboxBundleListResponse,
    SandboxBundleRegisterRequest,
    SandboxBundleResponse,
    SandboxArtifactListResponse,
    SandboxArtifactResponse,
    SandboxArtifactStageRequest,
    SandboxGatewayInvokeRequest,
    SandboxGatewaySubscriptionRequest,
    SandboxGatewaySubscriptionResponse,
    SandboxGatewayListResponse,
    SandboxGatewayRegisterRequest,
    SandboxGatewayResponse,
    SandboxSkillListResponse,
    SandboxSkillRegisterRequest,
    SandboxSkillExecutionBindingRequest,
    SandboxSkillRunRequest,
    SandboxSkillResponse,
    SandboxTaskEventsResponse,
    SandboxTaskListResponse,
    SandboxTaskResponse,
    SandboxRunnerResponse,
)
from src.core.auth import AuthService, AuthUser, ROLE_SYSTEM_ADMIN
from src.sandbox.models import SandboxTaskSpec
from src.sandbox.authorization import (
    SANDBOX_CAPABILITIES,
    SandboxAuthorizationError,
    SandboxAuthorizationStore,
    capability_snapshot_hash,
)
from src.sandbox.artifacts import build_registered_scanner
from src.sandbox.artifact_broker import (
    ArtifactAccessDenied,
    ArtifactBroker,
    ArtifactBrokerError,
    ArtifactNotFound,
    ArtifactStateConflict,
)
from src.sandbox.client import SandboxClientConfigurationError, SandboxServiceClient
from src.sandbox.dependencies import get_service_client
from src.sandbox.catalog import (
    BundleCatalogError,
    BundleNotAuthorized,
    BundleNotFound,
    BundleStateConflict,
    SandboxCatalog,
)
from src.agents.mcp.gateway import (
    GatewayDeployment,
    GatewayNotFoundError,
    GatewayPolicyError,
    GatewayRegistry,
    GatewayStateError,
)
from src.sandbox.protocol import RequestEnvelope
from src.sandbox.policy import SandboxPolicyError, require_execution_enabled
from src.sandbox.service import SandboxExecutionAdapter, SandboxExecutionError
from src.sandbox.reconciler import SandboxReconciler
from src.sandbox.skills import (
    SkillCatalog,
    SkillCatalogError,
    SkillNotFound,
    SkillStateConflict,
)
from src.sandbox.task_store import (
    IdempotencyConflict,
    InvalidTransition,
    SandboxTaskStore,
    TaskConflict,
    TaskNotFound,
)


router = APIRouter(prefix="/sandbox", tags=["sandbox"])


def _safe_error(value: Any) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = re.sub(
        r"(?i)(token|secret|password|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s,;]+)", "<internal-ref>", text)
    return text[:300]


def get_sandbox_task_store() -> SandboxTaskStore:
    return SandboxTaskStore()


def get_sandbox_catalog() -> SandboxCatalog:
    return SandboxCatalog()


def get_sandbox_authorization_store() -> SandboxAuthorizationStore:
    return SandboxAuthorizationStore()


def get_sandbox_gateway_registry() -> GatewayRegistry:
    return GatewayRegistry()


def get_sandbox_artifact_broker() -> ArtifactBroker:
    return ArtifactBroker(scanner=build_registered_scanner())


def get_skill_catalog() -> SkillCatalog:
    return SkillCatalog()


def get_sandbox_service_client() -> SandboxServiceClient | None:
    """Return an explicitly wired service client, never a ref-as-token client.

    Deployments must override this dependency with a workload-identity/secret
    provider.  HDB intentionally cannot turn ``SANDBOX_SERVICE_AUTH_REF`` into
    a bearer token by guessing or echoing the reference.
    """

    return get_service_client()


def _audit(
    user: AuthUser,
    *,
    action: str,
    task_id: str | None = None,
    target_id: str | None = None,
    target_type: str = "sandbox_task",
    success: bool = True,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        from src.core.app_logs import AppLogService

        AppLogService().record_audit(
            action=action,
            actor=user,
            target_type=target_type,
            target_id=str(target_id or task_id or ""),
            success=success,
            error_message=_safe_error(error),
            metadata=metadata or {},
        )
    except Exception:
        pass


def _context_capabilities(
    user: AuthUser,
    auth: AuthService,
    authorization: SandboxAuthorizationStore | None = None,
) -> set[str]:
    """Resolve server-owned capability facts; request JSON cannot grant access.

    The deployment allowlist is always the upper bound.  Explicit user or
    department grants can narrow it, but metadata supplied by a browser/model
    can never widen it.
    """

    context_capabilities: set[str] = set()
    try:
        context = build_context_for_user(user, auth=auth)
        metadata = dict(getattr(context, "metadata", {}) or {})
        for key in ("sandbox_capabilities", "capabilities", "authorized_capabilities"):
            value = metadata.get(key)
            if isinstance(value, (list, tuple, set, frozenset)):
                context_capabilities.update(str(item).strip() for item in value if str(item).strip())
    except Exception:
        # A missing authorization context is fail-closed; the deployment-level
        # capability snapshot may still be empty as well.
        pass
    store = authorization or SandboxAuthorizationStore()
    try:
        return store.effective_capabilities(
            user_id=user.id,
            department_id=user.department_id,
            context_capabilities=context_capabilities,
        )
    except Exception:
        return set()


def _require_task_visibility(task: Any, user: AuthUser) -> None:
    if user.role == ROLE_SYSTEM_ADMIN:
        return
    if str(task.owner_id) != str(user.id) or str(task.department_id) != str(user.department_id or ""):
        raise HTTPException(status_code=404, detail="sandbox task not found")


def _raise_store_error(exc: Exception) -> None:
    if isinstance(exc, TaskNotFound):
        raise HTTPException(status_code=404, detail="sandbox task not found") from exc
    if isinstance(exc, SandboxPolicyError):
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    if isinstance(exc, (TaskConflict, IdempotencyConflict, InvalidTransition)):
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc


@router.post("/bundles", response_model=SandboxBundleResponse)
def register_bundle(
    body: SandboxBundleRegisterRequest,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
):
    try:
        bundle = catalog.register(
            bundle_ref=body.bundle_ref,
            bundle_digest=body.bundle_digest,
            profile_ref=body.profile_ref,
            required_capabilities=body.required_capabilities,
            network_policy_ref=body.network_policy_ref,
            sbom_ref=body.sbom_ref,
            signature_ref=body.signature_ref,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            tool_allowlist=body.tool_allowlist,
            runtime_policy_ref=body.runtime_policy_ref,
            runtime_policy_digest=body.runtime_policy_digest,
            catalog_source=body.catalog_source,
            actor=actor.username,
            metadata=body.metadata,
            declaration=body.metadata,
        )
    except BundleCatalogError as exc:
        _audit(actor, action="sandbox.bundle_publish", task_id=body.bundle_ref, success=False, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    _audit(actor, action="sandbox.bundle_publish", task_id=bundle.bundle_ref)
    return SandboxBundleResponse(bundle=bundle.to_dict())


@router.post("/skills", response_model=SandboxSkillResponse)
def register_skill(
    body: SandboxSkillRegisterRequest,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    try:
        skill = catalog.register(
            skill_ref=body.skill_ref,
            version=body.version,
            content=body.content,
            source_ref=body.source_ref,
            catalog_source=body.catalog_source,
            department_id=body.department_id,
            actor=actor.username,
            metadata=body.metadata,
        )
    except SkillCatalogError as exc:
        _audit(
            actor,
            action="sandbox.skill_publish",
            task_id=body.skill_ref,
            target_type="sandbox_skill",
            success=False,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    _audit(actor, action="sandbox.skill_publish", task_id=skill.skill_ref, target_type="sandbox_skill")
    return SandboxSkillResponse(skill=skill.to_dict())


@router.get("/skills", response_model=SandboxSkillListResponse)
def list_skills(
    _actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    return SandboxSkillListResponse(items=[item.to_dict() for item in catalog.list(include_revoked=True)])


@router.get("/skills/available")
def available_skills(
    user: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
    skills: SkillCatalog = Depends(get_skill_catalog),
    authorization: SandboxAuthorizationStore = Depends(get_sandbox_authorization_store),
):
    if not bool(getattr(settings, "SKILLS_ENABLED", False)):
        return {"items": []}
    try:
        require_execution_enabled(operation="Skill bundle listing")
    except SandboxPolicyError:
        return {"items": []}
    effective = _context_capabilities(user, auth, authorization)
    items: list[dict[str, Any]] = []
    for skill in skills.list(enabled_only=True):
        try:
            bundle = skills.execution_bundle(
                skill.skill_ref, principal_id=user.id, department_id=user.department_id or "",
            )
        except SkillCatalogError:
            continue
        if not set(bundle.required_capabilities).issubset(effective):
            continue
        items.append({
            "skill_ref": skill.skill_ref,
            "version": skill.version,
            "bundle_ref": bundle.bundle_ref,
            "input_schema": dict(bundle.input_schema),
        })
    return {"items": items}


def _skill_state_action(
    skill_ref: str,
    state: str,
    actor: AuthUser,
    catalog: SkillCatalog,
):
    try:
        if state == "reviewed":
            skill = catalog.review(skill_ref, actor=actor.username)
        elif state == "tested":
            skill = catalog.mark_tested(skill_ref, actor=actor.username)
        elif state == "enabled":
            skill = catalog.enable(skill_ref, actor=actor.username)
        else:
            skill = catalog.revoke(skill_ref, actor=actor.username)
    except (SkillCatalogError, SkillNotFound, SkillStateConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action=f"sandbox.skill_{state}", task_id=skill_ref, target_type="sandbox_skill")
    return SandboxSkillResponse(skill=skill.to_dict())


@router.post("/skills/{skill_ref:path}/review", response_model=SandboxSkillResponse)
def review_skill(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    return _skill_state_action(skill_ref, "reviewed", actor, catalog)


@router.post("/skills/{skill_ref:path}/tested", response_model=SandboxSkillResponse)
def test_skill(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    return _skill_state_action(skill_ref, "tested", actor, catalog)


@router.post("/skills/{skill_ref:path}/enable", response_model=SandboxSkillResponse)
def enable_skill(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    return _skill_state_action(skill_ref, "enabled", actor, catalog)


@router.post("/skills/{skill_ref:path}/revoke", response_model=SandboxSkillResponse)
def revoke_skill(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    return _skill_state_action(skill_ref, "revoked", actor, catalog)


@router.post("/skills/{skill_ref:path}/bind-execution", response_model=SandboxSkillResponse)
def bind_skill_execution(
    skill_ref: str,
    body: SandboxSkillExecutionBindingRequest,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    try:
        skill = catalog.bind_execution(
            skill_ref, bundle_ref=body.bundle_ref, bundle_digest=body.bundle_digest,
            profile_ref=body.profile_ref, actor=actor.username,
        )
    except (SkillCatalogError, BundleCatalogError) as exc:
        _audit(actor, action="sandbox.skill_bind", task_id=skill_ref,
               target_type="sandbox_skill", success=False, error=str(exc))
        raise HTTPException(status_code=409, detail="Skill or reviewed bundle is not eligible") from exc
    _audit(actor, action="sandbox.skill_bind", task_id=skill_ref, target_type="sandbox_skill",
           metadata={"bundle_ref": skill.execution_bundle_ref, "bundle_digest": skill.execution_bundle_digest})
    return SandboxSkillResponse(skill=skill.to_dict())


@router.post("/skills/{skill_ref:path}/run", response_model=SandboxTaskResponse,
             status_code=status.HTTP_202_ACCEPTED)
def run_skill(
    skill_ref: str,
    body: SandboxSkillRunRequest,
    user: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
    skills: SkillCatalog = Depends(get_skill_catalog),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
    authorization: SandboxAuthorizationStore = Depends(get_sandbox_authorization_store),
):
    try:
        require_execution_enabled(operation="Skill bundle admission")
        bundle = skills.execution_bundle(
            skill_ref, principal_id=user.id, department_id=user.department_id or "",
        )
    except (SkillCatalogError, SandboxPolicyError) as exc:
        _audit(user, action="sandbox.skill_run", task_id=skill_ref,
               target_type="sandbox_skill", success=False, error=str(exc))
        raise HTTPException(status_code=403, detail="Skill execution is not authorized") from exc
    # Bind retries to the reviewed Skill identity, not just the shared bundle.
    skill = skills.require(skill_ref)
    identity = hashlib.sha256(
        f"{skill.skill_ref}:{skill.version}:{skill.content_digest}".encode("utf-8")
    ).hexdigest()[:20]
    required = set(bundle.required_capabilities)
    if not required.issubset(_context_capabilities(user, auth, authorization)) or user.department_id in (None, ""):
        raise HTTPException(status_code=403, detail="Skill bundle capabilities are not authorized")
    try:
        bundle = catalog.authorize(
            bundle_ref=bundle.bundle_ref, bundle_digest=bundle.bundle_digest,
            profile_ref=bundle.profile_ref, required_capabilities=required,
            network_policy_ref="", parameters=body.parameters,
        )
        spec = SandboxTaskSpec(
            idempotency_key=f"skill-{identity}-{body.idempotency_key}",
            owner_id=str(user.id), department_id=str(user.department_id),
            bundle_ref=bundle.bundle_ref, bundle_digest=bundle.bundle_digest,
            profile_ref=bundle.profile_ref, policy_snapshot_hash=bundle.policy_snapshot_hash,
            capability_snapshot_hash=capability_snapshot_hash(
                user_id=user.id, department_id=user.department_id,
                capabilities=required, policy_snapshot_hash=bundle.policy_snapshot_hash,
            ),
            required_capabilities=tuple(required), parameters=body.parameters,
            skill_ref=skill.skill_ref, skill_content_digest=skill.content_digest,
        )
        task = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker).admit(
            spec, actor_id=str(user.id),
        )
    except (BundleCatalogError, SandboxPolicyError, SandboxExecutionError, TaskConflict) as exc:
        _audit(user, action="sandbox.skill_run", task_id=skill_ref,
               target_type="sandbox_skill", success=False, error=str(exc))
        raise HTTPException(status_code=409, detail="Skill task admission failed") from exc
    _audit(user, action="sandbox.skill_run", task_id=task.task_id,
           target_type="sandbox_skill", metadata={"skill_ref": skill.skill_ref,
                                                   "bundle_ref": bundle.bundle_ref})
    return SandboxTaskResponse(task=task.to_dict())


@router.get("/bundles", response_model=SandboxBundleListResponse)
def list_bundles(
    actor: AuthUser = Depends(require_system_admin),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
):
    _ = actor
    return SandboxBundleListResponse(items=[item.to_dict() for item in catalog.list(include_revoked=True)])


@router.post("/bundles/{bundle_ref:path}/review", response_model=SandboxBundleResponse)
def review_bundle(
    bundle_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
):
    try:
        bundle = catalog.review(bundle_ref, actor=actor.username)
    except (BundleCatalogError, BundleNotFound) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="sandbox.bundle_review", task_id=bundle_ref)
    return SandboxBundleResponse(bundle=bundle.to_dict())


@router.post("/bundles/{bundle_ref:path}/enable", response_model=SandboxBundleResponse)
def enable_bundle(
    bundle_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
):
    try:
        bundle = catalog.enable(bundle_ref, actor=actor.username)
    except (BundleCatalogError, BundleNotFound, BundleStateConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="sandbox.bundle_enable", task_id=bundle_ref)
    return SandboxBundleResponse(bundle=bundle.to_dict())


@router.post("/bundles/{bundle_ref:path}/revoke", response_model=SandboxBundleResponse)
def revoke_bundle(
    bundle_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
):
    try:
        bundle = catalog.revoke(bundle_ref, actor=actor.username)
    except (BundleCatalogError, BundleNotFound) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="sandbox.bundle_revoke", task_id=bundle_ref)
    return SandboxBundleResponse(bundle=bundle.to_dict())


@router.post("/bundles/{bundle_ref:path}/tested", response_model=SandboxBundleResponse)
def test_bundle_declaration(
    bundle_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
):
    """Record an operator/runtime test result without accepting a recipe."""

    try:
        bundle = catalog.mark_tested(bundle_ref, actor=actor.username)
    except (BundleCatalogError, BundleNotFound) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="sandbox.bundle_test", task_id=bundle_ref, target_type="sandbox_bundle")
    return SandboxBundleResponse(bundle=bundle.to_dict())


@router.post("/authorizations/grant", response_model=SandboxCapabilityGrantResponse)
def grant_sandbox_capabilities(
    body: SandboxCapabilityGrantRequest,
    actor: AuthUser = Depends(require_system_admin),
    authorization: SandboxAuthorizationStore = Depends(get_sandbox_authorization_store),
):
    try:
        capabilities = authorization.grant(
            body.principal_type,
            body.principal_id,
            body.capabilities,
            actor=actor.username,
        )
    except SandboxAuthorizationError as exc:
        _audit(actor, action="sandbox.authorization_grant", target_id=body.principal_id, success=False, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    _audit(
        actor,
        action="sandbox.authorization_grant",
        target_id=body.principal_id,
        target_type="sandbox_authorization",
        metadata={"principal_type": body.principal_type, "capability_count": len(capabilities)},
    )
    return SandboxCapabilityGrantResponse(
        principal_type=body.principal_type,
        principal_id=body.principal_id,
        capabilities=capabilities,
    )


@router.post("/authorizations/revoke", response_model=SandboxCapabilityGrantResponse)
def revoke_sandbox_capabilities(
    body: SandboxCapabilityGrantRequest,
    actor: AuthUser = Depends(require_system_admin),
    authorization: SandboxAuthorizationStore = Depends(get_sandbox_authorization_store),
):
    try:
        capabilities = authorization.revoke(
            body.principal_type,
            body.principal_id,
            body.capabilities,
            actor=actor.username,
        )
    except SandboxAuthorizationError as exc:
        _audit(actor, action="sandbox.authorization_revoke", target_id=body.principal_id, success=False, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    _audit(
        actor,
        action="sandbox.authorization_revoke",
        target_id=body.principal_id,
        target_type="sandbox_authorization",
        metadata={"principal_type": body.principal_type, "capability_count": len(capabilities)},
    )
    return SandboxCapabilityGrantResponse(
        principal_type=body.principal_type,
        principal_id=body.principal_id,
        capabilities=capabilities,
    )


@router.get("/authorizations/{principal_type}/{principal_id}", response_model=SandboxCapabilityGrantResponse)
def list_sandbox_capabilities(
    principal_type: str,
    principal_id: str,
    actor: AuthUser = Depends(require_system_admin),
    authorization: SandboxAuthorizationStore = Depends(get_sandbox_authorization_store),
):
    _ = actor
    try:
        capabilities = authorization.list_for(principal_type, principal_id)
    except SandboxAuthorizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    return SandboxCapabilityGrantResponse(
        principal_type=principal_type,
        principal_id=principal_id,
        capabilities=list(capabilities),
    )


def _gateway_envelope(
    actor: AuthUser,
    deployment: GatewayDeployment,
    capabilities: tuple[str, ...] = ("external_tool.invoke",),
) -> RequestEnvelope:
    policy_hash = hashlib.sha256(
        f"{deployment.deployment_ref}:{deployment.profile_ref}:{deployment.digest}".encode("utf-8")
    ).hexdigest()
    snapshot_hash = capability_snapshot_hash(
        user_id=actor.id,
        department_id=actor.department_id or "system",
        capabilities=capabilities,
        policy_snapshot_hash=policy_hash,
    )
    return RequestEnvelope.create(
        actor_id=str(actor.id),
        department_id=str(actor.department_id or "system"),
        policy_snapshot_hash=policy_hash,
        capability_snapshot=capabilities,
        capability_snapshot_hash=snapshot_hash,
        audience="sandbox-gateway",
    )


def _require_gateway_enabled() -> None:
    if not bool(getattr(settings, "SANDBOX_GATEWAY_ENABLED", False)):
        raise HTTPException(status_code=409, detail="sandbox Gateway is disabled")


def _require_service_client(client: SandboxServiceClient | None) -> SandboxServiceClient:
    if client is None:
        raise HTTPException(status_code=503, detail="sandbox service identity provider is not configured")
    return client


@router.post("/gateways", response_model=SandboxGatewayResponse)
def register_gateway(
    body: SandboxGatewayRegisterRequest,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
):
    try:
        gateway = registry.register(
            deployment_ref=body.deployment_ref,
            profile_ref=body.profile_ref,
            endpoint=body.endpoint,
            digest=body.digest,
            network_policy_ref=body.network_policy_ref,
            capabilities=body.capabilities,
            sbom_ref=body.sbom_ref,
            tool_allowlist=body.tool_allowlist,
            actor=actor.username,
            metadata=body.metadata,
            extra=body.metadata,
        )
    except GatewayPolicyError as exc:
        _audit(
            actor,
            action="plugins.mcp.gateway_publish",
            target_id=body.deployment_ref,
            target_type="sandbox_gateway",
            success=False,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    _audit(
        actor,
        action="plugins.mcp.gateway_publish",
        task_id=gateway.deployment_ref,
        target_type="sandbox_gateway",
    )
    return SandboxGatewayResponse(gateway=gateway.to_dict())


@router.get("/gateways", response_model=SandboxGatewayListResponse)
def list_gateways(
    _actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
):
    return SandboxGatewayListResponse(items=[item.to_dict() for item in registry.list(include_revoked=True)])


@router.post("/gateways/{deployment_ref:path}/review", response_model=SandboxGatewayResponse)
def review_gateway(
    deployment_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
):
    try:
        gateway = registry.review(deployment_ref, actor=actor.username)
    except (GatewayPolicyError, GatewayNotFoundError, GatewayStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="plugins.mcp.gateway_review", task_id=deployment_ref, target_type="sandbox_gateway")
    return SandboxGatewayResponse(gateway=gateway.to_dict())


@router.post("/gateways/{deployment_ref:path}/test", response_model=SandboxGatewayResponse)
def test_gateway(
    deployment_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
    client: SandboxServiceClient = Depends(get_sandbox_service_client),
):
    _require_gateway_enabled()
    client = _require_service_client(client)
    try:
        deployment = registry.require(deployment_ref)
        result = registry.health(
            deployment_ref,
            client=client,
            envelope=_gateway_envelope(actor, deployment),
        )
        health = str(result.get("status") or result.get("health_status") or "").lower()
        if health not in {"healthy", "ok", "ready"}:
            raise GatewayStateError("Gateway health check did not pass")
        gateway = registry.mark_tested(deployment_ref, actor=actor.username, health_status=health)
    except (GatewayPolicyError, GatewayNotFoundError, GatewayStateError, SandboxClientConfigurationError) as exc:
        _audit(actor, action="plugins.mcp.gateway_test", task_id=deployment_ref, target_type="sandbox_gateway", success=False, error=str(exc))
        raise HTTPException(status_code=502, detail="Gateway health test failed") from exc
    _audit(actor, action="plugins.mcp.gateway_test", task_id=deployment_ref, target_type="sandbox_gateway")
    return SandboxGatewayResponse(gateway=gateway.to_dict())


@router.post("/gateways/{deployment_ref:path}/enable", response_model=SandboxGatewayResponse)
def enable_gateway(
    deployment_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
):
    try:
        gateway = registry.enable(deployment_ref, actor=actor.username)
    except (GatewayPolicyError, GatewayNotFoundError, GatewayStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="plugins.mcp.gateway_enable", task_id=deployment_ref, target_type="sandbox_gateway")
    return SandboxGatewayResponse(gateway=gateway.to_dict())


@router.post("/gateways/{deployment_ref:path}/disable", response_model=SandboxGatewayResponse)
def disable_gateway(
    deployment_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
    client: SandboxServiceClient = Depends(get_sandbox_service_client),
):
    try:
        gateway = registry.disable(deployment_ref, actor=actor.username)
    except (GatewayPolicyError, GatewayNotFoundError, GatewayStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="plugins.mcp.gateway_disable", task_id=deployment_ref, target_type="sandbox_gateway")
    try:
        remote = _require_service_client(client).gateway_disable(
            deployment_ref, envelope=_gateway_envelope(actor, gateway)
        )
        if remote.get("disabled") is not True:
            raise GatewayStateError("Gateway did not confirm service shutdown")
    except Exception as exc:
        registry._set_health(deployment_ref, "cleanup_pending")
        _audit(actor, action="plugins.mcp.gateway_cleanup_pending", task_id=deployment_ref,
               target_type="sandbox_gateway", success=False, error=_safe_error(exc))
        raise HTTPException(status_code=502, detail="Gateway disabled locally; remote cleanup pending") from exc
    registry._set_health(deployment_ref, "stopped")
    return SandboxGatewayResponse(gateway=registry.require(deployment_ref).to_dict())


@router.post("/gateways/{deployment_ref:path}/revoke", response_model=SandboxGatewayResponse)
def revoke_gateway(
    deployment_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
    client: SandboxServiceClient = Depends(get_sandbox_service_client),
):
    try:
        gateway = registry.revoke(deployment_ref, actor=actor.username)
    except (GatewayPolicyError, GatewayNotFoundError, GatewayStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="plugins.mcp.gateway_revoke", task_id=deployment_ref, target_type="sandbox_gateway")
    try:
        remote = _require_service_client(client).gateway_revoke(
            deployment_ref, envelope=_gateway_envelope(actor, gateway)
        )
        if remote.get("revoked") is not True:
            raise GatewayStateError("Gateway did not confirm service shutdown")
    except Exception as exc:
        registry._set_health(deployment_ref, "cleanup_pending")
        _audit(actor, action="plugins.mcp.gateway_cleanup_pending", task_id=deployment_ref,
               target_type="sandbox_gateway", success=False, error=_safe_error(exc))
        raise HTTPException(status_code=502, detail="Gateway revoked locally; remote cleanup pending") from exc
    registry._set_health(deployment_ref, "stopped")
    return SandboxGatewayResponse(gateway=registry.require(deployment_ref).to_dict())


@router.get("/gateways/{deployment_ref:path}/health")
def gateway_health(
    deployment_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
    client: SandboxServiceClient = Depends(get_sandbox_service_client),
):
    _require_gateway_enabled()
    client = _require_service_client(client)
    try:
        deployment = registry.require(deployment_ref)
        result = registry.health(
            deployment_ref,
            client=client,
            envelope=_gateway_envelope(actor, deployment),
        )
    except (GatewayPolicyError, GatewayNotFoundError, GatewayStateError, SandboxClientConfigurationError) as exc:
        raise HTTPException(status_code=502, detail="Gateway health is unavailable") from exc
    return result


@router.get("/gateways/{deployment_ref:path}/discover")
def discover_gateway(
    deployment_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
    client: SandboxServiceClient = Depends(get_sandbox_service_client),
):
    _require_gateway_enabled()
    client = _require_service_client(client)
    try:
        deployment = registry.require(deployment_ref)
        return registry.discover(
            deployment_ref,
            client=client,
            envelope=_gateway_envelope(actor, deployment),
        )
    except (GatewayPolicyError, GatewayNotFoundError, GatewayStateError, SandboxClientConfigurationError) as exc:
        raise HTTPException(status_code=502, detail="Gateway discovery is unavailable") from exc


@router.post("/gateways/{deployment_ref:path}/attach")
def attach_gateway(
    deployment_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
    client: SandboxServiceClient = Depends(get_sandbox_service_client),
):
    _require_gateway_enabled()
    client = _require_service_client(client)
    try:
        deployment = registry.require(deployment_ref)
        return registry.attach(
            deployment_ref,
            client=client,
            envelope=_gateway_envelope(actor, deployment),
        )
    except (GatewayPolicyError, GatewayNotFoundError, GatewayStateError, SandboxClientConfigurationError) as exc:
        raise HTTPException(status_code=502, detail="Gateway attach failed") from exc


@router.post(
    "/gateways/{deployment_ref:path}/subscribe",
    response_model=SandboxGatewaySubscriptionResponse,
)
def subscribe_gateway(
    deployment_ref: str,
    body: SandboxGatewaySubscriptionRequest,
    actor: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
    authorization: SandboxAuthorizationStore = Depends(get_sandbox_authorization_store),
):
    _require_gateway_enabled()
    effective = _context_capabilities(actor, auth, authorization)
    if "external_tool.invoke" not in effective:
        raise HTTPException(status_code=403, detail="external_tool.invoke capability is not authorized")
    if body.allow_write and "external_tool.write" not in effective:
        raise HTTPException(status_code=403, detail="external_tool.write capability is not authorized")
    try:
        subscription = registry.subscribe(
            deployment_ref,
            user_id=str(actor.id),
            department_id=str(actor.department_id or ""),
            allow_write=body.allow_write,
        )
    except GatewayPolicyError as exc:
        raise HTTPException(status_code=403, detail=str(exc)[:300]) from exc
    except (GatewayNotFoundError, GatewayStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="plugins.mcp.gateway_subscribe", task_id=deployment_ref, target_type="sandbox_gateway")
    return SandboxGatewaySubscriptionResponse(subscription=subscription)


@router.post("/gateways/{deployment_ref:path}/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe_gateway(
    deployment_ref: str,
    actor: AuthUser = Depends(current_user),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
):
    try:
        registry.unsubscribe(deployment_ref, user_id=str(actor.id))
    except (GatewayNotFoundError, GatewayStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="plugins.mcp.gateway_unsubscribe", task_id=deployment_ref, target_type="sandbox_gateway")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/gateways/{deployment_ref:path}/invoke")
def invoke_gateway(
    deployment_ref: str,
    body: SandboxGatewayInvokeRequest,
    actor: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
    authorization: SandboxAuthorizationStore = Depends(get_sandbox_authorization_store),
    registry: GatewayRegistry = Depends(get_sandbox_gateway_registry),
    client: SandboxServiceClient = Depends(get_sandbox_service_client),
):
    _require_gateway_enabled()
    client = _require_service_client(client)
    try:
        deployment = registry.require(deployment_ref)
        requested_capabilities = {"external_tool.invoke"}
        if isinstance(deployment.metadata, dict):
            write_tools = {str(item) for item in deployment.metadata.get("write_tools", ())}
            if body.tool_name in write_tools:
                requested_capabilities.add("external_tool.write")
        effective = _context_capabilities(actor, auth, authorization)
        if not requested_capabilities.issubset(effective):
            raise GatewayPolicyError("Gateway capability is not authorized for this principal")
        registry.authorize_invocation(
            deployment_ref,
            user_id=str(actor.id),
            department_id=str(actor.department_id or ""),
            tool_name=body.tool_name,
            arguments=body.arguments,
            capabilities=requested_capabilities,
            is_admin=actor.role == ROLE_SYSTEM_ADMIN,
        )
        envelope_capabilities = tuple(sorted(requested_capabilities))
        result = client.gateway_invoke(
            deployment_ref,
            body.tool_name,
            body.arguments,
            envelope=_gateway_envelope(actor, deployment, envelope_capabilities),
        )
    except GatewayPolicyError as exc:
        _audit(actor, action="plugins.mcp.gateway_invoke", task_id=deployment_ref, target_type="sandbox_gateway", success=False, error=str(exc))
        raise HTTPException(status_code=403, detail=str(exc)[:300]) from exc
    except (GatewayNotFoundError, GatewayStateError, SandboxClientConfigurationError) as exc:
        raise HTTPException(status_code=502, detail="Gateway invocation failed") from exc
    _audit(actor, action="plugins.mcp.gateway_invoke", task_id=deployment_ref, target_type="sandbox_gateway")
    return result


def _task_for_artifact(task_id: str, user: AuthUser, store: SandboxTaskStore):
    try:
        task = store.require(task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="sandbox task not found") from exc
    _require_task_visibility(task, user)
    return task


def _artifact_error(exc: Exception) -> None:
    if isinstance(exc, ArtifactNotFound):
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    if isinstance(exc, ArtifactAccessDenied):
        raise HTTPException(status_code=403, detail="artifact access denied") from exc
    if isinstance(exc, ArtifactStateConflict):
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    if isinstance(exc, ArtifactBrokerError):
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    raise HTTPException(status_code=400, detail="artifact request rejected") from exc


def _decode_artifact_body(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="content_base64 is invalid") from exc


def _require_runner_client(client: SandboxServiceClient | None) -> SandboxServiceClient:
    if client is None:
        raise HTTPException(status_code=503, detail="sandbox Runner identity provider is not configured")
    return client


def _runner_error(exc: Exception) -> None:
    if isinstance(exc, SandboxExecutionError):
        raise HTTPException(status_code=409, detail=_safe_error(exc)) from exc
    if isinstance(exc, SandboxPolicyError):
        raise HTTPException(status_code=409, detail=_safe_error(exc)) from exc
    raise HTTPException(status_code=503, detail="sandbox Runner request failed") from exc


@router.post("/artifacts", response_model=SandboxArtifactResponse)
def stage_artifact(
    body: SandboxArtifactStageRequest,
    user: AuthUser = Depends(require_employee),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    try:
        require_execution_enabled(operation="sandbox artifact staging")
    except SandboxPolicyError as exc:
        _audit(
            user,
            action="sandbox.artifact_received",
            task_id=body.task_id,
            target_type="sandbox_artifact",
            success=False,
            error="disabled",
        )
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    task = _task_for_artifact(body.task_id, user, store)
    if body.role != "input" and user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="employees may only stage input artifacts")
    content = _decode_artifact_body(body.content_base64)
    try:
        manifest = broker.stage(
            task_id=task.task_id,
            owner_id=task.owner_id,
            department_id=task.department_id,
            filename=body.filename,
            content=content,
            sha256=body.sha256,
            execution_id=body.execution_id,
            media_type=body.media_type,
            role=body.role,
            expires_at=body.expires_at,
            metadata=body.metadata,
        )
    except Exception as exc:  # noqa: BLE001
        _audit(
            user,
            action="sandbox.artifact_received",
            task_id=body.task_id,
            target_type="sandbox_artifact",
            success=False,
            error=str(exc),
        )
        _artifact_error(exc)
    _audit(
        user,
        action="sandbox.artifact_received",
        task_id=manifest.artifact_id,
        target_type="sandbox_artifact",
        metadata={"sha256": manifest.sha256, "role": manifest.role},
    )
    return SandboxArtifactResponse(artifact=manifest.to_dict())


@router.get("/tasks/{task_id}/artifacts", response_model=SandboxArtifactListResponse)
def list_artifacts(
    task_id: str,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    task = _task_for_artifact(task_id, user, store)
    # Listing is scoped to the task already authorized above; the broker never
    # exposes storage references or unrelated artifacts.
    manifests = broker.list_for_task(task.task_id)
    return SandboxArtifactListResponse(items=[manifest.to_dict() for manifest in manifests])


@router.post("/artifacts/{artifact_id}/scan", response_model=SandboxArtifactResponse)
def scan_artifact(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    try:
        manifest = broker.require(artifact_id)
        if user.role != ROLE_SYSTEM_ADMIN and (
            manifest.owner_id != str(user.id) or manifest.department_id != str(user.department_id or "")
        ):
            raise ArtifactAccessDenied()
        access_department = manifest.department_id if user.role == ROLE_SYSTEM_ADMIN else str(user.department_id or "")
        manifest = broker.scan(
            artifact_id,
            principal_id=str(user.id),
            department_id=access_department,
            task_id=manifest.task_id if user.role == ROLE_SYSTEM_ADMIN else None,
        )
    except Exception as exc:  # noqa: BLE001
        _artifact_error(exc)
    _audit(user, action="sandbox.artifact_scan", task_id=artifact_id, target_type="sandbox_artifact")
    return SandboxArtifactResponse(artifact=manifest.to_dict())


@router.post("/artifacts/{artifact_id}/commit", response_model=SandboxArtifactResponse)
def commit_artifact(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    try:
        manifest = broker.require(artifact_id)
        if user.role != ROLE_SYSTEM_ADMIN and (
            manifest.owner_id != str(user.id) or manifest.department_id != str(user.department_id or "")
        ):
            raise ArtifactAccessDenied()
        access_department = manifest.department_id if user.role == ROLE_SYSTEM_ADMIN else str(user.department_id or "")
        manifest = broker.commit(
            artifact_id,
            principal_id=str(user.id),
            department_id=access_department,
            task_id=manifest.task_id if user.role == ROLE_SYSTEM_ADMIN else None,
        )
    except Exception as exc:  # noqa: BLE001
        _artifact_error(exc)
    _audit(user, action="sandbox.artifact_confirm", task_id=artifact_id, target_type="sandbox_artifact")
    return SandboxArtifactResponse(artifact=manifest.to_dict())


@router.post("/artifacts/expire", response_model=SandboxArtifactListResponse)
def expire_artifacts(
    _actor: AuthUser = Depends(require_system_admin),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    expired = broker.expire()
    return SandboxArtifactListResponse(items=[{"artifact_id": item, "state": "deleted"} for item in expired])


@router.get("/artifacts/{artifact_id}")
def read_artifact(
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    try:
        manifest = broker.require(artifact_id)
        if user.role != ROLE_SYSTEM_ADMIN and (
            manifest.owner_id != str(user.id) or manifest.department_id != str(user.department_id or "")
        ):
            raise ArtifactAccessDenied()
        access_department = manifest.department_id if user.role == ROLE_SYSTEM_ADMIN else str(user.department_id or "")
        content = broker.read(
            artifact_id,
            principal_id=str(user.id),
            department_id=access_department,
            task_id=manifest.task_id if user.role == ROLE_SYSTEM_ADMIN else None,
        )
    except Exception as exc:  # noqa: BLE001
        _artifact_error(exc)
    _audit(
        user,
        action="sandbox.artifact_download",
        task_id=artifact_id,
        target_type="sandbox_artifact",
        metadata={"sha256": manifest.sha256},
    )
    return Response(
        content=content,
        media_type=manifest.media_type,
        headers={"Content-Disposition": f'attachment; filename="{manifest.filename}"'},
    )


@router.post("/tasks/{task_id}/submit", response_model=SandboxTaskResponse, status_code=status.HTTP_202_ACCEPTED)
def submit_task_to_runner(
    task_id: str,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
    client: SandboxServiceClient | None = Depends(get_sandbox_service_client),
):
    task = _task_for_artifact(task_id, user, store)
    client = _require_runner_client(client)
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=client)
    try:
        updated = adapter.submit(task.task_id)
    except Exception as exc:  # noqa: BLE001
        _audit(user, action="sandbox.task_start", task_id=task_id, success=False, error=str(exc))
        _runner_error(exc)
    _audit(user, action="sandbox.task_start", task_id=task_id)
    return SandboxTaskResponse(task=updated.to_dict())


@router.get("/tasks/{task_id}/observe", response_model=SandboxTaskResponse)
def observe_task_from_runner(
    task_id: str,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    client: SandboxServiceClient | None = Depends(get_sandbox_service_client),
):
    task = _task_for_artifact(task_id, user, store)
    client = _require_runner_client(client)
    try:
        updated = SandboxExecutionAdapter(store=store, client=client).observe(task.task_id)
    except Exception as exc:  # noqa: BLE001
        _runner_error(exc)
    return SandboxTaskResponse(task=updated.to_dict())


@router.post("/tasks/{task_id}/collect", response_model=SandboxRunnerResponse)
def collect_task_from_runner(
    task_id: str,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
    client: SandboxServiceClient | None = Depends(get_sandbox_service_client),
):
    task = _task_for_artifact(task_id, user, store)
    client = _require_runner_client(client)
    try:
        result = SandboxExecutionAdapter(
            store=store,
            catalog=catalog,
            broker=broker,
            client=client,
        ).collect(task.task_id)
    except Exception as exc:  # noqa: BLE001
        _runner_error(exc)
    _audit(user, action="sandbox.artifact_received", task_id=task_id, target_type="sandbox_task")
    return SandboxRunnerResponse(response=dict(result))


@router.post("/tasks/{task_id}/reconcile", response_model=SandboxTaskResponse)
def reconcile_task(
    task_id: str,
    user: AuthUser = Depends(require_system_admin),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    client: SandboxServiceClient | None = Depends(get_sandbox_service_client),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    _ = _task_for_artifact(task_id, user, store)
    # Local deadline/lease/artifact reconciliation remains available during a
    # shutdown or disabled-state rollout.  Remote observation is added only
    # when an authenticated Runner client is explicitly wired.
    reconciler = SandboxReconciler(store=store, client=client, broker=broker)
    report = reconciler.run_once()
    task = store.require(task_id)
    _audit(
        user,
        action="sandbox.task_reconcile",
        task_id=task_id,
        metadata=report.to_dict(),
    )
    return SandboxTaskResponse(task=task.to_dict())


@router.post("/tasks", response_model=SandboxTaskResponse, status_code=status.HTTP_202_ACCEPTED)
def create_task(
    body: SandboxTaskCreateRequest,
    user: AuthUser = Depends(require_employee),
    auth: AuthService = Depends(get_auth_service),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
    authorization: SandboxAuthorizationStore = Depends(get_sandbox_authorization_store),
):
    try:
        require_execution_enabled(operation="sandbox task admission")
    except SandboxPolicyError as exc:
        _audit(user, action="sandbox.task_reject", task_id=body.idempotency_key, success=False, error="disabled")
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    requested = {str(item).strip() for item in body.required_capabilities if str(item).strip()}
    unknown_requested = requested - SANDBOX_CAPABILITIES
    if unknown_requested:
        raise HTTPException(status_code=400, detail="unsupported sandbox capability")
    if "sandbox.execute" not in requested:
        raise HTTPException(status_code=403, detail="sandbox.execute capability is required")
    effective = _context_capabilities(user, auth, authorization)
    if not requested.issubset(effective):
        _audit(
            user,
            action="sandbox.task_reject",
            task_id=body.idempotency_key,
            success=False,
            error="capability_denied",
        )
        raise HTTPException(status_code=403, detail="requested sandbox capability is not authorized")
    if body.network_policy_ref and "sandbox.network" not in effective:
        raise HTTPException(status_code=403, detail="sandbox.network capability is not authorized")
    if body.network_policy_ref and "sandbox.network" not in requested:
        raise HTTPException(status_code=400, detail="sandbox.network must be declared for a network policy")
    if body.input_artifact_ids and "sandbox.internal_data" not in requested:
        raise HTTPException(status_code=400, detail="sandbox.internal_data must be declared for input artifacts")
    try:
        bundle = catalog.authorize(
            bundle_ref=body.bundle_ref,
            bundle_digest=body.bundle_digest,
            profile_ref=body.profile_ref,
            required_capabilities=requested,
            network_policy_ref=body.network_policy_ref,
            parameters=body.parameters,
        )
    except (BundleNotAuthorized, BundleNotFound) as exc:
        _audit(user, action="sandbox.task_reject", task_id=body.idempotency_key, success=False, error="bundle_not_authorized")
        raise HTTPException(status_code=409, detail="bundle/profile is not an enabled reviewed catalog entry") from exc
    expected_policy_hash = bundle.policy_snapshot_hash
    if body.policy_snapshot_hash and body.policy_snapshot_hash != expected_policy_hash:
        _audit(user, action="sandbox.task_reject", task_id=body.idempotency_key, success=False, error="policy_snapshot_mismatch")
        raise HTTPException(status_code=409, detail="policy snapshot does not match the reviewed bundle")
    department_id = user.department_id
    if department_id in (None, ""):
        raise HTTPException(status_code=403, detail="sandbox execution requires a department")
    cap_hash = capability_snapshot_hash(
        user_id=user.id,
        department_id=department_id,
        capabilities=requested,
        policy_snapshot_hash=expected_policy_hash,
    )
    for artifact_id in body.input_artifact_ids:
        try:
            broker.authorize_input(
                artifact_id,
                principal_id=str(user.id),
                department_id=str(department_id),
            )
        except Exception as exc:  # noqa: BLE001
            _audit(
                user,
                action="sandbox.task_reject",
                task_id=body.idempotency_key,
                success=False,
                error="input_artifact_denied",
            )
            if isinstance(exc, ArtifactAccessDenied):
                raise HTTPException(status_code=403, detail="input artifact access denied") from exc
            if isinstance(exc, ArtifactNotFound):
                raise HTTPException(status_code=404, detail="input artifact not found") from exc
            raise HTTPException(status_code=409, detail="input artifact is not committed") from exc
    spec = SandboxTaskSpec(
        idempotency_key=body.idempotency_key,
        owner_id=str(user.id),
        department_id=str(department_id),
        bundle_ref=body.bundle_ref,
        bundle_digest=body.bundle_digest,
        profile_ref=body.profile_ref,
        policy_snapshot_hash=expected_policy_hash,
        capability_snapshot_hash=cap_hash,
        kind=body.kind,
        network_policy_ref=body.network_policy_ref,
        required_capabilities=tuple(requested),
        input_artifact_ids=tuple(body.input_artifact_ids),
        parameters=body.parameters,
        deadline_at=body.deadline_at,
    )
    try:
        task = store.create(spec, actor_id=str(user.id))
    except Exception as exc:  # noqa: BLE001 - map to bounded HTTP errors
        _audit(user, action="sandbox.task_submit", task_id=body.idempotency_key, success=False, error=str(exc))
        _raise_store_error(exc)
    _audit(
        user,
        action="sandbox.task_submit",
        task_id=task.task_id,
        metadata={"kind": task.kind, "bundle_ref": task.bundle_ref},
    )
    return SandboxTaskResponse(task=task.to_dict())


@router.get("/tasks", response_model=SandboxTaskListResponse)
def list_tasks(
    statuses: list[str] | None = Query(default=None),
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
):
    kwargs: dict[str, Any] = {"statuses": statuses}
    if user.role != ROLE_SYSTEM_ADMIN:
        kwargs.update({"owner_id": str(user.id), "department_id": str(user.department_id or "")})
    try:
        tasks = store.list(**kwargs)
    except Exception as exc:  # noqa: BLE001
        _raise_store_error(exc)
    return SandboxTaskListResponse(items=[task.to_dict() for task in tasks])


@router.get("/tasks/{task_id}", response_model=SandboxTaskResponse)
def get_task(
    task_id: str,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
):
    try:
        task = store.require(task_id)
    except Exception as exc:  # noqa: BLE001
        _raise_store_error(exc)
    _require_task_visibility(task, user)
    return SandboxTaskResponse(task=task.to_dict())


@router.post("/tasks/{task_id}/cancel", response_model=SandboxTaskResponse, status_code=status.HTTP_202_ACCEPTED)
def cancel_task(
    task_id: str,
    body: SandboxCancelRequest | None = None,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    client: SandboxServiceClient | None = Depends(get_sandbox_service_client),
):
    try:
        current = store.require(task_id)
        _require_task_visibility(current, user)
        adapter = SandboxExecutionAdapter(store=store, client=client)
        task = adapter.cancel(
            task_id,
            actor_id=str(user.id),
            reason=(body.reason if body else "") or "user_requested",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_store_error(exc)
    _audit(user, action="sandbox.task_cancel", task_id=task_id)
    return SandboxTaskResponse(task=task.to_dict())


@router.get("/tasks/{task_id}/events", response_model=SandboxTaskEventsResponse)
def task_events(
    task_id: str,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
):
    try:
        task = store.require(task_id)
        _require_task_visibility(task, user)
        events = store.events(task_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_store_error(exc)
    return SandboxTaskEventsResponse(items=events)


__all__ = [
    "get_sandbox_authorization_store",
    "get_sandbox_catalog",
    "get_sandbox_task_store",
    "router",
]
