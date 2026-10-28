"""Shared scope validation and idempotency rules for both conversation stores."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.services.knowledge_scope import KnowledgeScopeInvalid
from src.services.knowledge_scope_models import KnowledgeScopeSnapshot, KnowledgeSelectionInput, selection_hash


class IdempotencyConflict(ValueError):
    def __init__(self):
        super().__init__("idempotency_conflict")


def initial_scope_summary(scope: dict[str, Any]) -> dict[str, Any]:
    """Authorize queued/streaming turns before the model produces its summary.

    Each turn snapshots its own source selection.  The session remains anchored
    to its primary KB, but adding or removing reference KBs for a later turn
    must not invalidate earlier turns' citations.
    """
    if not scope:
        return {}
    return {"source_dependencies": {
        "version": 1,
        "kb_ids": sorted({source["kb_id"] for source in scope["sources"]}),
        "inherited_turn_ids": [],
        "complete": True,
    }}


def selection_for_session(selection, *, user_id: int, anchor_kb_id: int | None, tenant_key: str = "default") -> tuple[dict, str]:
    if selection is None:
        return {}, ""
    value = selection.model_dump(mode="json") if hasattr(selection, "model_dump") else selection
    parsed = KnowledgeSelectionInput.model_validate(value)
    anchor = next((s for s in parsed.sources if s.kb_id == anchor_kb_id), None)
    if anchor is None or (parsed.purpose != "compare" and anchor.role != "primary"):
        raise KnowledgeScopeInvalid("knowledge_scope_anchor_mismatch")
    return parsed.model_dump(mode="json"), selection_hash(parsed, tenant_key=tenant_key, actor_user_id=user_id, anchor_kb_id=anchor_kb_id)


def turn_scope_payload(scope, *, user_id: int, anchor_kb_id: int | None, stored_hash: str) -> dict:
    if scope is None:
        if stored_hash:
            raise KnowledgeScopeInvalid("knowledge_scope_required")
        return {}
    value = scope.model_dump(mode="json") if hasattr(scope, "model_dump") else scope
    parsed = KnowledgeScopeSnapshot.model_validate(value)
    if parsed.actor_user_id != user_id or parsed.anchor_kb_id != anchor_kb_id:
        raise KnowledgeScopeInvalid("knowledge_scope_anchor_mismatch")
    _, expected = selection_for_session(parsed.selection(), user_id=user_id, anchor_kb_id=anchor_kb_id, tenant_key=parsed.tenant_key)
    if parsed.selection_hash != expected:
        raise KnowledgeScopeInvalid("knowledge_scope_invalid_hash")
    return parsed.model_dump(mode="json")


def request_fingerprint(*, scope: dict, query: str, source_scope: str, attachment_ids: list[str], query_mode: str, wiki_assist: bool) -> str:
    if not scope:
        return ""
    payload = {
        "version": 1, "query": query.strip(),
        "knowledge_selection_hash": scope["selection_hash"],
        "source_scope": source_scope, "attachment_ids": sorted(set(attachment_ids)),
        "query_mode": query_mode, "wiki_assist": bool(wiki_assist),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def require_replay_fingerprint(existing_fingerprint: str, requested_fingerprint: str) -> None:
    if (existing_fingerprint or requested_fingerprint) and existing_fingerprint != requested_fingerprint:
        raise IdempotencyConflict()
