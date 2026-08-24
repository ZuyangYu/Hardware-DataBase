"""Governed EDF-component → datasheet record link index (plan task 5b).

Admission rules:
- ``verified`` requires a unique, exact normalized MPN match between a
  namespaced EDF identifier and a confirmed DocumentProfile MPN, with matching
  manufacturer on both sides, a present document revision, and no
  normalization collisions. Internal part numbers participate only after a
  versioned governed catalog maps them to an MPN.
- Anything else (multi-document hits, manufacturer missing/conflicting,
  missing revision, filename/text/LLM hints) is at most ``candidate``.
- Every read re-validates both ends: circuit generation, record scope,
  profile stamps. Any change rejects the stale link immediately.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from src.circuit.component_identity import (
    normalize_identifier_value,
)
from src.circuit.models import ComponentDatasheetLink

logger = logging.getLogger(__name__)

LINKS_FILE = "datasheet_links.json"

# Field-source controlled split rules: which separators may split one raw
# property value into multiple MPN candidates. Raw values are preserved.
MPN_CANDIDATE_SEPARATORS = {
    "manufacturer_part_number": ("/", ";", ","),
    # Internal PNs are single-valued; they never split.
}

# Namespaces eligible for datasheet matching at all.
_MATCHABLE_NAMESPACES = ("manufacturer_part_number",)


@dataclass(frozen=True)
class DatasheetCatalogEntry:
    """Versioned internal-PN → MPN mapping from a governed catalog."""

    entry_id: str
    catalog_version: str
    source_file: str
    internal_pn: str
    mpn: str
    confidence: float = 1.0

    def __post_init__(self):
        for name in ("entry_id", "catalog_version", "source_file", "internal_pn", "mpn"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} 必须是非空字符串")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence 必须在 [0, 1] 区间内")


@dataclass(frozen=True)
class DocumentProfileSnapshot:
    """Read-only view of the governed DocumentProfile needed for matching."""

    record_id: int
    kb_name: str
    department_id: str
    remote_document_id: str
    parse_status: str = ""
    content_hash: str = ""
    source_version_id: str = ""
    revision: str = ""
    mpn_values: tuple[str, ...] = ()
    manufacturer: str = ""

    def has_mpn(self, normalized_mpn: str) -> bool:
        return any(normalize_identifier_value(value) == normalized_mpn for value in self.mpn_values)


def split_mpn_candidates(raw_value: str, namespace: str) -> list[str]:
    """Split a raw field value into candidates using its field-source rule."""
    text = str(raw_value or "").strip()
    if not text:
        return []
    separators = MPN_CANDIDATE_SEPARATORS.get(namespace, ())
    if not separators:
        return [text]
    import re

    parts = [part.strip() for part in re.split("[" + re.escape("".join(separators)) + "]", text)]
    return [part for part in parts if part] or [text]


def _manufacturer_key(value: Any) -> str:
    """Loose manufacturer comparison: CJK-safe letters/digits only."""
    import re as _re

    return _re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).casefold()


class ComponentDatasheetLinkIndex:
    """Persists per-design links and validates them on every read."""

    def __init__(self, store: Any, document_store: Any | None = None):
        self.store = store
        self.document_store = document_store

    # ── persistence ───────────────────────────────────────────────────────

    def links_path(self, kb_name: str, design_id: str) -> str:
        return os.path.join(self.store.design_dir(kb_name, design_id), LINKS_FILE)

    def save_links(self, kb_name: str, design_id: str, links: Sequence[ComponentDatasheetLink]) -> None:
        from src.circuit.store import _atomic_write

        payload = json.dumps(
            {"links": [item.to_dict() for item in links]},
            ensure_ascii=False,
            indent=2,
        )
        path = self.links_path(kb_name, design_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write(path, payload)

    def load_links(self, kb_name: str, design_id: str) -> list[ComponentDatasheetLink]:
        path = self.links_path(kb_name, design_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            links = [ComponentDatasheetLink.from_dict(item) for item in data.get("links", [])]
        except (OSError, ValueError, TypeError):
            return []
        return links

    def remove_links(self, kb_name: str, design_id: str) -> None:
        try:
            os.unlink(self.links_path(kb_name, design_id))
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Failed to remove datasheet links for %s/%s", kb_name, design_id)

    # ── matching ──────────────────────────────────────────────────────────

    def load_profile_snapshots(self, kb_name: str) -> list[DocumentProfileSnapshot]:
        """Governed profile views for one KB (ragflow records only)."""
        if self.document_store is None:
            return []
        snapshots: list[DocumentProfileSnapshot] = []
        try:
            records = self.document_store.list_documents(kb_name)
        except Exception:
            return []
        for record in records:
            if str(getattr(record, "processor_kind", "")) != "ragflow" or not getattr(record, "document_id", ""):
                continue
            profile = None
            try:
                profile = self.document_store.get_document_profile(record.id)
            except Exception:
                profile = None
            mpn_values = tuple(profile.get("mpn_values") or ()) if isinstance(profile, dict) else ()
            manufacturer = str(profile.get("manufacturer") or "") if isinstance(profile, dict) else ""
            snapshots.append(
                DocumentProfileSnapshot(
                    record_id=record.id,
                    kb_name=record.kb_name,
                    department_id=record.department_id,
                    remote_document_id=record.document_id,
                    parse_status=str(record.status or ""),
                    content_hash=str(record.content_hash or ""),
                    source_version_id=str(record.source_version_id or ""),
                    revision=str(record.revision or ""),
                    mpn_values=mpn_values,
                    manufacturer=manufacturer,
                )
            )
        return snapshots

    def rebuild_links_for_design(
        self,
        design: Any,
        generation_id: str | None = None,
        catalog_entries: Iterable[DatasheetCatalogEntry] = (),
    ) -> int:
        """Recompute and atomically republish one design's links."""
        try:
            from src.circuit.store import circuit_generation_id

            generation = generation_id or circuit_generation_id(design)
        except Exception:
            generation = generation_id or ""
        profiles = self.load_profile_snapshots(design.kb_name)
        links = self.build_links_for_design(design, generation, profiles, catalog_entries)
        self.save_links(design.kb_name, design.design_id, links)
        return len(links)

    def build_links_for_design(
        self,
        design: Any,
        generation_id: str,
        profiles: Sequence[DocumentProfileSnapshot],
        catalog_entries: Iterable[DatasheetCatalogEntry] = (),
    ) -> list[ComponentDatasheetLink]:
        """Derive candidate/verified links for every identity in the design.

        Fully recomputable from identities + profiles + catalog; never reads
        filenames, free text or vector hits.
        """
        entries = list(catalog_entries)
        by_refdes: dict[str, list[ComponentDatasheetLink]] = {}
        identity_source = (
            design.component_identities
            or __import__("src.circuit.component_identity", fromlist=["build_component_identities"]).build_component_identities(design)
        )
        for identity in identity_source:
            instance = next((item for item in design.instances if item.refdes == identity.refdes), None)
            manufacturer_raw = str((instance.properties or {}).get("Manufacturer") or "") if instance else ""
            links: list[ComponentDatasheetLink] = []
            seen_pairs: set[tuple[str, int, str]] = set()
            for identifier in identity.identifiers:
                # Group evaluation per raw field value: every split candidate
                # must resolve to the SAME profile, otherwise the whole value
                # degrades to candidates (never an auto-pick).
                mapped_mpns: list[tuple[str, str]] = []  # (candidate, match_method)
                if identifier.namespace in _MATCHABLE_NAMESPACES:
                    for candidate in split_mpn_candidates(identifier.raw_value, identifier.namespace):
                        mapped_mpns.append((candidate, "exact_mpn"))
                elif identifier.namespace == "internal_part_number":
                    for entry in entries:
                        if normalize_identifier_value(entry.internal_pn) != identifier.normalized_value:
                            continue
                        mapped_mpns.append((entry.mpn.strip(), "internal_pdn_catalog"))
                groups: dict[tuple[str, str], list[tuple[str, DocumentProfileSnapshot]]] = {}
                for candidate, method in mapped_mpns:
                    normalized = normalize_identifier_value(candidate)
                    if not normalized:
                        continue
                    hits = [profile for profile in profiles if profile.has_mpn(normalized)]
                    groups.setdefault(method, []).extend((candidate, profile) for profile in hits)
                for method, pairs in groups.items():
                    distinct_profiles = {profile.record_id for _, profile in pairs}
                    if len(distinct_profiles) > 1:
                        for _, profile in pairs:
                            key = (identity.refdes, profile.record_id, method)
                            if key in seen_pairs:
                                continue
                            seen_pairs.add(key)
                            links.append(self._link(
                                design, generation_id, identity.refdes, profile,
                                profile.mpn_values[0] if profile.mpn_values else "",
                                method, "candidate", 0.3,
                                source_locator={
                                    "identifier_namespace": identifier.namespace,
                                    "raw_value": identifier.raw_value,
                                    "degrade_reason": "multiple_documents",
                                },
                            ))
                        continue
                    if not pairs:
                        continue
                    profile = pairs[0][1]
                    status, confidence = "verified", float(entry_confidence(method))
                    degrade_reason = ""
                    profile_manufacturer = _manufacturer_key(profile.manufacturer)
                    instance_manufacturer = _manufacturer_key(manufacturer_raw)
                    if not profile_manufacturer or not instance_manufacturer:
                        status, confidence, degrade_reason = "candidate", 0.3, "manufacturer_missing"
                    elif profile_manufacturer != instance_manufacturer:
                        status, confidence, degrade_reason = "candidate", 0.3, "manufacturer_conflict"
                    elif not str(profile.revision or "").strip():
                        status, confidence, degrade_reason = "candidate", 0.3, "revision_missing"
                    matched_pn = next(
                        (
                            candidate
                            for candidate, hit_profile in pairs
                            if hit_profile.record_id == profile.record_id
                            and normalize_identifier_value(candidate) == normalize_identifier_value(pairs[0][0])
                        ),
                        pairs[0][0],
                    )
                    key = (identity.refdes, profile.record_id, method)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    links.append(self._link(
                        design, generation_id, identity.refdes, profile, matched_pn,
                        method, status, max(confidence, 0.55 if status == "verified" else confidence),
                        source_locator={
                            "identifier_namespace": identifier.namespace,
                            "raw_value": identifier.raw_value,
                            **({"degrade_reason": degrade_reason} if degrade_reason else {}),
                        },
                    ))
            if links:
                by_refdes[identity.refdes] = links
        merged: list[ComponentDatasheetLink] = []
        for refdes in sorted(by_refdes):
            seen: set[tuple[int, str, str]] = set()
            for link in by_refdes[refdes]:
                key = (link.datasheet_record_id, link.match_method, link.link_status)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(link)
        return merged

    @staticmethod
    def _link(
        design: Any,
        generation_id: str,
        refdes: str,
        profile: DocumentProfileSnapshot,
        matched_pn: str,
        method: str,
        status: str,
        confidence: float,
        source_locator: dict[str, Any],
    ) -> ComponentDatasheetLink:
        return ComponentDatasheetLink(
            circuit_id=design.design_id,
            refdes=refdes,
            datasheet_record_id=profile.record_id,
            matched_part_number=matched_pn,
            manufacturer=profile.manufacturer,
            document_revision=profile.revision,
            document_fingerprint=profile.content_hash,
            circuit_generation_id=generation_id,
            document_source_version_id=profile.source_version_id,
            remote_document_id=profile.remote_document_id,
            match_method=method,
            link_status=status,
            source_locator=source_locator,
            confidence=confidence,
        )

    # ── read-time validation ──────────────────────────────────────────────

    def get_verified_datasheet_links(
        self,
        kb_name: str,
        department_id: str,
        design_id: str,
        refdes: str | None = None,
        current_generation_id: str | None = None,
    ) -> list[ComponentDatasheetLink]:
        """Return only links that still pass both-side validation right now."""
        if self.document_store is None:
            return []
        stored = [
            link
            for link in self.load_links(kb_name, design_id)
            if link.link_status == "verified"
            and (refdes is None or link.refdes.casefold() == refdes.casefold())
        ]
        if not stored:
            return []
        generation = current_generation_id
        if self.store is not None and hasattr(self.store, "load"):
            design = self.store.load(kb_name, design_id)
            if design is not None:
                generation = current_generation_id or self._generation_of(design)
        validated: list[ComponentDatasheetLink] = []
        for link in stored:
            if generation and link.circuit_generation_id and link.circuit_generation_id != generation:
                continue  # stale circuit generation → reject immediately
            record = None
            try:
                record = self.document_store.get_document_by_id_scoped(link.datasheet_record_id, department_id)
            except Exception:
                record = None
            if record is None or record.kb_name != kb_name:
                continue  # deleted / cross-department / permission revoked
            if str(record.content_hash or "") != str(link.document_fingerprint or ""):
                continue  # document replaced/revised
            if link.document_source_version_id and str(record.source_version_id or "") != str(
                link.document_source_version_id
            ):
                continue
            validated.append(link)
        return validated

    def verified_links_for_question(
        self,
        kb_name: str,
        question: str,
        ctx: Any,
        query_engine: Any,
    ) -> list[dict[str, Any]]:
        """Resolve the question's role mention and return verified links.

        Output shape matches ``_derived_datasheet_calls`` expectations:
        ``[{refdes, part_number, record_ids}]`` — only for authorized designs.
        """
        from src.pipelines.document_rag.schemas import RequestContext  # noqa: F401
        from src.circuit.question_analysis import extract_role_term

        term = extract_role_term(question)
        if not term:
            return []
        department_id = str((getattr(ctx, "metadata", {}) or {}).get("department_id") or "")
        if not department_id:
            return []
        resolution = query_engine.resolve_component_identity(kb_name, term, allowed_design_ids=None)
        if resolution.resolution_status not in {"unique", "ambiguous"}:
            return []
        results: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in resolution.candidates:
            links = self.get_verified_datasheet_links(
                kb_name, department_id, candidate.design_id, refdes=candidate.refdes
            )
            for link in links:
                key = (candidate.design_id, link.refdes)
                entry = results.setdefault(
                    key,
                    {
                        "refdes": link.refdes,
                        "design_id": candidate.design_id,
                        "part_number": link.matched_part_number,
                        "record_ids": [],
                    },
                )
                if link.datasheet_record_id not in entry["record_ids"]:
                    entry["record_ids"].append(link.datasheet_record_id)
        return [item for item in results.values() if item["record_ids"]]

    def _generation_of(self, design: Any) -> str:
        try:
            from src.circuit.store import circuit_generation_id

            return circuit_generation_id(design)
        except Exception:
            return ""


def entry_confidence(method: str) -> float:
    return 0.95 if method == "exact_mpn" else 0.9 if method == "internal_pdn_catalog" else 0.5


__all__ = [
    "LINKS_FILE",
    "ComponentDatasheetLinkIndex",
    "DatasheetCatalogEntry",
    "DocumentProfileSnapshot",
    "split_mpn_candidates",
]
