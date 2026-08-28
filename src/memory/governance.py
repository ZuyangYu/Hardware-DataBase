"""Small governance facade over :class:`MemoryService`.

The HTTP layer calls the service directly today.  This facade keeps the
governance surface separately importable for a future CLI/admin worker without
creating a second implementation of authorization or revision CAS.
"""

from __future__ import annotations

from typing import Any, Iterable


class MemoryGovernance:
    """Delegate governance operations to the Catalog-first service."""

    def __init__(self, service):
        self.service = service

    def verify(self, **kwargs: Any) -> dict[str, Any]:
        return self.service.verify_memory(**kwargs)

    def reject(self, **kwargs: Any) -> dict[str, Any]:
        return self.service.reject_memory(**kwargs)

    def supersede(self, **kwargs: Any) -> dict[str, Any]:
        return self.service.supersede_memory(**kwargs)

    def delete(self, **kwargs: Any) -> dict[str, Any]:
        return self.service.delete_memory(**kwargs)


def verify(service, *, actor, memory_id: str, expected_revision: int, evidence_refs: Iterable[Any], reason: str, request_id: str = "", **kwargs: Any):
    return MemoryGovernance(service).verify(
        actor=actor,
        memory_id=memory_id,
        expected_revision=expected_revision,
        evidence_refs=evidence_refs,
        reason=reason,
        request_id=request_id,
        **kwargs,
    )


__all__ = ["MemoryGovernance", "verify"]
