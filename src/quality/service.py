from __future__ import annotations

from contextlib import closing

from src.core.auth import ROLE_EMPLOYEE
from .store import QualityStore


class QualityService:
    def __init__(self, store: QualityStore, auth):
        self.store = store
        self.auth = auth

    def reviewers(self, viewer) -> list[dict]:
        if viewer.role != ROLE_EMPLOYEE or viewer.department_id is None:
            return []
        return [{"id": u.id, "username": u.username} for u in self.auth.list_users()
                if u.is_active and u.role == ROLE_EMPLOYEE and u.department_id == viewer.department_id and u.id != viewer.id]

    def _validate_share(self, viewer, case_id, request):
        from src.api.context import build_context_for_user

        with closing(self.store.connect()) as conn:
            case, snapshot = self.store.case_rows(conn, case_id, viewer)
            if viewer.id != int(snapshot["owner_user_id"]) or viewer.role != ROLE_EMPLOYEE:
                raise PermissionError("only the conversation owner may share review material")
            # Sharing is a new content operation, unlike reading one's history.
            # A department move cannot grant sharing of the old department's KB.
            if viewer.department_id is None or viewer.department_id != snapshot["department_id"]:
                raise PermissionError("sharing requires the original department scope")
            import json

            manifest = json.loads(snapshot["manifest_json"])
            ctx = build_context_for_user(viewer, manifest["kb_name"], auth=self.auth)
            if not ctx.has_kb_permission(manifest["kb_name"], "read"):
                raise PermissionError("current KB read permission required for sharing")
        available = {row["id"] for row in self.reviewers(viewer)}
        if not set(request.reviewer_user_ids).issubset(available):
            raise PermissionError("reviewers must be active employees in the same department")

    def share(self, viewer, case_id, request):
        self._validate_share(viewer, case_id, request)
        return self.store.change_share(viewer, case_id, request)

    def approve_review_request(self, viewer, case_id, request_id, request):
        """Approve one administrator request without widening content access."""
        self._validate_share(viewer, case_id, request)
        return self.store.approve_review_request(viewer, case_id, request_id, request)
