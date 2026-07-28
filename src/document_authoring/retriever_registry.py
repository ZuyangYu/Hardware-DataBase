"""Capability-aware retrieval dispatch for the document authoring harness.

Stage 2 of the authoring improvement plan. Generalises the Stage 0 hardcoded
``tabular_lookup`` spreadsheet dispatch into a registry that routes each
declared ``required_capabilities`` entry to a corresponding retriever, merges
and deduplicates evidence by content hash, applies ``preferred_source_roles``
boosting (P7), and offers cross-unit evidence reuse (P8).

The registry is closure-internal: it introduces no new external tool, LLM call
or data source, so (like the Stage 0 spreadsheet dispatch) it is not gated by
``HarnessPolicy.allowed_tools``. Reused evidence comes from the same frozen
source set within the same run and is therefore already落域-validated.

The module is Evidence-type agnostic: it duck-types ``.content`` / ``.score``
/ ``.metadata`` and an optional ``.document_role`` so it works for both
``src.agents.state.Evidence`` (KB path) and ``EvidenceEnvelope`` (project path).
"""

from __future__ import annotations

import copy as _copy
import hashlib
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Capabilities that have a dedicated specialised retriever. Anything not listed
# here (e.g. ``revision_lookup``) falls back to the default (RAGFlow) retriever.
DEFAULT_CAPABILITY = "document_claim_lookup"

Retriever = Callable[[str, Any], list[Any]]


def content_hash(evidence: Any) -> str:
    """Stable content-based dedup key, independent of the Evidence type."""
    return hashlib.sha256(str(evidence.content).strip().encode("utf-8")).hexdigest()


def dedup_by_content(evidences: list[Any]) -> list[Any]:
    """Deduplicate by content hash, keeping the highest-scoring copy.

    Ties keep the first-seen entry. Order follows first-seen.
    """
    best: dict[str, Any] = {}
    for evidence in evidences:
        key = content_hash(evidence)
        existing = best.get(key)
        if existing is None or evidence.score > existing.score:
            best[key] = evidence
    return list(best.values())


def _evidence_role(evidence: Any) -> str | None:
    role = getattr(evidence, "document_role", None)
    if not role:
        role = evidence.metadata.get("document_role") if evidence.metadata else None
    return role or None


def apply_role_boost(
    evidences: list[Any],
    preferred_roles: list[str] | None,
    factor: float = 1.5,
) -> list[Any]:
    """Boost the score of evidence whose source role matches a preferred role.

    Pure deterministic adjustment; a no-op when no role information is present
    (e.g. the KB path, where ``state.Evidence`` carries no ``document_role``).
    """
    if not preferred_roles:
        return list(evidences)
    roles = set(preferred_roles)
    result: list[Any] = []
    for evidence in evidences:
        role = _evidence_role(evidence)
        if role and role in roles:
            evidence.score = evidence.score * factor
            if evidence.metadata is not None:
                evidence.metadata["preferred_source_role_match"] = role
        result.append(evidence)
    return result


def _copy_for_reuse(evidence: Any) -> Any:
    """Deep copy an evidence object so reuse tagging never mutates the cache."""
    if hasattr(evidence, "model_copy"):
        return evidence.model_copy(deep=True)
    return _copy.deepcopy(evidence)


class CrossUnitEvidenceCache:
    """Per-run cache enabling cross-unit evidence reuse (P8).

    Instantiated once per retriever closure (which persists across units in a
    run). ``ingest`` accumulates fresh hits; ``offer`` returns previously-seen
    evidence whose content shares a query term with the current query, tagged
    ``reused`` / ``reused_from_unit``. Offer is only consulted when the current
    unit's fresh retrieval is empty, so reuse never adds noise to a hit.
    """

    def __init__(self, max_reuse_per_unit: int = 5):
        self.max_reuse_per_unit = max_reuse_per_unit
        self._store: dict[str, dict[str, Any]] = {}

    def ingest(self, evidences: list[Any], unit_id: str) -> None:
        for evidence in evidences:
            key = content_hash(evidence)
            existing = self._store.get(key)
            if existing is None or evidence.score > existing["evidence"].score:
                self._store[key] = {"evidence": evidence, "unit_id": unit_id}

    def offer(
        self,
        requirement: Any,
        query: str,
        unit_id: str,
    ) -> list[Any]:
        query_terms = {term for term in str(query).lower().split() if term}
        if not query_terms:
            return []
        candidates: list[dict[str, Any]] = []
        for entry in self._store.values():
            if entry["unit_id"] == unit_id:
                # Never offer a unit its own previously-retrieved evidence.
                continue
            content_terms = {
                term for term in str(entry["evidence"].content).lower().split() if term
            }
            if query_terms & content_terms:
                candidates.append(entry)
        candidates.sort(key=lambda entry: entry["evidence"].score, reverse=True)
        result: list[Any] = []
        for entry in candidates[: self.max_reuse_per_unit]:
            reuse = _copy_for_reuse(entry["evidence"])
            if reuse.metadata is not None:
                reuse.metadata["reused"] = True
                reuse.metadata["reused_from_unit"] = entry["unit_id"]
            result.append(reuse)
        return result


class RetrieverRegistry:
    """Routes ``required_capabilities`` to retrievers and post-processes evidence.

    ``default_retriever`` (RAGFlow) is always invoked to preserve Stage 0
    behaviour and avoid regressing text recall; specialised retrievers are
    *additively* invoked for declared capabilities (e.g. ``tabular_lookup`` ->
    spreadsheet). Evidence is merged, deduplicated by content hash, role-boosted,
    and -- when fresh retrieval is empty -- supplemented with cross-unit reuse.
    """

    def __init__(
        self,
        default_retriever: Retriever,
        specialized: dict[str, Retriever] | None = None,
        cross_unit_cache: CrossUnitEvidenceCache | None = None,
        role_boost_factor: float = 1.5,
    ):
        self.default_retriever = default_retriever
        self.specialized = dict(specialized or {})
        self.cross_unit_cache = cross_unit_cache
        self.role_boost_factor = role_boost_factor

    def retrieve(self, requirement: Any, query: str) -> list[Any]:
        fresh: list[Any] = list(self.default_retriever(query, requirement))
        for capability in requirement.required_capabilities or []:
            retriever = self.specialized.get(capability)
            if retriever is None:
                # revision_lookup etc. have no specialised retriever yet.
                continue
            try:
                fresh.extend(retriever(query, requirement))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("specialised retriever '%s' failed: %s", capability, exc)
        fresh = dedup_by_content(fresh)
        fresh = apply_role_boost(
            fresh, requirement.preferred_source_roles, self.role_boost_factor
        )
        if fresh:
            result = fresh
        elif self.cross_unit_cache is not None:
            result = self.cross_unit_cache.offer(
                requirement, query, requirement.semantic_unit_id
            )
        else:
            result = []
        # Only genuinely fresh evidence enters the cache; reused evidence is
        # already cached under its original unit, so re-ingesting would clobber
        # provenance.
        if self.cross_unit_cache is not None:
            self.cross_unit_cache.ingest(fresh, requirement.semantic_unit_id)
        return result
