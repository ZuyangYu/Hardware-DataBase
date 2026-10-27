"""Versioned rollout boundary shared by authoring services and durable jobs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


REVIEW_WORKER_CONTRACT = 'multi_kb_review_v1'


def requires_review_contract(value: Any) -> bool:
    """Recognize complete R3 scope markers without reclassifying legacy data."""
    def read(key: str, default: Any = None) -> Any:
        return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)

    if read('source_snapshot_kind') == 'multi_knowledge_v1':
        return True
    if read('required_worker_contract') == REVIEW_WORKER_CONTRACT:
        return True
    for scope in (read('kb_scope_snapshot'), read('knowledge_scope_snapshot'), value):
        if isinstance(scope, Mapping):
            sources = scope.get('sources')
            if isinstance(sources, (list, tuple)) and len(sources) > 1:
                return True
    return False
