"""Group normalized claims and emit one case per unresolved field."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable

from .models import ConflictCase, NormalizedClaim


def _hash(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ConflictDetector:
    """Detect semantic disagreements, keeping conditions and project scope separate."""

    def __init__(self, *, policy_version: str = "conflict-policy-v1"):
        self.policy_version = policy_version

    def detect(self, claims: Iterable[NormalizedClaim]) -> list[ConflictCase]:
        groups: dict[tuple[str, str, str, str, str], list[NormalizedClaim]] = defaultdict(list)
        for claim in claims:
            key = (
                claim.field_key,
                claim.subject,
                claim.predicate,
                claim.condition,
                claim.scope_key,
            )
            groups[key].append(claim)
        cases: list[ConflictCase] = []
        for key, grouped in groups.items():
            values = {claim.normalized_unit + ":" + claim.normalized_value for claim in grouped}
            if len(values) <= 1:
                continue
            claim_ids = sorted(claim.claim_id for claim in grouped)
            fingerprint = _hash({"key": key, "claims": claim_ids, "values": sorted(values)})
            evidence_hash = _hash(sorted({e for claim in grouped for e in claim.evidence_ids}))
            cases.append(
                ConflictCase(
                    conflict_id=f"conflict-{uuid.uuid4().hex}",
                    fingerprint=fingerprint,
                    field_key=key[0],
                    subject=key[1],
                    predicate=key[2],
                    condition=key[3],
                    scope_key=key[4],
                    claims=tuple(grouped),
                    snapshot_hash=_hash(sorted({claim.source_content_hash for claim in grouped if claim.source_content_hash})),
                    evidence_set_hash=evidence_hash,
                    policy_version=self.policy_version,
                )
            )
        return cases


__all__ = ["ConflictDetector"]
