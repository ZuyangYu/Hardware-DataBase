"""Scope/expiry validation for EvidenceRegistry entries.

The coordinator re-validates every proposal and every recovery read against
the registry before writing receipts, drafts or graph state. Validation only
narrows: a stable error is returned instead of widening scope, and an
unreadable/expired entry yields ``evidence_unavailable`` rather than deleting
audit references.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.document_authoring.models import EvidenceRegistryEntry

ERROR_CROSS_TENANT = "evidence_cross_tenant"
ERROR_CROSS_RUN = "evidence_cross_run"
ERROR_SNAPSHOT_MISMATCH = "evidence_snapshot_mismatch"
ERROR_EXPIRED = "evidence_expired"
ERROR_UNAVAILABLE = "evidence_unavailable"


class EvidenceAccessError(PermissionError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def validate_evidence_access(
    entry: EvidenceRegistryEntry | None,
    *,
    tenant_id: str,
    harness_run_id: str,
    source_set_snapshot_id: str,
    now: datetime | None = None,
) -> EvidenceRegistryEntry:
    """Return the entry when it belongs to the requesting tenant/run/snapshot."""

    if entry is None:
        raise EvidenceAccessError(ERROR_UNAVAILABLE, "evidence is not registered for this run")
    if entry.tenant_id != tenant_id:
        raise EvidenceAccessError(ERROR_CROSS_TENANT, "evidence belongs to another tenant")
    if entry.harness_run_id != harness_run_id:
        raise EvidenceAccessError(ERROR_CROSS_RUN, "evidence belongs to another harness run")
    if entry.source_set_snapshot_id != source_set_snapshot_id:
        raise EvidenceAccessError(ERROR_SNAPSHOT_MISMATCH, "evidence is bound to a different frozen snapshot")
    current = now or datetime.now(timezone.utc)
    if entry.expires_at is not None and current >= entry.expires_at:
        raise EvidenceAccessError(ERROR_EXPIRED, "evidence registry entry has expired")
    return entry
