"""Server-side project membership and capability intersection."""

from __future__ import annotations

import hashlib
from datetime import datetime

from src.pipelines.document_rag.schemas import RequestContext
from src.projects.models import ProjectPrincipalBinding, canonical_hash, utc_now
from src.projects.store import ProjectStore


ROLE_CAPABILITIES: dict[str, set[str]] = {
    "viewer": {"view_project", "read_project_sources", "read_evidence", "download_review_candidate"},
    "author": {
        "view_project", "read_project_sources", "read_evidence", "download_review_candidate",
        "create_work_order", "run_deterministic_work_order", "submit_draft",
    },
    "reviewer": {
        "view_project", "read_project_sources", "read_evidence", "download_review_candidate",
        "submit_human_event", "confirm_result",
    },
    "approver": {
        "view_project", "read_project_sources", "read_evidence", "download_review_candidate",
        "submit_human_event", "confirm_result", "approve_artifact", "download_approved_release",
    },
    "project_admin": {
        "view_project", "read_project_sources", "read_evidence", "download_review_candidate",
        "create_work_order", "run_deterministic_work_order", "submit_draft", "submit_human_event",
        "confirm_result", "approve_artifact", "download_approved_release", "manage_project",
    },
}


class ProjectAccessService:
    def __init__(self, store: ProjectStore):
        self.store = store

    @staticmethod
    def _binding_capabilities(binding: ProjectPrincipalBinding) -> set[str]:
        return ROLE_CAPABILITIES.get(binding.project_role, set()) | set(binding.capabilities)

    def active_bindings(self, ctx: RequestContext, project_id: str, at: datetime | None = None) -> list[ProjectPrincipalBinding]:
        tenant_id = ctx.tenant_id or "default"
        at = at or utc_now()
        # Do not make an application-wide administrator an automatic project
        # content reader. Governance and content access are separate scopes.
        return [
            binding
            for binding in self.store.list_principal_bindings(project_id, tenant_id, ctx.user_id)
            if binding.is_valid_at(at)
        ]

    def capabilities(self, ctx: RequestContext, project_id: str, at: datetime | None = None) -> set[str]:
        capabilities: set[str] = set()
        for binding in self.active_bindings(ctx, project_id, at):
            capabilities |= self._binding_capabilities(binding)
        return capabilities

    def require(self, ctx: RequestContext, project_id: str, capability: str, at: datetime | None = None) -> None:
        project = self.store.get_project(project_id, ctx.tenant_id or "default")
        if project is None:
            raise PermissionError("project does not exist in the current tenant")
        if project.status != "active":
            raise PermissionError("project is not active")
        if capability not in self.capabilities(ctx, project_id, at):
            raise PermissionError(f"project capability required: {capability}")

    def authorization_snapshot_id(self, ctx: RequestContext, project_id: str) -> str:
        bindings = self.active_bindings(ctx, project_id)
        payload = {
            "tenant_id": ctx.tenant_id or "default",
            "project_id": project_id,
            "user_id": ctx.user_id,
            "bindings": [binding.model_dump(mode="json") for binding in bindings],
        }
        return hashlib.sha256(canonical_hash(payload).encode("utf-8")).hexdigest()[:32]
