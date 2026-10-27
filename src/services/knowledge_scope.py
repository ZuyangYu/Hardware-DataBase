"""Resolve complete KB selections using current, department scoped permissions.

Snapshots record what was authorized; they never grant access. Callers must
revalidate at each execution/history/export boundary, independently of hashes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from src.core.auth import AuthService, AuthUser, ROLE_EMPLOYEE
from src.pipelines.document_rag.schemas import RequestContext, kb_scope_key
from src.services.knowledge_scope_models import (
    AuthorizedKnowledgeSource,
    KnowledgeScopeSnapshot,
    KnowledgeSelectionInput,
    KnowledgeSourceInput,
    selection_hash,
)


class KnowledgeScopeDenied(PermissionError):
    def __init__(self):
        super().__init__("knowledge_scope_denied")


class KnowledgeScopeInvalid(ValueError):
    pass


class KnowledgeScopeChanged(ValueError):
    pass


def require_same_selection(stored_hash: str, requested_hash: str) -> None:
    if stored_hash and stored_hash != requested_hash:
        raise KnowledgeScopeChanged("knowledge_scope_changed")


class KnowledgeScopeResolver:
    def __init__(self, auth: AuthService, *, tenant_key: str = "default"):
        if not tenant_key:
            raise KnowledgeScopeInvalid("knowledge_scope_invalid_tenant")
        self.auth = auth
        self.tenant_key = tenant_key

    def _actor(self, user_id: int) -> AuthUser:
        actor = self.auth.get_user_by_id(user_id)
        if actor is None or not actor.is_active or actor.role != ROLE_EMPLOYEE or actor.department_id is None:
            raise KnowledgeScopeDenied()
        return actor

    def _authorize(self, actor: AuthUser, source: KnowledgeSourceInput | AuthorizedKnowledgeSource) -> AuthorizedKnowledgeSource:
        identity = self.auth.get_knowledge_base_identity(source.kb_id)
        if identity is None or identity.department_id != actor.department_id:
            raise KnowledgeScopeDenied()
        if isinstance(source, AuthorizedKnowledgeSource) and source.department_id != str(identity.department_id):
            raise KnowledgeScopeDenied()
        permissions = self.auth.get_kb_permissions_for_user(actor, [identity.kb_name])
        if permissions.get(kb_scope_key(identity.kb_name, identity.department_id)) not in {"read", "write", "admin"}:
            raise KnowledgeScopeDenied()
        return AuthorizedKnowledgeSource(
            kb_id=identity.kb_id,
            kb_name=identity.kb_name,
            department_id=str(identity.department_id),
            role=source.role,
        )

    def resolve(
        self,
        actor: AuthUser,
        anchor_kb_name: str,
        selection: KnowledgeSelectionInput | None,
    ) -> KnowledgeScopeSnapshot:
        actor = self._actor(actor.id)
        if not anchor_kb_name.strip() or anchor_kb_name == "__general__":
            raise KnowledgeScopeInvalid("knowledge_scope_anchor_mismatch")
        anchor_id = self.auth.get_knowledge_base_id(anchor_kb_name, department_id=actor.department_id)
        if anchor_id is None:
            raise KnowledgeScopeDenied()
        if selection is None:
            selection = KnowledgeSelectionInput(purpose="answer", sources=(KnowledgeSourceInput(kb_id=anchor_id, role="primary"),))
        # Authorize the entire set before validating the anchor or publishing
        # any context. A denied member must never be silently trimmed.
        sources = tuple(self._authorize(actor, source) for source in selection.sources)
        self._require_anchor(selection, anchor_id)
        return KnowledgeScopeSnapshot(
            snapshot_id=uuid4().hex,
            tenant_key=self.tenant_key,
            actor_user_id=actor.id,
            anchor_kb_id=anchor_id,
            selection_hash=selection_hash(selection, tenant_key=self.tenant_key, actor_user_id=actor.id, anchor_kb_id=anchor_id),
            purpose=selection.purpose,
            sources=sources,
            authorized_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _require_anchor(selection: KnowledgeSelectionInput, anchor_id: int) -> None:
        anchor = next((s for s in selection.sources if s.kb_id == anchor_id), None)
        if anchor is None or (selection.purpose != "compare" and anchor.role != "primary"):
            raise KnowledgeScopeInvalid("knowledge_scope_anchor_mismatch")

    def revalidate(self, actor_user_id: int, snapshot: KnowledgeScopeSnapshot) -> tuple[AuthorizedKnowledgeSource, ...]:
        if actor_user_id != snapshot.actor_user_id or self.tenant_key != snapshot.tenant_key:
            raise KnowledgeScopeDenied()
        actor = self._actor(actor_user_id)
        try:
            selection = snapshot.selection()
            self._require_anchor(selection, snapshot.anchor_kb_id)
        except (ValidationError, KnowledgeScopeInvalid) as exc:
            raise KnowledgeScopeDenied() from exc
        expected = selection_hash(selection, tenant_key=self.tenant_key, actor_user_id=actor_user_id, anchor_kb_id=snapshot.anchor_kb_id)
        if snapshot.selection_hash != expected:
            raise KnowledgeScopeDenied()
        return tuple(self._authorize(actor, source) for source in snapshot.sources)

    def context_for_source(self, actor_user_id: int, source: AuthorizedKnowledgeSource) -> RequestContext:
        actor = self._actor(actor_user_id)
        current = self._authorize(actor, source)
        permission_key = kb_scope_key(current.kb_name, current.department_id)
        permissions = self.auth.get_kb_permissions_for_user(actor, [current.kb_name])
        permission = permissions.get(permission_key)
        if permission not in {"read", "write", "admin"}:
            raise KnowledgeScopeDenied()
        return RequestContext(
            user_id=actor.username,
            session_id=uuid4().hex,
            roles=[actor.role],
            allowed_kbs=[permission_key],
            kb_permissions={permission_key: permission},
            tenant_id=self.tenant_key,
            metadata={
                "actor_user_id": actor.id,
                "actor_department_id": actor.department_id,
                "resource_department_id": int(current.department_id),
                "department_id": int(current.department_id),
                "kb_id": current.kb_id,
                "knowledge_source_role": current.role,
            },
        )
