"""Use-case service for project governance and frozen document inputs."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from src.pipelines.document_rag.schemas import RequestContext
from src.projects.access_service import ProjectAccessService
from src.projects.models import (
    Project,
    ProjectSourceCatalogEntry,
    SourceSetSnapshot,
)
from src.projects.store import ProjectStore


class ProjectService:
    def __init__(self, store: ProjectStore | None = None):
        self.store = store or ProjectStore()
        self.access = ProjectAccessService(self.store)

    def create_project(self, ctx: RequestContext, project: Project) -> Project:
        # A deployment may bootstrap the first project using a dedicated
        # service account.  A normal user must belong to the target department.
        if not ctx.user_id or "anonymous" in ctx.roles:
            raise PermissionError("authenticated user required to create a project")
        if project.tenant_id != (ctx.tenant_id or "default"):
            raise PermissionError("tenant mismatch")
        return self.store.create_project(project)

    def get_project_context(self, ctx: RequestContext, project_id: str) -> Project:
        self.access.require(ctx, project_id, "view_project")
        project = self.store.get_project(project_id, ctx.tenant_id or "default")
        assert project is not None
        return project

    def list_accessible_projects(self, ctx: RequestContext) -> list[Project]:
        tenant_id = ctx.tenant_id or "default"
        return [
            project
            for project in self.store.list_projects(tenant_id)
            if "view_project" in self.access.capabilities(ctx, project.project_id)
        ]

    def list_source_catalog(self, ctx: RequestContext, project_id: str) -> list[ProjectSourceCatalogEntry]:
        self.access.require(ctx, project_id, "read_project_sources")
        tenant_id = ctx.tenant_id or "default"
        entries: list[ProjectSourceCatalogEntry] = []
        for binding in self.store.list_project_source_bindings(project_id, tenant_id):
            version = self.store.get_source_version(binding.version_id, tenant_id)
            if version is None:
                continue
            document = self.store.get_logical_document(version.document_id, tenant_id)
            logical_document = document.title if document else version.document_id
            entries.append(ProjectSourceCatalogEntry(
                version_id=version.version_id,
                logical_document=logical_document,
                document_role=document.document_role if document else "unknown",
                module_scope=binding.module_scope,
                revision=version.revision,
                approval_status=version.approval_status,
                effective_from=version.effective_from,
                usage_type=binding.usage_type,
                current_for_context=version.approval_status in {"approved", "released"},
            ))
        return entries

    def create_source_set_snapshot(
        self,
        ctx: RequestContext,
        *,
        work_order_id: str,
        project_id: str,
        baseline_id: str,
        processing_artifact_ids: Iterable[str] | None = None,
    ) -> SourceSetSnapshot:
        """Freeze approved baseline sources while keeping future access dynamic."""
        self.access.require(ctx, project_id, "create_work_order")
        tenant_id = ctx.tenant_id or "default"
        baseline = self.store.get_baseline(baseline_id, tenant_id)
        if baseline is None or baseline.project_id != project_id:
            raise ValueError("baseline does not belong to the project")
        if baseline.status not in {"approved", "released"}:
            raise ValueError("document work orders require an approved or released baseline")

        source_bindings = {
            item.version_id: item
            for item in self.store.list_project_source_bindings(project_id, tenant_id)
            if item.status == "active" and item.usage_type != "template_only"
        }
        source_version_ids: list[str] = []
        shared_reference_version_ids: list[str] = []
        resolved_artifacts: list[str] = []
        requested_artifacts = set(processing_artifact_ids or [])
        policy_versions: dict[str, str] = {}

        for item in baseline.items:
            binding = source_bindings.get(item.source_version_id)
            if binding is None:
                raise ValueError(f"baseline item {item.config_item_key} is not actively bound to the project")
            version = self.store.get_source_version(item.source_version_id, tenant_id)
            if version is None:
                raise ValueError(f"baseline source version is missing: {item.source_version_id}")
            if version.approval_status not in {"approved", "released"}:
                raise ValueError(f"baseline source version is not approved: {version.version_id}")
            self._require_kb_intersection(ctx, project_id, version.document_id)
            source_version_ids.append(version.version_id)
            if binding.usage_type == "shared_reference":
                shared_reference_version_ids.append(version.version_id)

            ready = self.store.ready_artifacts_for_version(version.version_id, tenant_id)
            selected = [artifact for artifact in ready if not requested_artifacts or artifact.artifact_id in requested_artifacts]
            if requested_artifacts and not selected:
                raise ValueError(f"requested processing artifact is not ready for {version.version_id}")
            if not selected:
                raise ValueError(f"no ready processing artifact for {version.version_id}")
            for artifact in selected:
                resolved_artifacts.append(artifact.artifact_id)
                policies = self.store.allowed_region_policies(version.version_id, artifact.artifact_id)
                # P2a is fail-closed: a source can be frozen only if a human
                # approved at least one allowlisted region for that artifact.
                if not policies:
                    raise ValueError(f"no approved allowed region for {version.version_id}/{artifact.artifact_id}")
                for policy in policies:
                    policy_versions[policy.region_policy_id] = policy.policy_version

        snapshot = SourceSetSnapshot(
            source_set_snapshot_id=f"ss-{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            work_order_id=work_order_id,
            project_id=project_id,
            baseline_id=baseline.baseline_id,
            baseline_content_hash=baseline.content_hash,
            baseline_item_ids=[item.baseline_item_id for item in baseline.items],
            source_version_ids=sorted(set(source_version_ids)),
            shared_reference_version_ids=sorted(set(shared_reference_version_ids)),
            processing_artifact_ids=sorted(set(resolved_artifacts)),
            region_policy_versions=policy_versions,
            authorization_snapshot_id=self.access.authorization_snapshot_id(ctx, project_id),
        )
        return self.store.create_source_set_snapshot(snapshot)

    def _require_kb_intersection(self, ctx: RequestContext, project_id: str, document_id: str) -> None:
        """Apply KB scope when the migrated source declares a KB identity."""
        tenant_id = ctx.tenant_id or "default"
        document = self.store.get_logical_document(document_id, tenant_id)
        if document is None:
            raise ValueError("source logical document is missing")
        kb_id = str(document.metadata.get("kb_id") or "")
        kb_name = str(document.metadata.get("kb_name") or "")
        # Legacy sources have no KB identity yet. Their project binding still
        # governs access until the migration backfill populates this metadata.
        if not kb_id and not kb_name:
            return
        bindings = self.store.list_knowledge_bindings(project_id, tenant_id)
        if kb_id and kb_id not in {binding.kb_id for binding in bindings}:
            raise PermissionError("source knowledge base is not bound to the project")
        if kb_name and not ctx.has_kb_permission(kb_name, "read"):
            raise PermissionError("request context cannot read the source knowledge base")
