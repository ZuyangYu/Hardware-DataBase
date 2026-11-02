"""HDB-owned sandbox task admission and observation endpoints.

These routes manage a durable control-plane record only.  They never launch a
process, open a runtime socket, or accept a command/host path from a caller.
Actual execution is an explicitly configured external Runner concern.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

import src.settings as settings
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, require_employee, require_system_admin
from src.api.schemas import (
    SandboxCancelRequest,
    SandboxCapabilityGrantRequest,
    SandboxCapabilityGrantResponse,
    SandboxTaskCreateRequest,
    SandboxTaskInputBindRequest,
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
    SandboxSkillPackageUploadRequest,
    SandboxTextSkillPackageUploadRequest,
    SandboxPersonalTextSkillUploadRequest,
    SandboxPersonalExecutableSkillUploadRequest,
    SandboxPersonalSkillShareRequest,
    SandboxSkillBuildRequest,
    SandboxSkillPipSourceRequest,
    SandboxSkillExecutionBindingRequest,
    SandboxSkillSigningKeyRequest,
    SandboxSkillSigningKeyResponse,
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
from src.sandbox.protocol import RequestEnvelope, SandboxProtocolError
from src.sandbox.policy import SandboxPolicyError, require_execution_enabled
from src.sandbox.service import SandboxExecutionAdapter, SandboxExecutionError
from src.sandbox.reconciler import SandboxReconciler
from src.sandbox.skills import (
    SkillCatalog,
    SkillCatalogError,
    SkillNotAuthorized,
    SkillNotFound,
    SkillStateConflict,
)
from src.sandbox.skill_packages import (
    SkillPackageError, SkillPackageStore, validate_skill_package, validate_text_skill_package,
)
from src.sandbox.provenance import (
    SkillProvenanceError,
    SkillProvenanceStore,
    build_sbom,
    manifest_digest,
    validate_source_declaration,
    verify_signature,
)
from src.sandbox.schema import SchemaValidationError, normalize_schema
from src.sandbox.pip_sources import SkillPipSourceStore, SkillPipSourceError
from src.sandbox.task_store import (
    IdempotencyConflict,
    InvalidTransition,
    SandboxTaskStore,
    TaskConflict,
    TaskNotFound,
)


router = APIRouter(prefix="/sandbox", tags=["sandbox"])

_DIFF_TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json", ".svg", ".yaml", ".yml")
_MAX_DIFF_BYTES = 512 * 1024
_MAX_DIFF_LINES = 2_000
_MAX_TASK_LINEAGE = 8


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


def get_skill_package_store() -> SkillPackageStore:
    return SkillPackageStore()


def get_skill_pip_source_store() -> SkillPipSourceStore:
    return SkillPipSourceStore()


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


@router.post("/skills/packages", response_model=SandboxSkillResponse)
def upload_skill_package(
    body: SandboxSkillPackageUploadRequest,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
    packages: SkillPackageStore = Depends(get_skill_package_store),
    pip_sources: SkillPipSourceStore = Depends(get_skill_pip_source_store),
    bundles: SandboxCatalog = Depends(get_sandbox_catalog),
    client: SandboxServiceClient | None = Depends(get_sandbox_service_client),
):
    storage_ref = ""
    provenance_record: dict[str, Any] | None = None
    sbom: dict[str, Any] | None = None
    try:
        raw = base64.b64decode(body.package_base64, validate=True)
        package = validate_skill_package(raw)
        provenance_record, sbom = _package_provenance(
            package=package, skill_ref=body.skill_ref, version=body.version,
            source_kind=body.source_kind, source_ref=body.source_ref,
            source_commit=body.source_commit, signing_key_id=body.signing_key_id,
            signature=body.signature_base64, provided_sbom=body.sbom, catalog=catalog,
        )
        storage_ref = packages.save(body.skill_ref, package, raw)
        skill = catalog.register(
            skill_ref=body.skill_ref, version=body.version,
            content=package.skill_markdown,
            source_ref=str(provenance_record["source_ref"] if provenance_record else body.source_ref),
            catalog_source=body.catalog_source, department_id=body.department_id,
            actor=actor.username, metadata={
                **dict(body.metadata or {}), **dict(package.metadata),
                **_source_manifest_metadata(package),
            },
            package_storage_ref=storage_ref, package_archive_digest=package.archive_digest,
            package_files=package.files, package_review_flags=package.review_flags,
            package_build_state="uploaded",
        )
        _skill_provenance_store(catalog).put(
            skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            version=skill.version, record=provenance_record, sbom=sbom or {},
        )
        _record_skill_review(
            catalog, skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            action="source_uploaded", decision="pending", actor=actor,
            metadata={"signature_status": provenance_record["signature"]["status"],
                      "sbom_status": provenance_record["sbom"]["status"]},
        )
    except (ValueError, binascii.Error, SkillPackageError, SkillCatalogError, SkillProvenanceError) as exc:
        if storage_ref:
            packages.delete(storage_ref)
        _audit(actor, action="sandbox.skill_package_upload", task_id=body.skill_ref,
               target_type="sandbox_skill", success=False, error=str(exc))
        raise HTTPException(status_code=400, detail="Skill package is invalid") from exc
    _audit(actor, action="sandbox.skill_package_upload", task_id=skill.skill_ref,
           target_type="sandbox_skill", metadata={"archive_digest": package.archive_digest,
                                                   "source_kind": provenance_record["source_kind"] if provenance_record else "",
                                                   "signature_status": provenance_record["signature"]["status"] if provenance_record else "",
                                                   "sbom_status": provenance_record["sbom"]["status"] if provenance_record else ""})
    # Build is system-managed: upload leaves a durable draft even if the
    # Runner is unavailable; the administrator may retry the same build.
    return build_skill_package(
        skill.skill_ref,
        SandboxSkillBuildRequest(
            profile_ref="profile/dev-l1",
            runtime_policy_ref="profile/dev-l1",
            runtime_policy_digest=str(getattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "")),
            input_schema=body.input_schema,
            output_schema=body.output_schema,
        ),
        actor=actor, skills=catalog, packages=packages, catalog=bundles, client=client,
        pip_sources=pip_sources,
    )


@router.post("/skills/text-packages", response_model=SandboxSkillResponse)
def upload_text_skill_package(
    body: SandboxTextSkillPackageUploadRequest,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
    packages: SkillPackageStore = Depends(get_skill_package_store),
):
    """Register a reviewable text draft from SKILL.md and bundled Markdown."""
    storage_ref = ""
    provenance_record: dict[str, Any] | None = None
    sbom: dict[str, Any] | None = None
    try:
        raw = base64.b64decode(body.package_base64, validate=True)
        package = validate_text_skill_package(raw)
        provenance_record, sbom = _package_provenance(
            package=package, skill_ref=package.skill_ref, version=package.version,
            source_kind="direct_upload", source_ref="", source_commit="", catalog=catalog,
        )
        storage_ref = packages.save(package.skill_ref, package, raw)
        skill = catalog.register(
            skill_ref=package.skill_ref, version=package.version,
            content=package.content, department_id=body.department_id,
            actor=actor.username,
            metadata={**dict(package.metadata), **_source_manifest_metadata(package),
                      "skill_name": package.skill_ref.removeprefix("skill/"),
                      "summary": package.description[:500]},
            text_storage_ref=storage_ref, text_archive_digest=package.archive_digest,
            text_files=package.files,
        )
        _skill_provenance_store(catalog).put(
            skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            version=skill.version, record=provenance_record, sbom=sbom or {},
        )
        _record_skill_review(
            catalog, skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            action="source_uploaded", decision="pending", actor=actor,
            metadata={"signature_status": provenance_record["signature"]["status"],
                      "sbom_status": provenance_record["sbom"]["status"]},
        )
    except (ValueError, binascii.Error, SkillPackageError, SkillCatalogError, SkillProvenanceError) as exc:
        if storage_ref:
            packages.delete(storage_ref)
        _audit(actor, action="sandbox.text_skill_package_upload", target_type="sandbox_skill",
               success=False, error=str(exc))
        raise HTTPException(status_code=400, detail="text Skill package is invalid or conflicts with an enabled Skill") from exc
    _audit(actor, action="sandbox.text_skill_package_upload", task_id=skill.skill_ref,
           target_type="sandbox_skill", metadata={"archive_digest": package.archive_digest,
                                                   "file_count": len(package.files),
                                                   "sbom_status": provenance_record["sbom"]["status"] if provenance_record else ""})
    return SandboxSkillResponse(skill=_skill_provenance_payload(skill, catalog))


def _personal_skill_view(skill) -> dict[str, Any]:
    return {
        "skill_ref": skill.skill_ref, "version": skill.version,
        "approval_state": skill.approval_state, "share_scope": skill.share_scope,
        "kind": "executable" if skill.package_archive_digest else "text",
        "package_build_state": skill.package_build_state,
        "summary": str(skill.metadata.get("summary") or ""),
        "department_id": skill.department_id,
        "created_at": skill.created_at,
    }


@router.post("/skills/personal/text-packages", status_code=status.HTTP_201_CREATED)
def upload_personal_text_skill(
    body: SandboxPersonalTextSkillUploadRequest,
    user: AuthUser = Depends(require_employee),
    catalog: SkillCatalog = Depends(get_skill_catalog),
    packages: SkillPackageStore = Depends(get_skill_package_store),
):
    """Create a private draft; only an admin can review, enable or read the source."""
    if not user.department_id:
        raise HTTPException(status_code=403, detail="employee department is required")
    try:
        raw = base64.b64decode(body.package_base64, validate=True)
        package = validate_text_skill_package(raw)
    except (ValueError, binascii.Error, SkillPackageError) as exc:
        _audit(user, action="sandbox.personal_skill_upload", target_type="sandbox_skill",
               success=False, error=str(exc))
        raise HTTPException(status_code=400, detail="text Skill ZIP is invalid") from exc
    skill_ref = f"skill/personal/{user.id}/{package.skill_ref.removeprefix('skill/')}"
    if catalog.get(skill_ref) is not None:
        raise HTTPException(status_code=409, detail="personal Skill name already exists")
    storage_ref = ""
    try:
        provenance_record, sbom = _package_provenance(
            package=package, skill_ref=skill_ref, version=package.version,
            source_kind="direct_upload", source_ref="", source_commit="", catalog=catalog,
        )
        storage_ref = packages.save(skill_ref, package, raw)
        skill = catalog.register(
            skill_ref=skill_ref, version=package.version, content=package.content,
            catalog_source="employee", owner_id=user.id, department_id=user.department_id,
            actor=user.username, metadata={**dict(package.metadata), **_source_manifest_metadata(package),
                                           "skill_name": package.skill_ref.removeprefix("skill/"),
                                           "summary": package.description[:500]},
            text_storage_ref=storage_ref, text_archive_digest=package.archive_digest,
            text_files=package.files, allow_overwrite=False,
        )
        _skill_provenance_store(catalog).put(
            skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            version=skill.version, record=provenance_record, sbom=sbom,
        )
        _record_skill_review(
            catalog, skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            action="source_uploaded", decision="pending", actor=user,
            metadata={"signature_status": provenance_record["signature"]["status"],
                      "sbom_status": provenance_record["sbom"]["status"]},
        )
    except SkillStateConflict as exc:
        if storage_ref:
            packages.delete(storage_ref)
        raise HTTPException(status_code=409, detail="personal Skill name already exists") from exc
    except (SkillCatalogError, SkillPackageError, SkillProvenanceError) as exc:
        if storage_ref:
            packages.delete(storage_ref)
        _audit(user, action="sandbox.personal_skill_upload", task_id=skill_ref,
               target_type="sandbox_skill", success=False, error=str(exc))
        raise HTTPException(status_code=400, detail="text Skill ZIP is invalid") from exc
    _audit(user, action="sandbox.personal_skill_upload", task_id=skill_ref,
           target_type="sandbox_skill", metadata={"archive_digest": package.archive_digest})
    return {"skill": _personal_skill_view(skill)}


@router.post("/skills/personal/packages", status_code=status.HTTP_201_CREATED)
def upload_personal_executable_skill(
    body: SandboxPersonalExecutableSkillUploadRequest,
    user: AuthUser = Depends(require_employee),
    catalog: SkillCatalog = Depends(get_skill_catalog),
    packages: SkillPackageStore = Depends(get_skill_package_store),
):
    """Store employee code as a private draft; never build on employee request."""
    if not user.department_id:
        raise HTTPException(status_code=403, detail="employee department is required")
    try:
        raw = base64.b64decode(body.package_base64, validate=True)
        package = validate_skill_package(raw)
        input_schema = normalize_schema(body.input_schema, field_name="input_schema")
        output_schema = normalize_schema(body.output_schema, field_name="output_schema")
        if input_schema.get("type") != "object" or input_schema.get("additionalProperties") is not False:
            raise SkillPackageError("employee Skill input schema must be a closed object")
    except (ValueError, binascii.Error, SkillPackageError, SchemaValidationError) as exc:
        _audit(user, action="sandbox.personal_skill_package_upload", target_type="sandbox_skill",
               success=False, error=str(exc))
        raise HTTPException(status_code=400, detail="executable Skill ZIP or schema is invalid") from exc
    skill_ref = f"skill/personal/{user.id}/{body.skill_name}"
    if catalog.get(skill_ref) is not None:
        raise HTTPException(status_code=409, detail="personal Skill name already exists")
    storage_ref = ""
    try:
        provenance_record, sbom = _package_provenance(
            package=package, skill_ref=skill_ref, version=body.version,
            source_kind="direct_upload", source_ref="", source_commit="", catalog=catalog,
        )
        storage_ref = packages.save(skill_ref, package, raw)
        summary = str(package.metadata.get("summary") or "") or next(
            (line.lstrip("# ").strip() for line in package.skill_markdown.splitlines() if line.strip()), ""
        )
        skill = catalog.register(
            skill_ref=skill_ref, version=body.version, content=package.skill_markdown,
            catalog_source="employee", owner_id=user.id, department_id=user.department_id,
            actor=user.username, metadata={**dict(package.metadata), **_source_manifest_metadata(package),
                                           "skill_name": body.skill_name, "summary": summary[:500],
                                           "submitted_input_schema": input_schema,
                                           "submitted_output_schema": output_schema},
            package_storage_ref=storage_ref, package_archive_digest=package.archive_digest,
            package_files=package.files, package_review_flags=package.review_flags,
            package_build_state="uploaded", allow_overwrite=False,
        )
        _skill_provenance_store(catalog).put(
            skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            version=skill.version, record=provenance_record, sbom=sbom,
        )
        _record_skill_review(
            catalog, skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            action="source_uploaded", decision="pending", actor=user,
            metadata={"signature_status": provenance_record["signature"]["status"],
                      "sbom_status": provenance_record["sbom"]["status"]},
        )
    except SkillStateConflict as exc:
        if storage_ref:
            packages.delete(storage_ref)
        raise HTTPException(status_code=409, detail="personal Skill name already exists") from exc
    except (SkillCatalogError, SkillPackageError, SkillProvenanceError) as exc:
        if storage_ref:
            packages.delete(storage_ref)
        _audit(user, action="sandbox.personal_skill_package_upload", task_id=skill_ref,
               target_type="sandbox_skill", success=False, error=str(exc))
        raise HTTPException(status_code=400, detail="executable Skill ZIP or schema is invalid") from exc
    _audit(user, action="sandbox.personal_skill_package_upload", task_id=skill_ref,
           target_type="sandbox_skill", metadata={"archive_digest": package.archive_digest})
    return {"skill": _personal_skill_view(skill)}


@router.get("/skills/mine")
def list_personal_skills(
    user: AuthUser = Depends(require_employee),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    return {"items": [_personal_skill_view(skill) for skill in catalog.list_owned(user.id)]}


@router.post("/skills/personal/{skill_ref:path}/share")
def share_personal_skill(
    skill_ref: str,
    body: SandboxPersonalSkillShareRequest,
    user: AuthUser = Depends(require_employee),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    try:
        skill = catalog.set_department_share(
            skill_ref, principal_id=user.id, department_id=user.department_id or "",
            share=body.share_to_department,
        )
    except (SkillNotFound, SkillNotAuthorized) as exc:
        raise HTTPException(status_code=404, detail="personal Skill is not available") from exc
    except SkillStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(user, action="sandbox.personal_skill_share", task_id=skill_ref,
           target_type="sandbox_skill", metadata={"share_scope": skill.share_scope})
    return {"skill": _personal_skill_view(skill)}


@router.post("/skills/{skill_ref:path}/approve-source", response_model=SandboxSkillResponse)
def approve_personal_skill_source(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    skills: SkillCatalog = Depends(get_skill_catalog),
    packages: SkillPackageStore = Depends(get_skill_package_store),
):
    """Administrator confirms source review before the Runner may build it."""
    try:
        skill = skills.require(skill_ref)
        package = validate_skill_package(packages.read(skill.package_storage_ref))
        if package.archive_digest != skill.package_archive_digest:
            raise SkillStateConflict("Skill source changed before review")
        skill = skills.approve_source_for_build(skill_ref, archive_digest=package.archive_digest,
                                                actor=actor.username)
        _set_provenance_review(
            skills, skill, actor=actor, state="source_reviewed", decision="approved",
            action="source_review", metadata={"archive_digest": package.archive_digest},
        )
    except (SkillCatalogError, SkillPackageError, SkillProvenanceError) as exc:
        _audit(actor, action="sandbox.skill_source_approve", task_id=skill_ref,
               target_type="sandbox_skill", success=False, error=str(exc))
        raise HTTPException(status_code=409, detail="employee Skill source is not eligible for build") from exc
    _audit(actor, action="sandbox.skill_source_approve", task_id=skill_ref,
           target_type="sandbox_skill", metadata={"archive_digest": skill.package_archive_digest})
    return SandboxSkillResponse(skill=_skill_provenance_payload(skill, skills))


@router.post("/skills/{skill_ref:path}/build", response_model=SandboxSkillResponse)
def build_skill_package(
    skill_ref: str,
    body: SandboxSkillBuildRequest,
    actor: AuthUser = Depends(require_system_admin),
    skills: SkillCatalog = Depends(get_skill_catalog),
    packages: SkillPackageStore = Depends(get_skill_package_store),
    catalog: SandboxCatalog = Depends(get_sandbox_catalog),
    client: SandboxServiceClient | None = Depends(get_sandbox_service_client),
    pip_sources: SkillPipSourceStore = Depends(get_skill_pip_source_store),
):
    """Ask the independent Runner to build a fixed image from an uploaded package."""
    try:
        skill = skills.require(skill_ref)
        if skill.approval_state != "draft" or not skill.package_storage_ref:
            raise SkillStateConflict("Skill package must be an uploaded draft")
        if skill.catalog_source == "employee" and skill.package_build_state != "source_reviewed":
            raise SkillStateConflict("employee Skill source must be approved before build")
        package = validate_skill_package(packages.read(skill.package_storage_ref))
        if package.archive_digest != skill.package_archive_digest:
            raise SkillStateConflict("Skill package digest changed")
        _enforce_source_runtime_gate(catalog, skill, body.profile_ref)
        if not client:
            raise SandboxClientConfigurationError("Runner client is not configured")
        dependency_index_url = pip_sources.get() if package.requirements_declared else ""
        if package.requirements_declared and not dependency_index_url:
            raise SkillPipSourceError("administrator must configure a reviewed pip index first")
        runtime_policy_ref = body.runtime_policy_ref or body.profile_ref
        runtime_policy_digest = body.runtime_policy_digest or str(
            getattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "")
        )
        result = client.skill_build(
            skill_ref=skill.skill_ref, package_digest=package.archive_digest,
            package_bytes=packages.read(skill.package_storage_ref),
            base_bundle_ref=body.base_bundle_ref, profile_ref=body.profile_ref,
            runtime_policy_ref=runtime_policy_ref,
            runtime_policy_digest=runtime_policy_digest,
            input_schema=body.input_schema, output_schema=body.output_schema,
            required_capabilities=body.required_capabilities,
            dependency_index_url=dependency_index_url,
            envelope=_runner_envelope(actor, capabilities=tuple(body.required_capabilities)),
        )
        if str(result.get("package_digest") or "") != package.archive_digest:
            raise SkillStateConflict("Runner returned a different Skill package digest")
        if str(result.get("profile_ref") or "") != body.profile_ref:
            raise SkillStateConflict("Runner returned a different runtime profile")
        image_digest = str(result.get("image_digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
            raise SkillStateConflict("Runner did not return a fixed image identity")
        sbom_ref, signature_ref, source_provenance = _bundle_provenance_refs(
            skills, skill, package.archive_digest,
        )
        bundle = catalog.register(
            bundle_ref=str(result.get("bundle_ref") or ""),
            bundle_digest=str(result.get("bundle_digest") or ""),
            profile_ref=body.profile_ref, required_capabilities=body.required_capabilities,
            input_schema=body.input_schema, output_schema=body.output_schema,
            sbom_ref=sbom_ref, signature_ref=signature_ref,
            runtime_policy_ref=runtime_policy_ref,
            runtime_policy_digest=runtime_policy_digest,
            actor=actor.username, metadata={"skill_ref": skill.skill_ref, "package_digest": package.archive_digest,
                                             "image_digest": image_digest,
                                             "source_provenance": source_provenance},
        )
        skill = skills.mark_package_built(skill.skill_ref, archive_digest=package.archive_digest, actor=actor.username)
        skill = skills.bind_execution(skill.skill_ref, bundle_ref=bundle.bundle_ref,
                                      bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref,
                                      actor=actor.username, require_enabled=False)
        _set_provenance_review(
            skills, skill, actor=actor, state="built", decision="build_completed",
            action="bundle_build", metadata={"bundle_ref": bundle.bundle_ref, "bundle_digest": bundle.bundle_digest},
        )
    except (SkillCatalogError, SkillPackageError, SkillPipSourceError, BundleCatalogError,
            SandboxProtocolError, SkillProvenanceError) as exc:
        _audit(actor, action="sandbox.skill_package_build", task_id=skill_ref,
               target_type="sandbox_skill", success=False, error=str(exc))
        raise HTTPException(status_code=409, detail="Skill package build failed; uploaded source is retained for retry") from exc
    _audit(actor, action="sandbox.skill_package_build", task_id=skill_ref,
           target_type="sandbox_skill", metadata={"bundle_ref": skill.execution_bundle_ref})
    return SandboxSkillResponse(skill=_skill_provenance_payload(skill, skills))


@router.get("/skill-dependencies")
def get_skill_dependency_source(
    actor: AuthUser = Depends(require_system_admin),
    sources: SkillPipSourceStore = Depends(get_skill_pip_source_store),
):
    _ = actor
    return {"index_url": sources.get(),
            "allowed_hosts": str(getattr(settings, "SANDBOX_SKILL_PIP_ALLOWED_HOSTS", "pypi.org"))}


@router.put("/skill-dependencies")
def set_skill_dependency_source(
    body: SandboxSkillPipSourceRequest,
    actor: AuthUser = Depends(require_system_admin),
    sources: SkillPipSourceStore = Depends(get_skill_pip_source_store),
):
    try:
        index_url = sources.set(body.index_url)
    except SkillPipSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, action="sandbox.skill_dependency_source", target_type="sandbox_skill",
           metadata={"index_host": index_url.split("/")[2] if index_url else ""})
    return {"index_url": index_url}


@router.get("/skills", response_model=SandboxSkillListResponse)
def list_skills(
    _actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    return SandboxSkillListResponse(
        items=[_skill_provenance_payload(item, catalog, include_reviews=True) for item in catalog.list(include_revoked=True)]
    )


def _key_view(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "key_id": str(value.get("key_id") or ""),
        "algorithm": str(value.get("algorithm") or ""),
        "fingerprint": str(value.get("fingerprint") or ""),
        "state": str(value.get("state") or ""),
        "created_by": str(value.get("created_by") or ""),
        "created_at": str(value.get("created_at") or ""),
        "revoked_by": str(value.get("revoked_by") or ""),
        "revoked_at": str(value.get("revoked_at") or ""),
    }


@router.post("/skill-signing-keys", response_model=SandboxSkillSigningKeyResponse)
def register_skill_signing_key(
    body: SandboxSkillSigningKeyRequest,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    try:
        key = _skill_provenance_store(catalog).register_key(
            key_id=body.key_id, public_key=body.public_key, actor=actor.username,
        )
    except SkillProvenanceError as exc:
        _audit(actor, action="sandbox.skill_signing_key_register", target_type="sandbox_signing_key",
               target_id=body.key_id, success=False, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    _audit(actor, action="sandbox.skill_signing_key_register", target_type="sandbox_signing_key",
           target_id=body.key_id, metadata={"fingerprint": key.get("fingerprint", "")})
    return SandboxSkillSigningKeyResponse(key=_key_view(key))


@router.get("/skill-signing-keys")
def list_skill_signing_keys(
    _actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    return {"items": [_key_view(item) for item in _skill_provenance_store(catalog).list_keys(include_revoked=True)]}


@router.post("/skill-signing-keys/{key_id}/revoke", response_model=SandboxSkillSigningKeyResponse)
def revoke_skill_signing_key(
    key_id: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    try:
        key = _skill_provenance_store(catalog).revoke_key(key_id, actor=actor.username)
    except SkillProvenanceError as exc:
        _audit(actor, action="sandbox.skill_signing_key_revoke", target_type="sandbox_signing_key",
               target_id=key_id, success=False, error=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    _audit(actor, action="sandbox.skill_signing_key_revoke", target_type="sandbox_signing_key",
           target_id=key_id)
    return SandboxSkillSigningKeyResponse(key=_key_view(key))


def _source_manifest_metadata(package: Any) -> dict[str, Any]:
    items = [dict(item) for item in (getattr(package, "source_manifest", ()) or ())][:64]
    if not items:
        return {}
    digest = manifest_digest(getattr(package, "source_manifest", ()))
    return {
        "source_manifest": items,
        "source_manifest_digest": digest,
        "source_manifest_truncated": len(getattr(package, "source_manifest", ()) or ()) > len(items),
    }


def _skill_provenance_store(catalog: Any) -> SkillProvenanceStore:
    return SkillProvenanceStore(str(getattr(catalog, "db_path", settings.SANDBOX_TASK_DB_PATH)))


def _skill_provenance_payload(skill: Any, catalog: Any, *, include_reviews: bool = False) -> dict[str, Any]:
    payload = skill.to_dict()
    package_digest = str(skill.package_archive_digest or skill.text_archive_digest or "")
    if package_digest:
        store = _skill_provenance_store(catalog)
        item = store.get(skill.skill_ref, package_digest)
        if item is not None:
            payload["provenance"] = item.to_dict()
            if include_reviews:
                payload["review_records"] = store.reviews(skill.skill_ref, package_digest)
    return payload


def _package_provenance(
    *,
    package: Any,
    skill_ref: str,
    version: str,
    source_kind: str,
    source_ref: str,
    source_commit: str,
    signing_key_id: str = "",
    signature: str = "",
    provided_sbom: Mapping[str, Any] | None = None,
    catalog: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    declaration = validate_source_declaration(
        source_kind=source_kind, source_ref=source_ref, source_commit=source_commit,
    )
    store = _skill_provenance_store(catalog)
    trusted_key = store.get_key(signing_key_id, active_only=True) if signing_key_id else None
    signature_record = verify_signature(
        package_digest=package.archive_digest,
        source_ref=declaration["source_ref"],
        source_commit=declaration["source_commit"],
        signing_key_id=signing_key_id,
        signature=signature,
        trusted_public_key=str(trusted_key.get("public_key") or "") if trusted_key else None,
    )
    sbom, sbom_summary = build_sbom(
        package_digest=package.archive_digest,
        source_manifest=package.source_manifest,
        dependencies=getattr(package, "dependencies", ()),
        provided=provided_sbom,
    )
    record = {
        "schema_version": "hdb-skill-provenance-v1",
        "source_kind": declaration["source_kind"],
        "source_ref": declaration["source_ref"],
        "source_ref_status": "declared_not_fetched" if declaration["source_ref"] else "not_provided",
        "source_commit": declaration["source_commit"],
        "package_digest": package.archive_digest,
        "source_manifest_digest": manifest_digest(package.source_manifest),
        "source_manifest_count": len(package.source_manifest),
        "signature": signature_record,
        "sbom": {
            **sbom_summary,
            "ref": f"hdb://skill-sbom/{package.archive_digest}",
        },
        "review": {
            "state": "pending",
            "decision": "not_reviewed",
            "reviewed_by": "",
            "reviewed_at": "",
        },
        # Unattested or model-generated source remains on the lowest reviewed
        # runtime profile.  This is a gate, not a claim that the source is safe.
        "runtime_gate": {
            "minimum_profile": "profile/dev-l1",
            "reason": (
                "model_generated" if declaration["source_kind"] == "model_generated"
                else "signature_verified" if signature_record["status"] == "verified"
                else "source_not_attested"
            ),
        },
        "skill_ref": skill_ref,
        "version": version,
    }
    return record, sbom


def _record_skill_review(
    catalog: Any,
    *,
    skill_ref: str,
    package_digest: str,
    action: str,
    decision: str,
    actor: AuthUser,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if not package_digest:
        return
    try:
        _skill_provenance_store(catalog).record_review(
            skill_ref=skill_ref, package_digest=package_digest, action=action,
            decision=decision, actor_id=actor.id, actor_name=actor.username,
            reason=reason, metadata=metadata,
        )
    except (SkillProvenanceError, OSError, sqlite3.Error):
        # The immutable audit event below is the minimum fallback.  A review
        # action must not turn a successfully persisted Skill state into a
        # 500 merely because the optional projection is unavailable.
        pass


def _set_provenance_review(
    catalog: Any,
    skill: Any,
    *,
    actor: AuthUser,
    state: str,
    decision: str,
    action: str,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    package_digest = str(skill.package_archive_digest or skill.text_archive_digest or "")
    if not package_digest:
        return
    try:
        store = _skill_provenance_store(catalog)
        item = store.get(skill.skill_ref, package_digest)
        if item is not None:
            record = dict(item.record)
            record["review"] = {
                "state": state,
                "decision": decision,
                "reviewed_by": actor.username,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            }
            store.put(
                skill_ref=skill.skill_ref, package_digest=package_digest,
                version=skill.version, record=record, sbom=item.sbom,
            )
        _record_skill_review(
            catalog, skill_ref=skill.skill_ref, package_digest=package_digest,
            action=action, decision=decision, actor=actor, reason=reason, metadata=metadata,
        )
    except (SkillProvenanceError, OSError, sqlite3.Error):
        pass


def _enforce_source_runtime_gate(catalog: Any, skill: Any, profile_ref: str) -> None:
    """Keep unverified and model-generated source on the restricted profile."""

    if str(profile_ref) == "profile/dev-l1":
        return
    package_digest = str(skill.package_archive_digest or "")
    item = _skill_provenance_store(catalog).get(skill.skill_ref, package_digest) if package_digest else None
    record = dict(item.record) if item is not None else {}
    signature = dict(record.get("signature") or {})
    if signature.get("status") != "verified":
        raise SkillStateConflict("unverified Skill source is restricted to profile/dev-l1")
    if str(record.get("source_kind") or "") == "model_generated":
        raise SkillStateConflict("model-generated Skill source is restricted to profile/dev-l1")


def _bundle_provenance_refs(catalog: Any, skill: Any, package_digest: str) -> tuple[str, str, dict[str, Any]]:
    item = _skill_provenance_store(catalog).get(skill.skill_ref, package_digest)
    if item is None:
        return "", "", {}
    record = dict(item.record)
    sbom = dict(record.get("sbom") or {})
    signature = dict(record.get("signature") or {})
    signature_ref = ""
    if signature.get("status") == "verified" and signature.get("signature_digest"):
        signature_ref = f"hdb://skill-signature/{signature['signature_digest']}"
    return str(sbom.get("ref") or ""), signature_ref, record


def _skill_directory_metadata(skill: Any) -> dict[str, Any]:
    """Return only review metadata intended for the employee catalog."""
    source = dict(getattr(skill, "metadata", {}) or {})
    result: dict[str, Any] = {}
    for key in ("skill_name", "summary", "risk", "declared_version"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:500]
    for key in ("intent_examples", "input_kinds", "output_kinds", "interaction_modes", "side_effects"):
        value = source.get(key)
        if isinstance(value, (list, tuple)):
            result[key] = [str(item).strip()[:160] for item in value if str(item).strip()][:32]
    return result


def _skill_runtime_view(bundle: Any | None) -> dict[str, Any] | None:
    if bundle is None:
        return None
    from src.sandbox.runtime_contract import RUNTIME_CONTRACT_VERSION

    return {
        "profile_ref": str(bundle.profile_ref or ""),
        "contract_version": RUNTIME_CONTRACT_VERSION,
        "runtime_policy_ref": str(bundle.runtime_policy_ref or ""),
        "runtime_policy_digest": str(bundle.runtime_policy_digest or ""),
        "required_capabilities": list(bundle.required_capabilities),
        "network_policy_ref": str(bundle.network_policy_ref or ""),
        "bundle_digest": str(bundle.bundle_digest or ""),
        "sbom_ref": str(bundle.sbom_ref or ""),
        "signature_ref": str(bundle.signature_ref or ""),
    }


@router.get("/skills/{skill_ref:path}/provenance")
def get_skill_provenance(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    try:
        skill = catalog.require(skill_ref)
        package_digest = str(skill.package_archive_digest or skill.text_archive_digest or "")
        item = _skill_provenance_store(catalog).get(skill.skill_ref, package_digest) if package_digest else None
        if item is None:
            raise SkillProvenanceError("Skill provenance is unavailable")
    except (SkillNotFound, SkillProvenanceError) as exc:
        raise HTTPException(status_code=404, detail="Skill provenance is unavailable") from exc
    _audit(actor, action="sandbox.skill_provenance_read", task_id=skill_ref,
           target_type="sandbox_skill", metadata={"package_digest": package_digest})
    return {
        "provenance": item.to_dict(),
        "sbom": dict(item.sbom),
        "reviews": _skill_provenance_store(catalog).reviews(skill_ref, package_digest),
    }


@router.get("/skills/{skill_ref:path}/sbom")
def get_skill_sbom(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
):
    payload = get_skill_provenance(skill_ref, actor=actor, catalog=catalog)
    return {"sbom": payload["sbom"], "package_digest": payload["provenance"]["package_digest"]}


@router.get("/skills/{skill_ref:path}/source")
def download_skill_source(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    skills: SkillCatalog = Depends(get_skill_catalog),
    packages: SkillPackageStore = Depends(get_skill_package_store),
):
    try:
        skill = skills.require(skill_ref)
        storage_ref = skill.text_storage_ref or skill.package_storage_ref
        archive_digest = skill.text_archive_digest or skill.package_archive_digest
        raw = packages.read(storage_ref)
        if hashlib.sha256(raw).hexdigest() != archive_digest:
            raise SkillPackageError("Skill source changed")
    except (SkillCatalogError, SkillPackageError) as exc:
        raise HTTPException(status_code=404, detail="Skill source is unavailable") from exc
    _audit(actor, action="sandbox.skill_source_read", task_id=skill_ref, target_type="sandbox_skill")
    return Response(content=raw, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="skill-source.zip"'})


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
        executable_available = True
    except SandboxPolicyError:
        executable_available = False
    effective: set[str] | None = None
    items: list[dict[str, Any]] = []
    for skill in skills.list(enabled_only=True):
        if not skill.execution_bundle_ref:
            if not skills._department_allowed(skill, user.department_id or ""):
                continue
            if not skills._principal_allowed(skill, user.id, user.department_id or ""):
                continue
            metadata = _skill_directory_metadata(skill)
            items.append({
                "skill_ref": skill.skill_ref, "version": skill.version, "bundle_ref": "",
                "input_schema": {}, "output_schema": {}, "runtime": None,
                "metadata": metadata, "subscribed": skills.is_subscribed(skill.skill_ref, user.id),
                "description": skills.content(skill.skill_ref)[:2000], "kind": "text",
                "is_owner": skill.owner_id == str(user.id) and skill.catalog_source == "employee",
                "share_scope": skill.share_scope if skill.catalog_source == "employee" else "",
            })
            continue
        if not executable_available:
            continue
        if effective is None:
            effective = _context_capabilities(user, auth, authorization)
        try:
            bundle = skills.execution_bundle(
                skill.skill_ref, principal_id=user.id, department_id=user.department_id or "",
            )
        except SkillCatalogError:
            continue
        if not set(bundle.required_capabilities).issubset(effective):
            continue
        metadata = _skill_directory_metadata(skill)
        items.append({
            "skill_ref": skill.skill_ref,
            "version": skill.version,
            "bundle_ref": bundle.bundle_ref,
            "input_schema": dict(bundle.input_schema),
            "output_schema": dict(bundle.output_schema),
            "runtime": _skill_runtime_view(bundle),
            "metadata": metadata,
            "subscribed": skills.is_subscribed(skill.skill_ref, user.id),
            "description": skills.content(skill.skill_ref)[:2000],
            "kind": "executable",
            "is_owner": skill.owner_id == str(user.id) and skill.catalog_source == "employee",
            "share_scope": skill.share_scope if skill.catalog_source == "employee" else "",
        })
    return {"items": items}


@router.post("/skills/{skill_ref:path}/subscribe")
def subscribe_skill(
    skill_ref: str,
    user: AuthUser = Depends(require_employee),
    skills: SkillCatalog = Depends(get_skill_catalog),
):
    try:
        skills.subscribe(skill_ref, principal_id=user.id, department_id=user.department_id or "")
    except SkillCatalogError as exc:
        raise HTTPException(status_code=403, detail="Skill is not available") from exc
    _audit(user, action="sandbox.skill_subscribe", task_id=skill_ref, target_type="sandbox_skill")
    return {"subscribed": True}


@router.delete("/skills/{skill_ref:path}/subscribe")
def unsubscribe_skill(
    skill_ref: str,
    user: AuthUser = Depends(require_employee),
    skills: SkillCatalog = Depends(get_skill_catalog),
):
    skills.unsubscribe(skill_ref, principal_id=user.id)
    _audit(user, action="sandbox.skill_unsubscribe", task_id=skill_ref, target_type="sandbox_skill")
    return {"subscribed": False}


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
    _set_provenance_review(
        catalog, skill, actor=actor, state=state, decision=state,
        action=f"skill_{state}",
    )
    _audit(actor, action=f"sandbox.skill_{state}", task_id=skill_ref, target_type="sandbox_skill")
    return SandboxSkillResponse(skill=_skill_provenance_payload(skill, catalog))


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


@router.delete("/skills/{skill_ref:path}")
def delete_skill(
    skill_ref: str,
    actor: AuthUser = Depends(require_system_admin),
    catalog: SkillCatalog = Depends(get_skill_catalog),
    packages: SkillPackageStore = Depends(get_skill_package_store),
    client: SandboxServiceClient | None = Depends(get_sandbox_service_client),
):
    try:
        item = catalog.delete_revoked(skill_ref)
    except SkillNotFound as exc:
        raise HTTPException(status_code=404, detail="Skill not found") from exc
    except SkillStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    # Retention cleanup: catalog history stays, but the uploaded source bytes
    # and any Runner-generated bundle/image for this deleted Skill are removed.
    sources_purged = 0
    for storage_ref in (item.text_storage_ref, item.package_storage_ref):
        if storage_ref and packages.delete(storage_ref):
            sources_purged += 1
    bundle_purged = False
    cleanup_pending = False
    bundle_ref = str(item.execution_bundle_ref or "")
    if bundle_ref.startswith("bundle/skill-"):
        if client is None:
            cleanup_pending = True
        else:
            try:
                result = client.purge_generated_bundle(
                    bundle_ref, envelope=_runner_envelope(actor),
                )
                bundle_purged = bool(result.get("purged"))
                cleanup_pending = bool(result.get("cleanup_pending"))
            except Exception as exc:  # noqa: BLE001 - cleanup must not block deletion
                cleanup_pending = True
                _audit(actor, action="sandbox.skill_purge", task_id=skill_ref,
                       target_type="sandbox_skill", success=False, error=str(exc))
    _audit(actor, action="sandbox.skill_delete", task_id=skill_ref, target_type="sandbox_skill",
           metadata={"version": item.version, "content_digest": item.content_digest,
                     "archive_digest": item.package_archive_digest or item.text_archive_digest,
                     "sources_purged": sources_purged, "bundle_purged": bundle_purged,
                     "cleanup_pending": cleanup_pending})
    return {"deleted": True, "sources_purged": sources_purged,
            "bundle_purged": bundle_purged, "cleanup_pending": cleanup_pending}


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
        if not skills.is_subscribed(skill_ref, user.id):
            raise SkillNotAuthorized("Skill must be added before use")
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
    input_ids = tuple(dict.fromkeys(str(item).strip() for item in body.input_artifact_ids if str(item).strip()))
    if input_ids:
        if "sandbox.internal_data" not in required:
            raise HTTPException(status_code=403, detail="Skill bundle is not approved to read input artifacts")
        required.add("sandbox.internal_data")
        for artifact_id in input_ids:
            try:
                broker.authorize_input(
                    artifact_id, principal_id=str(user.id), department_id=str(user.department_id),
                )
            except (ArtifactAccessDenied, ArtifactNotFound) as exc:
                # Do not disclose whether another department's artifact exists.
                raise HTTPException(status_code=404, detail="input artifact is not available") from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=409, detail="input artifact is not committed") from exc
    if body.resume_from and body.resume_from not in input_ids:
        raise HTTPException(status_code=400, detail="resume_from must reference a bound input artifact")
    if body.parent_task_id:
        try:
            parent = store.require(body.parent_task_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail="parent task not found") from exc
        if parent.owner_id != str(user.id) or parent.department_id != str(user.department_id):
            raise HTTPException(status_code=403, detail="parent task is not available")
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
            required_capabilities=tuple(sorted(required)), parameters=body.parameters,
            input_artifact_ids=input_ids,
            skill_ref=skill.skill_ref, skill_content_digest=skill.content_digest,
            skill_version=skill.version,
            runtime_contract_version="v1",
            parent_task_id=body.parent_task_id,
            resume_from=body.resume_from,
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


def _runner_envelope(actor: AuthUser, *, capabilities: tuple[str, ...] = ("sandbox.execute",)) -> RequestEnvelope:
    policy_hash = hashlib.sha256(
        f"skill-build:{settings.SANDBOX_RUNTIME_PROFILE_DIGEST}".encode("utf-8")
    ).hexdigest()
    return RequestEnvelope.create(
        actor_id=str(actor.id),
        department_id=str(actor.department_id or "system"),
        policy_snapshot_hash=policy_hash,
        capability_snapshot=capabilities,
        capability_snapshot_hash=capability_snapshot_hash(
            user_id=actor.id,
            department_id=actor.department_id or "system",
            capabilities=capabilities,
            policy_snapshot_hash=policy_hash,
        ),
        audience="sandbox-runner",
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


def _require_artifact_access(manifest: Any, user: AuthUser) -> None:
    if user.role == ROLE_SYSTEM_ADMIN:
        return
    if (
        str(manifest.owner_id) != str(user.id)
        or str(manifest.department_id) != str(user.department_id or "")
    ):
        # Never disclose whether another department's artifact exists.
        raise HTTPException(status_code=404, detail="artifact not found")


def _artifact_diff_payload(
    current: Any,
    previous: Any,
    *,
    user: AuthUser,
    broker: ArtifactBroker,
) -> dict[str, Any]:
    filename = str(current.filename or "")
    if not filename.lower().endswith(_DIFF_TEXT_SUFFIXES):
        raise HTTPException(status_code=415, detail="artifact type does not support text diff")
    if int(current.size or 0) > _MAX_DIFF_BYTES or int(previous.size or 0) > _MAX_DIFF_BYTES:
        raise HTTPException(status_code=413, detail="artifact is too large for an inline diff")
    department = str(current.department_id if user.role == ROLE_SYSTEM_ADMIN else user.department_id or "")
    current_content = broker.read(
        current.artifact_id,
        principal_id=str(user.id),
        department_id=department,
        task_id=current.task_id if user.role == ROLE_SYSTEM_ADMIN else None,
    ).decode("utf-8", errors="replace")
    previous_content = broker.read(
        previous.artifact_id,
        principal_id=str(user.id),
        department_id=str(previous.department_id if user.role == ROLE_SYSTEM_ADMIN else department),
        task_id=previous.task_id if user.role == ROLE_SYSTEM_ADMIN else None,
    ).decode("utf-8", errors="replace")
    lines = list(
        difflib.unified_diff(
            previous_content.splitlines(),
            current_content.splitlines(),
            fromfile=f"{previous.artifact_id}/{previous.filename}",
            tofile=f"{current.artifact_id}/{current.filename}",
            lineterm="",
            n=3,
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    truncated = len(lines) > _MAX_DIFF_LINES
    return {
        "artifact_id": current.artifact_id,
        "base_artifact_id": previous.artifact_id,
        "filename": current.filename,
        "base_filename": previous.filename,
        "sha256": current.sha256,
        "base_sha256": previous.sha256,
        "changed": bool(lines),
        "added_lines": added,
        "removed_lines": removed,
        "truncated": truncated,
        "diff": "\n".join(lines[:_MAX_DIFF_LINES]),
    }


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


@router.post("/artifacts/{artifact_id}/promote", response_model=SandboxArtifactResponse)
def promote_artifact(
    artifact_id: str,
    user: AuthUser = Depends(require_employee),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    """Explicitly copy an output into a new input Artifact with lineage."""

    try:
        manifest = broker.require(artifact_id)
        if user.role != ROLE_SYSTEM_ADMIN and (
            manifest.owner_id != str(user.id) or manifest.department_id != str(user.department_id or "")
        ):
            raise ArtifactAccessDenied()
        promoted = broker.promote_output(
            artifact_id,
            principal_id=str(user.id),
            department_id=str(user.department_id or ""),
        )
    except Exception as exc:  # noqa: BLE001
        _artifact_error(exc)
    _audit(
        user,
        action="sandbox.artifact_promote",
        task_id=artifact_id,
        target_type="sandbox_artifact",
        metadata={"promoted_artifact_id": promoted.artifact_id},
    )
    return SandboxArtifactResponse(artifact=promoted.to_dict())


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


@router.get("/artifacts/{artifact_id}/diff/{base_artifact_id}")
def diff_artifacts(
    artifact_id: str,
    base_artifact_id: str,
    user: AuthUser = Depends(current_user),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    """Compare two committed text artifacts in the caller's scope."""

    try:
        current = broker.require(artifact_id)
        previous = broker.require(base_artifact_id)
        _require_artifact_access(current, user)
        _require_artifact_access(previous, user)
        payload = _artifact_diff_payload(current, previous, user=user, broker=broker)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _artifact_error(exc)
    _audit(
        user,
        action="sandbox.artifact_diff",
        task_id=artifact_id,
        target_type="sandbox_artifact",
        metadata={
            "base_artifact_id": base_artifact_id,
            "changed": payload["changed"],
            "added_lines": payload["added_lines"],
            "removed_lines": payload["removed_lines"],
        },
    )
    return payload


@router.get("/tasks/{task_id}/artifacts/{artifact_id}/diff-parent")
def diff_artifact_with_parent(
    task_id: str,
    artifact_id: str,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    """Compare an output with the same-named output from its parent task."""

    task = _task_for_artifact(task_id, user, store)
    if not task.parent_task_id:
        raise HTTPException(status_code=404, detail="parent task not found")
    parent = _task_for_artifact(task.parent_task_id, user, store)
    try:
        current = broker.require(artifact_id)
        if current.task_id != task.task_id:
            raise HTTPException(status_code=404, detail="artifact not found")
        _require_artifact_access(current, user)
        candidates = [
            item
            for item in broker.list_for_task(parent.task_id, states={"committed", "consumed"})
            if item.role == "output" and item.filename == current.filename
        ]
        if not candidates:
            raise HTTPException(status_code=404, detail="parent artifact not found")
        previous = max(candidates, key=lambda item: str(item.created_at or ""))
        payload = _artifact_diff_payload(current, previous, user=user, broker=broker)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _artifact_error(exc)
    _audit(
        user,
        action="sandbox.artifact_diff_parent",
        task_id=task_id,
        target_type="sandbox_artifact",
        metadata={"artifact_id": artifact_id, "base_artifact_id": previous.artifact_id},
    )
    return {**payload, "task_id": task.task_id, "base_task_id": parent.task_id}


@router.get("/tasks/{task_id}/lineage")
def task_lineage(
    task_id: str,
    user: AuthUser = Depends(current_user),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
):
    """Return the bounded parent-task chain for a Skill run."""

    task = _task_for_artifact(task_id, user, store)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = task
    while current is not None and len(items) < _MAX_TASK_LINEAGE and current.task_id not in seen:
        seen.add(current.task_id)
        items.append({
            "task_id": current.task_id,
            "parent_task_id": current.parent_task_id,
            "skill_ref": current.skill_ref,
            "skill_version": current.skill_version,
            "status": current.status,
            "result_status": current.result_status,
            "created_at": current.created_at,
        })
        if not current.parent_task_id:
            break
        try:
            current = _task_for_artifact(current.parent_task_id, user, store)
        except HTTPException:
            break
    return {"items": items}


@router.post("/tasks/{task_id}/inputs", response_model=SandboxTaskResponse)
def bind_task_inputs(
    task_id: str,
    body: SandboxTaskInputBindRequest,
    user: AuthUser = Depends(require_employee),
    store: SandboxTaskStore = Depends(get_sandbox_task_store),
    broker: ArtifactBroker = Depends(get_sandbox_artifact_broker),
):
    """Bind user attachments to an admitted task before it is dispatched."""

    task = _task_for_artifact(task_id, user, store)
    if "sandbox.internal_data" not in task.required_capabilities:
        raise HTTPException(status_code=403, detail="task is not approved to read input artifacts")
    for artifact_id in body.artifact_ids:
        try:
            broker.authorize_input(
                artifact_id,
                principal_id=str(user.id),
                department_id=str(task.department_id),
            )
        except (ArtifactAccessDenied, ArtifactNotFound) as exc:
            raise HTTPException(status_code=404, detail="input artifact is not available") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail="input artifact is not committed") from exc
    try:
        updated = store.bind_inputs(
            task.task_id,
            tuple(body.artifact_ids),
            expected_revision=body.expected_revision if body.expected_revision >= 0 else None,
            actor_id=str(user.id),
        )
    except Exception as exc:  # noqa: BLE001
        _raise_store_error(exc)
    _audit(
        user,
        action="sandbox.task_bind_inputs",
        task_id=task.task_id,
        target_type="sandbox_task",
        metadata={"artifact_count": len(body.artifact_ids)},
    )
    return SandboxTaskResponse(task=updated.to_dict())


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
            if isinstance(exc, (ArtifactAccessDenied, ArtifactNotFound)):
                raise HTTPException(status_code=404, detail="input artifact is not available") from exc
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
        events = [{**item, "kind": "task"} for item in store.events(task_id)]
        events.extend(
            {**item, "kind": "runtime"} for item in store.runtime_events(task_id)
        )
        events.sort(key=lambda item: str(item.get("created_at") or ""))
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
