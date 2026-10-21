"""Verified evidence identities, reciprocal ranks and coverage-aware admission."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Callable

import src.settings as settings


class EvidenceIdentityIncomplete(ValueError):
    def __init__(self):
        super().__init__("evidence_identity_incomplete")


def evidence_text_hash(raw_content: str) -> str:
    return hashlib.sha256(raw_content.encode("utf-8")).hexdigest()


_IDENTITY_FIELDS = (
    "tenant_key", "department_id", "kb_id", "processor_kind", "local_record_id",
    "source_content_hash", "processing_artifact_id", "index_revision",
    "remote_document_id", "chunk_or_structured_locator", "evidence_text_hash",
)


def evidence_identity(fields: dict) -> str:
    if any(key not in fields for key in _IDENTITY_FIELDS):
        raise EvidenceIdentityIncomplete()
    for key in ("kb_id", "local_record_id"):
        if type(fields[key]) is not int or fields[key] <= 0:
            raise EvidenceIdentityIncomplete()
    if type(fields["index_revision"]) is not int or fields["index_revision"] < 0:
        raise EvidenceIdentityIncomplete()
    if any(not fields[key] for key in ("tenant_key", "department_id", "processor_kind", "chunk_or_structured_locator")):
        raise EvidenceIdentityIncomplete()
    if fields["processor_kind"] == "ragflow" and not fields["remote_document_id"]:
        raise EvidenceIdentityIncomplete()
    for key in ("source_content_hash", "evidence_text_hash"):
        if not isinstance(fields[key], str) or not re.fullmatch(r"[0-9a-f]{64}", fields[key]):
            raise EvidenceIdentityIncomplete()
    payload = {key: fields[key] for key in _IDENTITY_FIELDS}
    try:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise EvidenceIdentityIncomplete() from exc
    return "knowledge:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def source_label(source_name: str, metadata: dict) -> str:
    source = metadata.get("knowledge_source")
    if not isinstance(source, dict):
        return source_name
    version = metadata.get("revision") or str(metadata.get("source_content_hash") or "")[:12]
    return " · ".join(str(part) for part in (source.get("kb_name"), source_name, version) if part)


@dataclass
class EvidenceFusionResult:
    evidence: list
    coverage: dict[int, bool]
    complete: bool
    token_count: int
    candidate_refs: list[dict] = field(default_factory=list)
    discarded: dict[str, int] = field(default_factory=dict)
    query_coverage: list[dict] = field(default_factory=list)
    source_representatives: list[dict] = field(default_factory=list)


@dataclass
class RankedCandidate:
    identity: str
    item: object
    score: float
    cost: int
    kb_id: int
    primary: bool
    ranks: list[dict] = field(default_factory=list)


@dataclass
class ScopedCandidateBatch:
    candidates: list[RankedCandidate]
    discarded: dict[str, int] = field(default_factory=dict)
    query_candidates: dict[tuple, list[str]] = field(default_factory=dict)


def rank_evidence_candidates(
    outcomes, *, purpose: str, min_score: float | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> ScopedCandidateBatch:
    """Identity/quality-validate and rank every qualified candidate.

    This entry deliberately stops before per-call item/byte admission. The
    runtime owns that final, reversible decision; a ranking helper must not
    silently delete a target candidate.
    """
    if hasattr(outcomes, "source_outcomes"):
        outcomes = outcomes.source_outcomes
    if purpose not in {"answer", "compare", "compose"}:
        raise ValueError("invalid_knowledge_purpose")
    threshold = settings.RAGFLOW_SIMILARITY_THRESHOLD if min_score is None else min_score
    count = token_counter or (lambda text: len(text.encode("utf-8")))
    discarded = {"identity": 0, "quality": 0, "budget": 0}
    candidates: dict[str, dict] = {}
    seen_ranks = set()
    query_candidates: dict[tuple, list[str]] = {}
    for outcome in outcomes:
        query_key = (outcome.kb_id, outcome.query, outcome.retriever_kind)
        query_candidates.setdefault(query_key, [])
        if outcome.status != "success_with_hits":
            continue
        for rank, item in enumerate(outcome.evidence, 1):
            metadata = dict(item.metadata or {})
            try:
                key = evidence_identity(metadata)
                source = metadata.get("knowledge_source") or {}
                if (metadata["evidence_text_hash"] != evidence_text_hash(item.content)
                        or metadata["kb_id"] != outcome.kb_id or source.get("kb_id") != outcome.kb_id
                        or str(source.get("department_id")) != str(metadata["department_id"])):
                    raise EvidenceIdentityIncomplete()
            except EvidenceIdentityIncomplete:
                discarded["identity"] += 1
                continue
            if not math.isfinite(item.score) or item.score < threshold:
                discarded["quality"] += 1
                continue
            if key not in candidates:
                render_payload = json.dumps({
                    "source": source_label(item.source_name, metadata),
                    "locator": metadata["chunk_or_structured_locator"],
                    "content": item.content[:1200],
                }, ensure_ascii=False, sort_keys=True)
                cost = max(1, int(count(render_payload if token_counter else render_payload + " " * 128)))
                candidates[key] = {"item": replace(item, id=key, metadata=metadata), "score": 0.0, "cost": cost,
                                   "kb_id": outcome.kb_id, "primary": source.get("role") == "primary", "ranks": []}
            query_rank_key = (outcome.kb_id, outcome.query, outcome.retriever_kind, key)
            if query_rank_key in seen_ranks:
                continue
            seen_ranks.add(query_rank_key)
            candidates[key]["score"] += 1 / (60 + rank)
            candidates[key]["ranks"].append({"query": outcome.query, "rank": rank, "raw_score": item.score,
                                             "retriever_kind": outcome.retriever_kind})
            query_candidates[query_key].append(key)
    ranked = sorted(candidates, key=lambda key: (-candidates[key]["score"], not candidates[key]["primary"],
                                                 candidates[key]["kb_id"], key))
    return ScopedCandidateBatch(
        candidates=[RankedCandidate(identity=key, item=candidates[key]["item"], score=candidates[key]["score"],
                                    cost=candidates[key]["cost"], kb_id=candidates[key]["kb_id"],
                                    primary=candidates[key]["primary"], ranks=list(candidates[key]["ranks"]))
                    for key in ranked],
        discarded=discarded, query_candidates=query_candidates,
    )


def fuse_evidence(
    outcomes, *, purpose: str, max_items: int, token_budget: int,
    token_counter: Callable[[str], int] | None = None, min_score: float | None = None,
) -> EvidenceFusionResult:
    """Rank qualified candidates, reserve source coverage, then spend spare room.

    The default counter is a conservative UTF-8 byte bound for the rendered
    content/header. A caller with the model tokenizer can provide it instead;
    these counts never claim to be observed provider usage.
    """
    if hasattr(outcomes, "source_outcomes"):
        outcomes = outcomes.source_outcomes
    if purpose not in {"answer", "compare", "compose"}:
        raise ValueError("invalid_knowledge_purpose")
    threshold = settings.RAGFLOW_SIMILARITY_THRESHOLD if min_score is None else min_score
    count = token_counter or (lambda text: len(text.encode("utf-8")))
    coverage = {outcome.kb_id: False for outcome in outcomes}
    discarded = {"identity": 0, "quality": 0, "budget": 0}
    candidates = {}
    seen_ranks = set()
    query_candidates = {}
    for outcome in outcomes:
        query_key = (outcome.kb_id, outcome.query, outcome.retriever_kind)
        query_candidates.setdefault(query_key, [])
        if outcome.status != "success_with_hits":
            continue
        for rank, item in enumerate(outcome.evidence, 1):
            metadata = dict(item.metadata or {})
            try:
                key = evidence_identity(metadata)
                source = metadata.get("knowledge_source") or {}
                if (metadata["evidence_text_hash"] != evidence_text_hash(item.content)
                        or metadata["kb_id"] != outcome.kb_id or source.get("kb_id") != outcome.kb_id
                        or str(source.get("department_id")) != str(metadata["department_id"])):
                    raise EvidenceIdentityIncomplete()
            except EvidenceIdentityIncomplete:
                discarded["identity"] += 1
                continue
            if not math.isfinite(item.score) or item.score < threshold:
                discarded["quality"] += 1
                continue
            if key not in candidates:
                render_payload = json.dumps({
                    "source": source_label(item.source_name, metadata),
                    "locator": metadata["chunk_or_structured_locator"],
                    "content": item.content[:1200],
                }, ensure_ascii=False, sort_keys=True)
                cost = max(1, int(count(render_payload if token_counter else render_payload + " " * 128)))
                candidates[key] = {"item": replace(item, id=key, metadata=metadata), "score": 0.0, "cost": cost,
                                   "kb_id": outcome.kb_id, "primary": source.get("role") == "primary", "ranks": []}
            query_rank_key = (outcome.kb_id, outcome.query, outcome.retriever_kind, key)
            if query_rank_key in seen_ranks:
                continue
            seen_ranks.add(query_rank_key)
            candidates[key]["score"] += 1 / (60 + rank)
            candidates[key]["ranks"].append({"query": outcome.query, "rank": rank, "raw_score": item.score, "retriever_kind": outcome.retriever_kind})
            query_candidates[query_key].append(key)
    ranked = sorted(candidates, key=lambda key: (-candidates[key]["score"], not candidates[key]["primary"], candidates[key]["kb_id"], key))
    source_ids = sorted({item["kb_id"] for item in candidates.values()}, key=lambda kb: (not any(c["kb_id"] == kb and c["primary"] for c in candidates.values()), kb))
    per_source = {kb: [key for key in ranked if candidates[key]["kb_id"] == kb] for kb in source_ids}
    representatives = []
    preferred = {kb: [] for kb in source_ids}
    for kb in source_ids:
        kinds = sorted({entry["retriever_kind"] for key in per_source[kb] for entry in candidates[key]["ranks"]})
        for kind in kinds:
            rows = [(key, entry) for key in per_source[kb] for entry in candidates[key]["ranks"]
                    if entry["retriever_kind"] == kind]
            best_rank = min(entry["rank"] for _, entry in rows)
            top = [(key, entry) for key, entry in rows if entry["rank"] == best_rank]
            # RAGFlow document scores have a known [0, 1] similarity scale.
            # Compare only within this KB and retriever. Other scales retain
            # rank fusion, including a malformed/unknown score in the group.
            comparable = (kind == "document"
                          and all(candidates[key]["item"].metadata["processor_kind"] == "ragflow"
                                  and 0 <= entry["raw_score"] <= 1 for key, entry in rows))
            if comparable:
                key, entry = min(top, key=lambda row: (-row[1]["raw_score"], ranked.index(row[0])))
            else:
                key, entry = min(top, key=lambda row: ranked.index(row[0]))
            preferred[kb].append(key)
            representatives.append({"kb_id": kb, "retriever_kind": kind, "query": entry["query"],
                                    "evidence_identity": key, "retriever_rank": entry["rank"],
                                    "raw_score": entry["raw_score"],
                                    "selection_method": "source_retriever_score" if comparable else "rank"})
        preferred[kb] = [key for key in ranked if key in preferred[kb]]
    chosen = set()
    remaining = max(0, token_budget)
    limit = max(0, max_items)
    for position, kb in enumerate(source_ids):
        if len(chosen) >= limit:
            break
        others = source_ids[position + 1:]
        reserve = sum(min(candidates[key]["cost"] for key in preferred[other]) for other in others)
        feasible = [key for key in preferred[kb] if candidates[key]["cost"] <= remaining - reserve]
        if not feasible:
            reserve = sum(min(candidates[key]["cost"] for key in per_source[other]) for other in others)
            feasible = [key for key in preferred[kb] if candidates[key]["cost"] <= remaining - reserve]
        if not feasible:
            feasible = [key for key in per_source[kb] if candidates[key]["cost"] <= remaining - reserve]
        if not feasible:
            feasible = [key for key in per_source[kb] if candidates[key]["cost"] <= remaining]
        if feasible:
            chosen.add(feasible[0])
            remaining -= candidates[feasible[0]]["cost"]
    # Preserve distinct query winners before repeated generic hits spend the
    # spare budget. Output order remains RRF and never changes citations.
    for keys in query_candidates.values():
        if not keys or keys[0] in chosen or len(chosen) >= limit:
            continue
        key = keys[0]
        if candidates[key]["cost"] <= remaining:
            chosen.add(key)
            remaining -= candidates[key]["cost"]
    for key in ranked:
        if key not in chosen and len(chosen) < limit and candidates[key]["cost"] <= remaining:
            chosen.add(key)
            remaining -= candidates[key]["cost"]
    admitted = []
    for key in ranked:
        if key not in chosen:
            continue
        candidate = candidates[key]
        item = candidate["item"]
        metadata = {**item.metadata, "fusion_score": candidate["score"], "fusion_rank": len(admitted) + 1,
                    "raw_score": item.score, "retrieval_ranks": candidate["ranks"],
                    "context_token_estimate": candidate["cost"]}
        admitted.append(replace(item, metadata=metadata))
        coverage[candidate["kb_id"]] = True
    refs = [{"evidence_identity": key, **{field: candidates[key]["item"].metadata[field] for field in _IDENTITY_FIELDS}} for key in ranked]
    discarded["budget"] = len(candidates) - len(chosen)
    query_coverage = [{"kb_id": kb, "query": query, "retriever_kind": kind,
                       "qualified_candidates": len(set(keys)),
                       "retained_candidates": len(set(keys) & chosen),
                       "covered": bool(set(keys) & chosen),
                       "top_candidate_retained": bool(keys) and keys[0] in chosen,
                       "budget_cropped": bool(keys) and keys[0] not in chosen}
                      for (kb, query, kind), keys in query_candidates.items()]
    for representative in representatives:
        representative["retained"] = representative["evidence_identity"] in chosen
    return EvidenceFusionResult(admitted, coverage, bool(admitted) and all(coverage.values()),
                                max(0, token_budget) - remaining, refs, discarded,
                                query_coverage, representatives)
