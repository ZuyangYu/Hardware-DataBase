"""Read-side query API over ``CircuitStore``.

All four ``search_*`` methods take both a raw ``query`` string AND an optional
``keywords`` list. The list is intended to be populated by the LLM router with
synonym-expanded forms (e.g. user typed "电源模块" → ``keywords=["电源",
"power", "PWR", "VCC", "VDD"]``). When ``keywords`` is provided, it bypasses
the local tokenizer entirely — the model already knows the user's intent and
the EDF field conventions better than a regex can. When it's empty, we fall
back to the legacy regex tokenizer over ``query`` so rule-route callers and
old tests still work.

Matching is OR over needles: any single needle being a (case-insensitive)
substring of the field bag counts as a hit. ``MIN_NEEDLE_LEN`` filters
1-character tokens to keep noise like "U" or "C" from matching every refdes.

Stage 2 (vector index) augmentation: when an embedding model is configured
and ``CircuitVectorIndex`` has rows for the design, semantic neighbours of
the raw query get unioned with the keyword hits. Stage 1 still drives
precision (refdes/exact-name); Stage 2 drives recall (语义 ≈ 同义词).
"""

from __future__ import annotations

import os
import re
from collections import deque
from typing import Iterable, Sequence

from src.circuit.parsers.edf_power import classify_net_name, classify_power_pin_name
from src.circuit.relations.derivers import RelationDeriver
from src.circuit.relations.extractor import RelationExtractor
from src.circuit.relations.views import build_power_tree_view
from src.circuit.store import CircuitStore, derive_circuit_aliases
from src.circuit.vector_index import (
    KIND_INSTANCE,
    KIND_MODULE,
    KIND_NET,
    CircuitVectorIndex,
    default_circuit_vector_index,
)


# Minimum needle length. Single Latin chars (`U`, `C`) substring-match almost
# every refdes; single CJK chars rarely appear meaningfully on their own. We
# keep the cutoff conservative — 2 covers both `U1` and `RX`.
MIN_NEEDLE_LEN = 2

_INPUT_PIN_NAMES = {
    "VIN", "IN", "PVIN", "AVIN", "VBAT", "BATT", "BAT", "VBIAS", "VCCIN", "VDDIN",
}
_INPUT_SUPPLY_PIN_NAMES = {"VCC", "VDD", "AVDD", "DVDD", "VCCA", "VDDD"}
_OUTPUT_PIN_NAMES = {
    "VOUT", "OUT", "OUTPUT", "VREG", "VLDO", "LDO", "BUCK", "VCCOUT", "VDDOUT",
}
_SWITCH_PIN_NAMES = {"SW", "LX", "PH", "BOOTSW"}
_ENABLE_PIN_NAMES = {"EN", "ENABLE", "ON", "CE", "SHDN", "RUN"}
_POWER_GOOD_PIN_NAMES = {"PG", "PGOOD", "PWRGD", "POWERGOOD", "POK", "RESETB"}
_GROUND_PIN_NAMES = {"GND", "PGND", "AGND", "DGND", "EP", "PAD"}

_POWER_DEVICE_PATTERNS = (
    ("pmic", re.compile(r"(^|[^A-Z0-9])(PMIC|POWER\s*MANAGEMENT)([^A-Z0-9]|$)", re.IGNORECASE), 0.76),
    ("buck_regulator", re.compile(r"(^|[^A-Z0-9])(BUCK|DCDC|DC[\s_-]*DC|STEP[\s_-]*DOWN)([^A-Z0-9]|$)", re.IGNORECASE), 0.76),
    ("boost_regulator", re.compile(r"(^|[^A-Z0-9])(BOOST|STEP[\s_-]*UP)([^A-Z0-9]|$)", re.IGNORECASE), 0.74),
    ("ldo_regulator", re.compile(r"(^|[^A-Z0-9])(LDO|LINEAR\s*REG)([^A-Z0-9]|$)", re.IGNORECASE), 0.76),
    ("regulator", re.compile(r"(^|[^A-Z0-9])(REGULATOR|VREG|REG)([^A-Z0-9]|$)", re.IGNORECASE), 0.66),
    ("load_switch", re.compile(r"(^|[^A-Z0-9])(LOAD\s*SW|LOADSW|POWER\s*SW|SWITCH)([^A-Z0-9]|$)", re.IGNORECASE), 0.66),
)


def _prepare_needles(keywords: Sequence[str] | None, fallback_query: str, pattern: str) -> list[str]:
    """Build the OR-matched needle list.

    Order:
    1. If ``keywords`` is non-empty, use it verbatim (uppercased + trimmed),
       skipping entries shorter than ``MIN_NEEDLE_LEN``.
    2. Otherwise fall back to the regex tokenizer over ``fallback_query`` —
       this is the legacy path the rule router still uses.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    if keywords:
        for token in keywords:
            text = str(token or "").strip()
            if len(text) < MIN_NEEDLE_LEN:
                continue
            key = text.upper()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        if cleaned:
            return cleaned
    # Fallback: tokenize the raw query.
    if fallback_query:
        for token in re.findall(pattern, fallback_query):
            text = token.strip()
            if len(text) < MIN_NEEDLE_LEN:
                continue
            key = text.upper()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
    return cleaned


def _pin_key(name: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(name or "").upper().lstrip("&"))


def _classify_power_topology_pin(pin_name: str | None, net_name: str | None, rail_names: set[str]) -> str:
    pin = _pin_key(pin_name)
    net_role = classify_net_name(net_name or "")
    if pin in _GROUND_PIN_NAMES or net_role == "ground":
        return "ground"
    if pin in _ENABLE_PIN_NAMES or pin.endswith("EN") or pin.endswith("ENABLE"):
        return "enable"
    if pin in _POWER_GOOD_PIN_NAMES or "PGOOD" in pin or "PWRGD" in pin:
        return "power_good"
    if pin in _SWITCH_PIN_NAMES:
        return "switch_node"
    if pin in _INPUT_PIN_NAMES or pin.startswith("VIN") or pin.endswith("VIN"):
        return "input"
    if pin in _OUTPUT_PIN_NAMES or pin.startswith("VOUT") or pin.endswith("OUT"):
        return "output"
    if pin in _INPUT_SUPPLY_PIN_NAMES and (net_role == "power" or (net_name or "") in rail_names):
        return "input"
    if classify_power_pin_name(pin_name) == "ground":
        return "ground"
    if net_role == "power" or (net_name or "") in rail_names:
        return "other_power"
    return "other"


def _classify_power_device(inst) -> tuple[str | None, float]:
    refdes = str(getattr(inst, "refdes", "") or "").upper()
    descriptor = " ".join(
        str(value or "")
        for value in (getattr(inst, "library_cell", None), getattr(inst, "part_number", None), getattr(inst, "value", None))
    )
    if refdes.startswith("FB"):
        return "ferrite_bead", 0.7
    if refdes.startswith("F"):
        return "fuse", 0.7
    if refdes.startswith("L"):
        return "series_passive", 0.48
    for device_type, pattern, confidence in _POWER_DEVICE_PATTERNS:
        if pattern.search(descriptor):
            return device_type, confidence
    return None, 0.42


def _looks_like_non_power_source(inst) -> bool:
    """Filter devices that expose VDD/OUT pins but are not power converters."""
    refdes = str(getattr(inst, "refdes", "") or "").upper()
    descriptor = " ".join(
        str(value or "").upper()
        for value in (getattr(inst, "library_cell", None), getattr(inst, "part_number", None), getattr(inst, "value", None))
    )
    return refdes.startswith(("Y", "X")) or any(term in descriptor for term in ("CRYSTAL", "OSC", "XTAL"))


def _unique_pin_rows(rows: Iterable[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for row in rows:
        key = (str(row.get("pin") or ""), str(row.get("net") or ""), str(row.get("role") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(row))
    return unique


def _matches_any(haystack: str, needles: Iterable[str]) -> bool:
    """Case-insensitive OR substring match.

    ``haystack`` should already be the concatenated/upper-cased text bag for a
    candidate row. We upper-case the needles here instead of caching them so
    callers can pass through CJK strings untouched (CJK has no case fold but
    `.upper()` is still a no-op safe operation).
    """
    if not needles:
        return True
    upper = haystack.upper()
    for needle in needles:
        if needle.upper() in upper:
            return True
    return False


def _normalize_alias(value: str | None) -> str:
    """Normalize a circuit alias for matching.

    Lower-cases, collapses separators to spaces and strips them, while
    preserving CJK (so ``主板`` / ``电源板`` survive). Lets ``main_board``,
    ``main-board`` and ``main board`` all resolve to the same circuit.
    """
    return re.sub(r"[^a-z0-9一-鿿]+", " ", (value or "").lower()).strip()


def _first_module_name(refdes: str, membership: dict[str, list[dict]]) -> str | None:
    """First module name a refdes belongs to, or ``None`` when unmapped.

    ``membership`` is the ``{refdes: [{"module_id", "module_name"}]}`` map
    produced by :meth:`CircuitQueryEngine._module_membership`.
    """
    mods = membership.get(refdes, [])
    return mods[0]["module_name"] if mods else None


class CircuitQueryEngine:
    def __init__(
        self,
        store: CircuitStore | None = None,
        vector_index: CircuitVectorIndex | None = None,
    ):
        self.store = store or CircuitStore()
        # Stage 2 augmentation. Pass ``CircuitVectorIndex()`` (or a no-op
        # double) to disable in tests. The default singleton degrades to a
        # no-op when no embedding model is bound, so leaving it on is safe.
        self.vector_index = vector_index or default_circuit_vector_index

    def list_designs(self, kb_name: str) -> list[dict]:
        return [
            {
                "design_id": design.design_id,
                "status": str(design.status),
                "files": [file.file_name for file in design.files],
                "instance_count": len(design.instances),
                "net_count": len(design.nets),
                "module_count": len(design.modules),
            }
            for design in self.store.list_designs(kb_name)
        ]

    def get_design_summary(self, kb_name: str, design_id: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if not design:
            return None
        return {
            "design_id": design.design_id,
            "status": str(design.status),
            "instances": len(design.instances),
            "nets": len(design.nets),
            "modules": [
                {
                    "module_id": module.module_id,
                    "name": module.name,
                    "instance_count": len(module.instances),
                    "net_count": len(module.nets),
                }
                for module in design.modules
            ],
            "power_nets": [net.name for net in design.nets if net.net_type in {"power", "ground"}],
            "clock_nets": [net.name for net in design.nets if net.net_type == "clock"],
            "warnings": design.parse_warnings,
        }

    def list_modules(self, kb_name: str, design_id: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        instance_by_refdes = {inst.refdes: inst for inst in design.instances}
        modules = [self._module_to_row(design, module, instance_by_refdes, max_instances=50) for module in design.modules]
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "module_count": len(modules),
            "modules": modules,
        }

    def get_circuit_overview(self, kb_name: str, design_id: str) -> dict | None:
        summary = self.get_design_summary(kb_name, design_id)
        if summary is None:
            return None
        design = self.store.load(kb_name, design_id)
        summary["kb_name"] = kb_name
        summary["circuit_id"] = summary.get("design_id")
        summary["source_files"] = [file.file_name for file in design.files] if design else []
        summary["instance_count"] = summary.get("instances", 0)
        summary["net_count"] = summary.get("nets", 0)
        summary["module_count"] = len(summary.get("modules", []))
        return summary

    def get_module_detail(self, kb_name: str, design_id: str, module_id_or_name: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        module = self._find_module_by_id_or_name(design, module_id_or_name)
        if module is None:
            return None
        instance_by_refdes = {inst.refdes: inst for inst in design.instances}
        row = self._module_to_row(design, module, instance_by_refdes, max_instances=1000)
        row["kb_name"] = kb_name
        row["circuit_id"] = design.design_id
        row["source_files"] = [file.file_name for file in design.files]
        return row

    def get_module_instances(self, kb_name: str, design_id: str, module_id_or_name: str) -> dict | None:
        row = self.get_module_detail(kb_name, design_id, module_id_or_name)
        if row is not None:
            row["query_kind"] = "module_instances"
        return row

    def get_instance_connections(self, kb_name: str, design_id: str, refdes: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        instance = self._find_instance_by_refdes(design, refdes)
        if instance is None:
            return None
        net_by_name = {net.name: net for net in design.nets}
        # Map each pin of this instance to the net it sits on by scanning net
        # connections (the EDF net list is the source of truth for connectivity).
        net_of_pin: dict[str, str] = {}
        for net in design.nets:
            for conn in net.connections:
                if self._normalize_refdes(conn.refdes) == self._normalize_refdes(instance.refdes) and conn.pin:
                    net_of_pin[conn.pin] = net.name
        pins = []
        pin_names = [pin.name for pin in instance.pins] or list(net_of_pin.keys())
        for pin_name in pin_names:
            net_name = net_of_pin.get(pin_name)
            peers: list[str] = []
            if net_name and net_name in net_by_name:
                for conn in net_by_name[net_name].connections:
                    if self._normalize_refdes(conn.refdes) == self._normalize_refdes(instance.refdes):
                        continue
                    endpoint = f"{conn.refdes}.{conn.pin}" if conn.pin else conn.refdes
                    peers.append(endpoint)
            pins.append({"name": pin_name, "net_name": net_name, "peers": peers})
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "refdes": instance.refdes,
            "library_cell": instance.library_cell,
            "part_number": instance.part_number,
            "pins": pins,
        }

    def get_net_connections(self, kb_name: str, design_id: str, net_name: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        net = self._find_net_by_name(design, net_name)
        if net is None:
            return None
        instance_by_refdes = {inst.refdes: inst for inst in design.instances}
        membership = self._module_membership(design)
        connections = []
        for conn in net.connections:
            instance = instance_by_refdes.get(conn.refdes)
            modules = membership.get(conn.refdes, [])
            connections.append(
                {
                    "refdes": conn.refdes,
                    "pin": conn.pin,
                    "endpoint": f"{conn.refdes}.{conn.pin}" if conn.pin else conn.refdes,
                    "library_cell": instance.library_cell if instance else None,
                    "part_number": instance.part_number if instance else None,
                    "module_ids": [item["module_id"] for item in modules],
                    "module_names": [item["module_name"] for item in modules],
                }
            )
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "net_name": net.name,
            "name": net.name,
            "net_type": net.net_type,
            "connection_count": len(net.connections),
            "connections": connections,
        }

    def search_net_connections(
        self,
        kb_name: str,
        query: str = "",
        keywords: Sequence[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        rows = []
        for hit in self.search_nets(kb_name, query, keywords=keywords, limit=limit):
            row = self.get_net_connections(kb_name, hit.get("design_id"), hit.get("name"))
            if row:
                rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def resolve_circuit_by_file(self, kb_name: str, source_file: str) -> list[dict]:
        target = (source_file or "").strip().lower()
        if not target:
            return []
        target_base = target.rsplit("/", 1)[-1]
        matches = []
        for design in self.store.list_designs(kb_name):
            files = [file.file_name for file in design.files]
            for file_name in files:
                name = (file_name or "").lower()
                if name == target or name.rsplit("/", 1)[-1] == target_base:
                    matches.append({
                        "design_id": design.design_id,
                        "circuit_id": design.design_id,
                        "source_files": files,
                        "status": str(design.status),
                    })
                    break
        return matches

    def search_entity_across_circuits(
        self,
        kb_name: str,
        entity_query: str,
        entity_type: str | None = None,
        current_circuit_id: str | None = None,
    ) -> list[dict]:
        """Find an entity across every circuit in ``kb_name`` (plan §4.7).

        Results are ranked by the plan §4.7 reduce factors: session-current
        circuit first, then uniqueness (entities that appear in only one
        circuit rank higher), then source-confidence (EDF exact > EDF graph >
        PDF visual > LLM guess), then most-recently-uploaded. The caller
        (``CircuitScopeResolver``) decides single / multiple / unresolved from
        the ranked set.
        """
        query = self._normalize_entity(entity_query)
        if not query:
            return []
        results = []
        index_entries = self._read_index_entries(kb_name)
        for design in self.store.list_designs(kb_name):
            files = [file.file_name for file in design.files]
            if entity_type in (None, "instance"):
                normalized_refdes = self._normalize_refdes(entity_query)
                for inst in design.instances:
                    if self._normalize_refdes(inst.refdes) == normalized_refdes:
                        results.append({
                            "design_id": design.design_id,
                            "circuit_id": design.design_id,
                            "entity_type": "instance",
                            "entity_id": inst.refdes,
                            "display_name": inst.refdes,
                            "source_files": files,
                            "confidence": 1.0,
                        })
            if entity_type in (None, "module"):
                for module in design.modules:
                    if query in {self._normalize_entity(module.module_id), self._normalize_entity(module.name)}:
                        results.append({
                            "design_id": design.design_id,
                            "circuit_id": design.design_id,
                            "entity_type": "module",
                            "entity_id": module.module_id,
                            "display_name": module.name,
                            "source_files": files,
                            "confidence": 1.0,
                        })
            if entity_type in (None, "net"):
                for net in design.nets:
                    if self._normalize_entity(net.name) == query:
                        results.append({
                            "design_id": design.design_id,
                            "circuit_id": design.design_id,
                            "entity_type": "net",
                            "entity_id": net.name,
                            "display_name": net.name,
                            "source_files": files,
                            "confidence": 1.0,
                        })
        return self.rank_cross_circuit_candidates(results, current_circuit_id=current_circuit_id, index_entries=index_entries)

    def rank_cross_circuit_candidates(
        self,
        results: list[dict],
        current_circuit_id: str | None = None,
        index_entries: dict[str, dict] | None = None,
    ) -> list[dict]:
        """Rank cross-circuit candidates by plan §4.7 factors.

        Factors (descending priority):
        1. session-current circuit relevance (current_circuit_id first);
        2. match uniqueness — entities appearing in fewer circuits rank higher;
        3. source-confidence (EDF exact 1.0 > EDF graph > PDF visual > LLM);
        4. most-recently-uploaded (index ``updated_at``);
        5. display-name text match.

        ``index_entries`` is an optional ``design_id → index entry`` map; when
        omitted it is read from the store. Ranking never merges entities across
        circuits (plan §4.7 constraint).
        """
        if not results:
            return results
        if index_entries is None:
            index_entries = self._read_index_entries(results[0].get("kb_name") or "")
        # Uniqueness: how many distinct circuits hold the same entity id.
        circuit_sets: dict[str, set[str]] = {}
        for item in results:
            key = self._normalize_entity(item.get("entity_id"))
            circuit_sets.setdefault(key, set()).add(item.get("circuit_id") or item.get("design_id"))

        def _sort_key(item: dict) -> tuple:
            cid = item.get("circuit_id") or item.get("design_id")
            entity_key = self._normalize_entity(item.get("entity_id"))
            # 1. current circuit first (True sorts after False, so negate).
            current_first = 0 if cid == current_circuit_id else 1
            # 2. uniqueness — fewer circuits holding this entity = higher rank.
            uniqueness = len(circuit_sets.get(entity_key, {cid}))
            # 3. source confidence.
            confidence = float(item.get("confidence") or 0.0)
            # 4. recency — parse updated_at into a comparable int (0 if absent).
            entry = index_entries.get(cid, {})
            updated = entry.get("updated_at") or ""
            recency = updated.replace("-", "").replace(":", "").replace("T", "").replace("Z", "")
            recency_rank = -int(recency) if recency.isdigit() else 0
            # 5. display name length (shorter exact id ranks higher).
            name_len = len(str(item.get("display_name") or ""))
            return (current_first, uniqueness, -confidence, recency_rank, name_len)

        return sorted(results, key=_sort_key)

    def _read_index_entries(self, kb_name: str) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        for entry in self.store.read_index().get("designs", []):
            if not kb_name or entry.get("kb_name") == kb_name:
                entries[entry.get("design_id")] = entry
        return entries

    def search_instances(
        self,
        kb_name: str,
        query: str = "",
        limit: int = 20,
        keywords: Sequence[str] | None = None,
    ) -> list[dict]:
        # Instance fields are refdes/library_cell/MPN/footprint/value/erp — a
        # mix of Latin identifiers ("STM32F407") and free text. The legacy
        # regex preserved here covers refdes-style tokens plus longer alnum
        # strings; the keyword path bypasses it entirely.
        needles = _prepare_needles(
            keywords,
            query,
            r"[A-Za-z]+\d+(?:-\d+)?|[A-Za-z0-9_.-]{3,}",
        )
        designs = self.store.list_designs(kb_name)
        results: list[dict] = []
        seen_refdes: set[tuple[str, str]] = set()
        has_filter_query = bool((query or "").strip() or keywords)
        # If a non-empty query tokenizes to zero needles (common for pure
        # Chinese queries like "主控"), do NOT treat that as "match all".
        # Skip keyword recall and let Stage 2 semantic recall try.
        if not (has_filter_query and not needles):
            for design in designs:
                for instance in design.instances:
                    haystack = " ".join(
                        str(value or "")
                        for value in [
                            instance.refdes,
                            instance.library_cell,
                            instance.part_number,
                            instance.footprint,
                            instance.value,
                            instance.erp_number,
                        ]
                    )
                    if not _matches_any(haystack, needles):
                        continue
                    key = (design.design_id, instance.refdes)
                    if key in seen_refdes:
                        continue
                    seen_refdes.add(key)
                    results.append(self._instance_to_row(design, instance))
                    if len(results) >= limit:
                        return results

        # Stage 2: semantic recall. Only fires when keyword recall hasn't
        # already filled `limit`. We restrict to instance docs so module/net
        # neighbours don't pollute the result type.
        if query and len(results) < limit:
            hits = self._semantic_supplement(kb_name, query, [KIND_INSTANCE], limit)
            for hit in hits:
                key = (hit.design_id, hit.natural_id)
                if key in seen_refdes:
                    continue
                instance = self._find_instance(designs, hit.design_id, hit.natural_id)
                if instance is None:
                    continue
                seen_refdes.add(key)
                results.append(self._instance_to_row(self._find_design(designs, hit.design_id), instance))
                if len(results) >= limit:
                    break
        return results

    def search_nets(
        self,
        kb_name: str,
        query: str = "",
        limit: int = 20,
        keywords: Sequence[str] | None = None,
    ) -> list[dict]:
        # Net names are short Latin identifiers (VOUT, GND, USB_DP, CLK_24M).
        # The fallback tokenizer allows ./+- so net names like "3.3V" or
        # "USB+" survive intact.
        needles = _prepare_needles(keywords, query, r"[A-Za-z0-9_./+-]{2,}")
        designs = self.store.list_designs(kb_name)
        results: list[dict] = []
        seen: set[tuple[str, str]] = set()
        has_filter_query = bool((query or "").strip() or keywords)
        if not (has_filter_query and not needles):
            for design in designs:
                for net in design.nets:
                    if not _matches_any(net.name, needles):
                        continue
                    key = (design.design_id, net.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(self._net_to_row(design, net))
                    if len(results) >= limit:
                        return results

        if query and len(results) < limit:
            hits = self._semantic_supplement(kb_name, query, [KIND_NET], limit)
            for hit in hits:
                key = (hit.design_id, hit.natural_id)
                if key in seen:
                    continue
                net = self._find_net(designs, hit.design_id, hit.natural_id)
                if net is None:
                    continue
                seen.add(key)
                results.append(self._net_to_row(self._find_design(designs, hit.design_id), net))
                if len(results) >= limit:
                    break
        return results

    def search_cross_references(
        self,
        kb_name: str,
        query: str = "",
        limit: int = 20,
        keywords: Sequence[str] | None = None,
    ) -> list[dict]:
        # Cross references couple an EDF refdes (U1) with a PDF label
        # (probably a free-text annotation). The legacy code treated the
        # whole query as one needle; preserve that when keywords is empty
        # (fallback regex matches any alnum chunk) but also accept the
        # LLM-supplied keyword list when present.
        needles = _prepare_needles(keywords, query, r"[A-Za-z0-9_.-]{2,}")
        if not needles and query.strip():
            # Truly tiny / pure-CJK queries: preserve old behaviour by using
            # the raw query as a single needle.
            needles = [query.strip()]
        results = []
        for design in self.store.list_designs(kb_name):
            for ref in design.cross_references:
                haystack = f"{ref.edf_refdes} {ref.pdf_label}"
                if not _matches_any(haystack, needles):
                    continue
                results.append(
                    {
                        "design_id": design.design_id,
                        "edf_refdes": ref.edf_refdes,
                        "pdf_label": ref.pdf_label,
                        "page_number": ref.page_number,
                        "confidence": ref.confidence,
                        "strategy": ref.strategy,
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    def search_modules(
        self,
        kb_name: str,
        query: str = "",
        limit: int = 10,
        max_instances: int = 50,
        keywords: Sequence[str] | None = None,
    ) -> list[dict]:
        """Search EDF/schematic modules by name.

        EDF parsing groups instances into ``CircuitModule`` objects (e.g.
        "MCU module", "Power Supply", "USB Interface"). The router/LLM uses
        this method to answer questions like "MCU 模块都包含哪些器件" by
        expanding the module's ``instances`` field into the full part list
        (refdes / library_cell / MPN / footprint).

        The keyword path lets the LLM hand us a synonym-expanded list — the
        single biggest precision/recall win for module queries, because EDF
        module names are usually English while users type Chinese ("电源",
        "主控", "调试").

        Stage 2 augmentation: when keyword recall is empty / partial AND the
        vector index has a row for a module that semantically matches the
        query, that module is appended after the keyword hits.
        """
        # Module-name fallback tokenizer: both Latin identifiers AND raw CJK
        # runs (".strip()" alone preserved Chinese characters, which the
        # legacy `[A-Za-z]+...` regex would have dropped).
        if keywords:
            needles = _prepare_needles(keywords, query, r"")
        else:
            latin_tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", query or "")
            cjk_tokens = re.findall(r"[一-鿿]+", query or "")
            needles = _prepare_needles(None, "", "") or []
            for token in latin_tokens + cjk_tokens:
                if len(token) >= MIN_NEEDLE_LEN and token not in needles:
                    needles.append(token)

        designs = self.store.list_designs(kb_name)
        results: list[dict] = []
        seen: set[tuple[str, str]] = set()
        has_filter_query = bool((query or "").strip() or keywords)
        if not (has_filter_query and not needles):
            for design in designs:
                instance_by_refdes = {inst.refdes: inst for inst in design.instances}
                for module in design.modules:
                    if not _matches_any(module.name, needles):
                        continue
                    key = (design.design_id, module.module_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(self._module_to_row(design, module, instance_by_refdes, max_instances))
                    if len(results) >= limit:
                        return results

        if query and len(results) < limit:
            hits = self._semantic_supplement(kb_name, query, [KIND_MODULE], limit)
            for hit in hits:
                key = (hit.design_id, hit.natural_id)
                if key in seen:
                    continue
                module = self._find_module(designs, hit.design_id, hit.natural_id)
                design = self._find_design(designs, hit.design_id)
                if module is None or design is None:
                    continue
                seen.add(key)
                instance_by_refdes = {inst.refdes: inst for inst in design.instances}
                results.append(self._module_to_row(design, module, instance_by_refdes, max_instances))
                if len(results) >= limit:
                    break
        return results

    def get_module_power_nets(self, kb_name: str, design_id: str, module_id_or_name: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        module = self._find_module_by_id_or_name(design, module_id_or_name)
        if module is None:
            return None
        instance_by_refdes = {inst.refdes: inst for inst in design.instances}
        net_by_name = {net.name: net for net in design.nets}
        row = self._module_power_nets_row(design, module, instance_by_refdes, net_by_name)
        row["kb_name"] = kb_name
        row["circuit_id"] = design.design_id
        row["source_files"] = [file.file_name for file in design.files]
        return row

    def search_module_power_nets(
        self,
        kb_name: str,
        query: str = "",
        keywords: Sequence[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Return supply and reference-ground nets for matching modules."""
        module_hits = self.search_modules(kb_name, query, keywords=keywords, limit=limit)
        if not module_hits:
            return []
        designs = self.store.list_designs(kb_name)
        results: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for hit in module_hits:
            design = self._find_design(designs, hit.get("design_id"))
            if design is None:
                continue
            module = self._find_module(designs, design.design_id, hit.get("module_id"))
            if module is None:
                continue
            key = (design.design_id, module.module_id)
            if key in seen:
                continue
            seen.add(key)
            instance_by_refdes = {inst.refdes: inst for inst in design.instances}
            net_by_name = {net.name: net for net in design.nets}
            row = self._module_power_nets_row(design, module, instance_by_refdes, net_by_name)
            row["kb_name"] = kb_name
            row["circuit_id"] = design.design_id
            row["source_files"] = [file.file_name for file in design.files]
            results.append(row)
            if len(results) >= limit:
                break
        return results

    def _module_power_nets_row(self, design, module, instance_by_refdes, net_by_name) -> dict:
        power_nets: list[dict] = []
        ground_nets: list[dict] = []
        for net_name in module.nets:
            net = net_by_name.get(net_name)
            if net is None:
                continue
            role, reason = self._infer_supply_net_role(net, module, instance_by_refdes)
            if role not in {"power", "ground"}:
                continue
            row = {
                "name": net.name,
                "net_type": net.net_type,
                "role": role,
                "reason": reason,
                "connection_count": len(net.connections),
                "connections": [f"{c.refdes}.{c.pin}" for c in net.connections[:20]],
            }
            if role == "ground":
                ground_nets.append(row)
            else:
                power_nets.append(row)
        return {
            "design_id": design.design_id,
            "module_id": module.module_id,
            "module": module.name,
            "instance_count": len(module.instances),
            "net_count": len(module.nets),
            "power_nets": power_nets,
            "ground_nets": ground_nets,
            "supply_net_count": len(power_nets) + len(ground_nets),
        }

    @staticmethod
    def _infer_supply_net_role(net, module, instance_by_refdes) -> tuple[str | None, str]:
        if net.net_type in {"power", "ground"}:
            return net.net_type, f"net_type={net.net_type}"
        name_role = classify_net_name(net.name)
        if name_role in {"power", "ground"}:
            return name_role, f"网络名 `{net.name}`"
        module_refs = set(module.instances)
        for conn in net.connections:
            if conn.refdes not in module_refs:
                continue
            pin_role = classify_power_pin_name(conn.pin)
            if pin_role:
                return pin_role, f"引脚 `{conn.pin}`"
        for refdes in module.instances:
            inst = instance_by_refdes.get(refdes)
            if inst is None:
                continue
            for pin in inst.pins:
                if pin.net != net.name:
                    continue
                pin_role = classify_power_pin_name(pin.name)
                if pin_role:
                    return pin_role, f"引脚 `{pin.name}`"
        return None, ""

    # ── row-builders & semantic glue ──────────────────────────────────────

    @staticmethod
    def _instance_to_row(design, instance) -> dict:
        return {
            "design_id": design.design_id if design else "",
            "refdes": instance.refdes,
            "library_cell": instance.library_cell,
            "part_number": instance.part_number,
            "footprint": instance.footprint,
            "value": instance.value,
            "erp_number": instance.erp_number,
            "pin_count": len(instance.pins),
            "pins": [
                {
                    "number": pin.name,
                    "name": pin.name,
                    "net_name": pin.net,
                }
                for pin in instance.pins
            ],
        }

    @staticmethod
    def _net_to_row(design, net) -> dict:
        return {
            "design_id": design.design_id if design else "",
            "name": net.name,
            "net_type": net.net_type,
            "connection_count": len(net.connections),
            "connections": [f"{conn.refdes}.{conn.pin}" for conn in net.connections[:20]],
        }

    @staticmethod
    def _module_to_row(design, module, instance_by_refdes, max_instances) -> dict:
        instance_rows = []
        for refdes in module.instances[:max_instances]:
            inst = instance_by_refdes.get(refdes)
            if inst is None:
                instance_rows.append({"refdes": refdes})
                continue
            instance_rows.append(
                {
                    "refdes": inst.refdes,
                    "library_cell": inst.library_cell,
                    "part_number": inst.part_number,
                    "footprint": inst.footprint,
                    "value": inst.value,
                }
            )
        return {
            "design_id": design.design_id if design else "",
            "module_id": module.module_id,
            "name": module.name,
            "strategy": module.strategy,
            "instance_count": len(module.instances),
            "net_count": len(module.nets),
            "instances": instance_rows,
            "nets": list(module.nets[:20]),
            "connectivity_description": module.connectivity_description,
            "visual_description": module.visual_description,
            "merged_description": module.merged_description,
        }

    @staticmethod
    def _find_design(designs, design_id):
        for design in designs:
            if design.design_id == design_id:
                return design
        return None

    @classmethod
    def _find_instance(cls, designs, design_id, refdes):
        design = cls._find_design(designs, design_id)
        if design is None:
            return None
        for inst in design.instances:
            if inst.refdes == refdes:
                return inst
        return None

    @classmethod
    def _find_net(cls, designs, design_id, name: str):
        design = cls._find_design(designs, design_id)
        if design is None:
            return None
        return cls._find_net_by_name(design, name)

    @classmethod
    def _find_net_by_name(cls, design, name: str):
        normalized = cls._normalize_entity(name)
        lowered = str(name or "").strip().lower()
        for net in design.nets:
            if net.name.lower() == lowered:
                return net
            if cls._normalize_entity(net.name) == normalized:
                return net
        return None

    @staticmethod
    def _module_membership(design) -> dict[str, list[dict]]:
        membership: dict[str, list[dict]] = {}
        for module in design.modules:
            for refdes in module.instances:
                membership.setdefault(refdes, []).append({"module_id": module.module_id, "module_name": module.name})
        return membership
    @classmethod
    def _find_module(cls, designs, design_id, module_id):
        design = cls._find_design(designs, design_id)
        if design is None:
            return None
        for module in design.modules:
            if module.module_id == module_id:
                return module
        return None

    @staticmethod
    def _normalize_entity(value: str | None) -> str:
        return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())

    @staticmethod
    def _normalize_refdes(value: str | None) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if "/" in text:
            text = text.rsplit("/", 1)[-1]
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", text).upper()
        match = re.match(r"^([A-Z]+)0*(\d+)([A-Z0-9]*)$", cleaned)
        if match:
            prefix, digits, suffix = match.groups()
            return f"{prefix}{int(digits)}{suffix}"
        return cleaned

    @classmethod
    def _find_instance_by_refdes(cls, design, refdes: str):
        normalized = cls._normalize_refdes(refdes)
        for instance in design.instances:
            if cls._normalize_refdes(instance.refdes) == normalized:
                return instance
        return None

    @classmethod
    def _find_module_by_id_or_name(cls, design, module_id_or_name: str):
        normalized = cls._normalize_entity(module_id_or_name)
        lowered = str(module_id_or_name or "").strip().lower()
        for module in design.modules:
            if cls._normalize_entity(module.module_id) == normalized:
                return module
            if cls._normalize_entity(module.name) == normalized:
                return module
            if lowered and module.name.lower() == lowered:
                return module
        return None

    def search_locations(
        self,
        kb_name: str,
        query: str = "",
        keywords: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Locate modules / refdes on schematic pages when parser data exists."""
        results: list[dict] = []
        # Module regions first.
        modules = self.search_modules(kb_name, query, keywords=keywords, limit=limit)
        module_ids = {item.get("module_id") for item in modules}
        # Component/cross-reference matches.
        instances = self.search_instances(kb_name, query, keywords=keywords, limit=limit)
        refdes_set = {item.get("refdes") for item in instances}
        for design in self.store.list_designs(kb_name):
            module_by_id = {m.module_id: m for m in design.modules}
            for region in design.module_regions:
                if module_ids and region.module_id not in module_ids:
                    continue
                module = module_by_id.get(region.module_id)
                results.append(
                    {
                        "design_id": design.design_id,
                        "kind": "module_region",
                        "module_id": region.module_id,
                        "module_name": module.name if module else region.module_id,
                        "page_number": region.page_number,
                        "bbox": region.bbox,
                        "confidence": region.confidence,
                        "strategy": region.strategy,
                    }
                )
                if len(results) >= limit:
                    return results
            for ref in design.cross_references:
                if refdes_set and ref.edf_refdes not in refdes_set:
                    continue
                if not refdes_set and keywords:
                    haystack = f"{ref.edf_refdes} {ref.pdf_label}"
                    if not _matches_any(haystack, keywords):
                        continue
                results.append(
                    {
                        "design_id": design.design_id,
                        "kind": "cross_reference",
                        "edf_refdes": ref.edf_refdes,
                        "pdf_label": ref.pdf_label,
                        "page_number": ref.page_number,
                        "confidence": ref.confidence,
                        "strategy": ref.strategy,
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    def search_module_connections(
        self,
        kb_name: str,
        query: str = "",
        keywords: Sequence[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Find nets that connect matching modules.

        If one module matches, returns that module's nets with connection
        samples. If two or more modules match, returns the shared nets between
        module pairs. This supports questions like:
        - MCU 模块连接了哪些网络？
        - MCU 模块和电源模块之间有哪些连接？
        - USB 模块和 MCU 之间有哪些信号？
        """
        modules = self.search_modules(kb_name, query, keywords=keywords, limit=limit)
        if not modules:
            return []
        results: list[dict] = []
        designs = self.store.list_designs(kb_name)
        for design in designs:
            net_by_name = {net.name: net for net in design.nets}
            modules_in_design = [m for m in modules if m.get("design_id") == design.design_id]
            if len(modules_in_design) == 1:
                module = modules_in_design[0]
                for net_name in module.get("nets") or []:
                    net = net_by_name.get(net_name)
                    if not net:
                        continue
                    results.append(
                        {
                            "design_id": design.design_id,
                            "module": module.get("name"),
                            "net": net.name,
                            "net_type": net.net_type,
                            "connection_count": len(net.connections),
                            "connections": [f"{c.refdes}.{c.pin}" for c in net.connections[:20]],
                        }
                    )
            else:
                for i, left in enumerate(modules_in_design):
                    left_nets = set(left.get("nets") or [])
                    for right in modules_in_design[i + 1:]:
                        shared = sorted(left_nets.intersection(set(right.get("nets") or [])))
                        for net_name in shared:
                            net = net_by_name.get(net_name)
                            results.append(
                                {
                                    "design_id": design.design_id,
                                    "from_module": left.get("name"),
                                    "to_module": right.get("name"),
                                    "net": net_name,
                                    "net_type": net.net_type if net else "",
                                    "connection_count": len(net.connections) if net else 0,
                                    "connections": [f"{c.refdes}.{c.pin}" for c in (net.connections[:20] if net else [])],
                                }
                            )
            if len(results) >= limit:
                return results[:limit]
        return results[:limit]

    # ── plan §5.1 determinstic query functions ────────────────────────────

    def list_circuits(self, kb_name: str) -> list[dict]:
        """List circuits (designs) in a knowledge base with source/alias info.

        Mirrors plan §4.3's ``kb_name → circuits`` view so ``CircuitScopeResolver``
        and the UI can present source files, status and aliases per circuit.
        """
        circuits: list[dict] = []
        for design in self.store.list_designs(kb_name):
            files = [file.file_name for file in design.files]
            circuits.append(
                {
                    "kb_name": kb_name,
                    "circuit_id": design.design_id,
                    "design_id": design.design_id,
                    "name": design.design_id,
                    "source_files": files,
                    "aliases": derive_circuit_aliases(design.design_id, files),
                    "status": str(design.status),
                    "instance_count": len(design.instances),
                    "net_count": len(design.nets),
                    "module_count": len(design.modules),
                    "schematic_page_count": len(design.schematic_pages),
                }
            )
        return circuits

    def resolve_circuit_by_alias(self, kb_name: str, alias: str) -> list[dict]:
        """Resolve a circuit by alias / file stem / spaced id form.

        Falls back to the EDF-file matcher when the alias is actually a
        filename. Resolution is always scoped to ``kb_name`` — an alias that
        only matches a circuit in another knowledge base is never returned
        (plan §4.8).
        """
        target = _normalize_alias(alias)
        if not target:
            return []
        if target.endswith(".edf") or target.endswith(".edif") or target.endswith(".pdf"):
            return self.resolve_circuit_by_file(kb_name, alias)
        matches: list[dict] = []
        for design in self.store.list_designs(kb_name):
            files = [file.file_name for file in design.files]
            aliases = derive_circuit_aliases(design.design_id, files)
            if target in {_normalize_alias(item) for item in aliases}:
                matches.append(
                    {
                        "design_id": design.design_id,
                        "circuit_id": design.design_id,
                        "source_files": files,
                        "aliases": aliases,
                        "status": str(design.status),
                    }
                )
        return matches

    def get_instance_detail(self, kb_name: str, design_id: str, refdes: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        instance = self._find_instance_by_refdes(design, refdes)
        if instance is None:
            return None
        net_by_name = {net.name: net for net in design.nets}
        pins = []
        for pin in instance.pins:
            net_name = pin.net
            peers: list[str] = []
            net = net_by_name.get(net_name or "")
            if net:
                peers = [
                    f"{conn.refdes}.{conn.pin}" if conn.pin else conn.refdes
                    for conn in net.connections
                    if self._normalize_refdes(conn.refdes) != self._normalize_refdes(instance.refdes)
                ][:20]
            pins.append({"name": pin.name, "net_name": net_name, "peers": peers})
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "refdes": instance.refdes,
            "library_cell": instance.library_cell,
            "part_number": instance.part_number,
            "footprint": instance.footprint,
            "value": instance.value,
            "erp_number": instance.erp_number,
            "properties": instance.properties,
            "pin_count": len(instance.pins),
            "pins": pins,
            "net_names": [pin["net_name"] for pin in pins if pin["net_name"]],
        }

    def get_net_detail(self, kb_name: str, design_id: str, net_name: str) -> dict | None:
        return self.get_net_connections(kb_name, design_id, net_name)

    def get_module_interfaces(self, kb_name: str, design_id: str, module_id_or_name: str) -> dict | None:
        """Return the nets a module exposes to the rest of the circuit.

        An "interface" net belongs to the module but also connects at least one
        instance *outside* the module — the module's external edge. Purely
        internal nets are excluded. Powers plan intent ``module_interfaces``.
        """
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        module = self._find_module_by_id_or_name(design, module_id_or_name)
        if module is None:
            return None
        module_refs = {self._normalize_refdes(ref) for ref in module.instances}
        net_by_name = {net.name: net for net in design.nets}
        membership = self._module_membership(design)
        interfaces: list[dict] = []
        for net_name in module.nets:
            net = net_by_name.get(net_name)
            if net is None:
                continue
            external = [
                conn for conn in net.connections
                if self._normalize_refdes(conn.refdes) not in module_refs
            ]
            if not external:
                continue
            external_modules = sorted(
                {
                    m["module_name"]
                    for conn in external
                    for m in membership.get(conn.refdes, [])
                    if m["module_id"] != module.module_id
                }
            )
            interfaces.append(
                {
                    "net_name": net.name,
                    "net_type": net.net_type,
                    "external_endpoints": [
                        f"{conn.refdes}.{conn.pin}" if conn.pin else conn.refdes for conn in external[:30]
                    ],
                    "external_endpoint_count": len(external),
                    "external_modules": external_modules,
                }
            )
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "module_id": module.module_id,
            "module": module.name,
            "instance_count": len(module.instances),
            "interface_count": len(interfaces),
            "interfaces": interfaces,
        }

    def find_connected_modules(self, kb_name: str, design_id: str, module_id_or_name: str) -> dict | None:
        """Return modules that share nets with the given module (inter-module connections)."""
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        module = self._find_module_by_id_or_name(design, module_id_or_name)
        if module is None:
            return None
        module_refs = {self._normalize_refdes(ref) for ref in module.instances}
        net_by_name = {net.name: net for net in design.nets}
        membership = self._module_membership(design)
        connected: dict[str, dict] = {}
        for net_name in module.nets:
            net = net_by_name.get(net_name)
            if net is None:
                continue
            for conn in net.connections:
                if self._normalize_refdes(conn.refdes) in module_refs:
                    continue
                for m in membership.get(conn.refdes, []):
                    if m["module_id"] == module.module_id:
                        continue
                    entry = connected.setdefault(
                        m["module_id"], {"module_id": m["module_id"], "module_name": m["module_name"], "shared_nets": set()}
                    )
                    entry["shared_nets"].add(net.name)
        rows = [
            {"module_id": v["module_id"], "module_name": v["module_name"], "shared_nets": sorted(v["shared_nets"])}
            for v in connected.values()
        ]
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "module_id": module.module_id,
            "module": module.name,
            "connected_module_count": len(rows),
            "connected_modules": rows,
        }

    def get_power_distribution_tree(self, kb_name: str, design_id: str) -> dict | None:
        """Build a power/ground distribution view: each supply net → modules/instances it feeds."""
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        membership = self._module_membership(design)
        nets: list[dict] = []
        for net in design.nets:
            name_role = classify_net_name(net.name)
            if net.net_type not in {"power", "ground"} and name_role not in {"power", "ground"}:
                continue
            effective_role = net.net_type if net.net_type in {"power", "ground"} else name_role
            modules: dict[str, str] = {}
            instances: list[str] = []
            for conn in net.connections:
                instances.append(conn.refdes)
                for m in membership.get(conn.refdes, []):
                    modules[m["module_id"]] = m["module_name"]
            nets.append(
                {
                    "net_name": net.name,
                    "net_type": net.net_type,
                    "role": effective_role,
                    "connection_count": len(net.connections),
                    "modules": [{"module_id": mid, "module_name": name} for mid, name in sorted(modules.items())],
                    "instances": instances,
                }
            )
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "supply_net_count": len(nets),
            "power_nets": [n for n in nets if n["role"] == "power"],
            "ground_nets": [n for n in nets if n["role"] == "ground"],
        }

    def build_power_topology(self, kb_name: str, design_id: str) -> dict | None:
        """Infer a power-conversion topology from EDF pins and nets.

        This is intentionally conservative: it reports only relationships that
        can be tied to a concrete component and named pins/nets in the parsed
        EDF. It does not read datasheets, so ambiguous power ICs are surfaced as
        incomplete converter candidates instead of invented power paths.
        """
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None

        membership = self._module_membership(design)
        net_by_name = {net.name: net for net in design.nets}
        distribution = self.get_power_distribution_tree(kb_name, design_id) or {}
        modules_by_net = {
            net.get("net_name"): net.get("modules") or []
            for net in (distribution.get("power_nets") or []) + (distribution.get("ground_nets") or [])
        }

        converters: list[dict] = []
        edges: list[dict] = []
        output_nets: set[str] = set()
        input_nets: set[str] = set()
        rail_names: set[str] = {
            net.name
            for net in design.nets
            if net.net_type in {"power", "ground"} or classify_net_name(net.name) in {"power", "ground"}
        }
        relation_graph = RelationDeriver().derive(design, RelationExtractor().extract(design))
        power_view = build_power_tree_view(relation_graph)

        for inst in design.instances:
            candidate = self._power_converter_candidate(inst, membership, rail_names)
            if candidate is None:
                continue
            converters.append(candidate)
            for name in candidate["input_nets"]:
                input_nets.add(name)
            for name in candidate["output_nets"]:
                output_nets.add(name)
            for source in candidate["input_nets"] or [None]:
                for target in candidate["output_nets"]:
                    edges.append(
                        {
                            "from_net": source,
                            "to_net": target,
                            "via_refdes": candidate["refdes"],
                            "via_type": candidate["type"],
                            "via_label": candidate["label"],
                            "control_nets": candidate["enable_nets"] + candidate["power_good_nets"],
                            "confidence": candidate["confidence"],
                        }
                    )

        edge_keys = {(edge.get("from_net"), edge.get("to_net"), edge.get("via_refdes")) for edge in edges}
        for edge in power_view.get("direct_edges") or []:
            key = (edge.get("from_net"), edge.get("to_net"), edge.get("via_refdes"))
            if key in edge_keys:
                continue
            edges.append(edge)
            edge_keys.add(key)
            if edge.get("from_net"):
                input_nets.add(edge["from_net"])
            if edge.get("to_net"):
                output_nets.add(edge["to_net"])
        for edge in power_view.get("inferred_edges") or []:
            if edge.get("from_net"):
                input_nets.add(edge["from_net"])
            if edge.get("to_net"):
                output_nets.add(edge["to_net"])

        roots = sorted(name for name in input_nets if name not in output_nets)
        all_source_edges = edges + (power_view.get("inferred_edges") or [])
        rails = []
        for name in sorted(rail_names | input_nets | output_nets):
            net = net_by_name.get(name)
            rails.append(
                {
                    "net_name": name,
                    "role": "ground" if classify_net_name(name) == "ground" else "power",
                    "connection_count": len(net.connections) if net else 0,
                    "modules": modules_by_net.get(name, []),
                    "produced_by": [edge["via_refdes"] for edge in all_source_edges if edge.get("to_net") == name],
                    "consumed_by": [edge["via_refdes"] for edge in all_source_edges if edge.get("from_net") == name],
                }
            )

        missing_info: list[str] = []
        if not converters:
            missing_info.append("未从 EDF 中识别到带有明确 VIN/VOUT/EN 等引脚角色的电源转换器件。")
        if converters and not edges:
            missing_info.append("识别到疑似电源器件，但缺少可配对的输入/输出电源网络。")
        incomplete = [item["refdes"] for item in converters if item.get("is_incomplete")]
        if incomplete:
            missing_info.append("部分疑似电源器件缺少明确输入或输出网络：" + "、".join(incomplete[:12]))

        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "topology_type": "power_conversion_tree",
            "converter_count": len(converters),
            "edge_count": len(edges),
            "inferred_edge_count": len(power_view.get("inferred_edges") or []),
            "root_nets": roots,
            "input_nets": sorted(input_nets),
            "output_nets": sorted(output_nets),
            "rails": rails,
            "converters": converters,
            "conversion_edges": edges,
            "direct_edges": power_view.get("direct_edges") or [],
            "derived_edges": power_view.get("derived_edges") or [],
            "inferred_edges": power_view.get("inferred_edges") or [],
            "ambiguous_edges": power_view.get("ambiguous_edges") or [],
            "control_edges": power_view.get("control_edges") or [],
            "relation_summary": power_view.get("relation_summary") or {},
            "distribution_fallback": distribution,
            "missing_info": missing_info,
            "confidence": 0.86 if edges and power_view.get("inferred_edges") else (0.82 if edges else 0.45),
        }

    @classmethod
    def _power_converter_candidate(cls, inst, membership: dict[str, list[dict]], rail_names: set[str]) -> dict | None:
        if _looks_like_non_power_source(inst):
            return None
        device_type, type_confidence = _classify_power_device(inst)
        pin_rows: list[dict] = []
        for pin in inst.pins:
            net_name = pin.net
            if not net_name:
                continue
            role = _classify_power_topology_pin(pin.name, net_name, rail_names)
            if role == "other" and classify_net_name(net_name) not in {"power", "ground"} and net_name not in rail_names:
                continue
            pin_rows.append({"pin": pin.name, "net": net_name, "role": role})

        role_names = {row["role"] for row in pin_rows}
        has_conversion_pins = bool(role_names & {"input", "output", "switch_node"})
        if device_type is None and not ({"input", "output"} <= role_names):
            return None
        if not pin_rows or (device_type is None and not has_conversion_pins):
            return None

        inputs = _unique_pin_rows(row for row in pin_rows if row["role"] == "input")
        outputs = _unique_pin_rows(row for row in pin_rows if row["role"] == "output")
        switch_nodes = _unique_pin_rows(row for row in pin_rows if row["role"] == "switch_node")
        enables = _unique_pin_rows(row for row in pin_rows if row["role"] == "enable")
        power_good = _unique_pin_rows(row for row in pin_rows if row["role"] == "power_good")
        grounds = _unique_pin_rows(row for row in pin_rows if row["role"] == "ground")

        if device_type in {"fuse", "ferrite_bead", "series_passive"} and len(pin_rows) == 2:
            inputs = [pin_rows[0]]
            outputs = [pin_rows[1]]

        input_nets = sorted({row["net"] for row in inputs})
        output_nets = sorted({row["net"] for row in outputs})

        modules = membership.get(inst.refdes, [])
        is_incomplete = not input_nets or not output_nets
        confidence = min(0.98, type_confidence + (0.18 if input_nets and output_nets else 0.0))
        if is_incomplete:
            confidence = min(confidence, 0.62)

        return {
            "refdes": inst.refdes,
            "type": device_type or "power_ic_candidate",
            "label": inst.part_number or inst.library_cell or inst.value or inst.refdes,
            "library_cell": inst.library_cell,
            "part_number": inst.part_number,
            "value": inst.value,
            "modules": modules,
            "pins": {
                "inputs": inputs,
                "outputs": outputs,
                "switch_nodes": switch_nodes,
                "enables": enables,
                "power_good": power_good,
                "grounds": grounds,
                "power_related": _unique_pin_rows(pin_rows),
            },
            "input_nets": input_nets,
            "output_nets": output_nets,
            "enable_nets": sorted({row["net"] for row in enables}),
            "power_good_nets": sorted({row["net"] for row in power_good}),
            "is_incomplete": is_incomplete,
            "confidence": round(confidence, 2),
        }

    def trace_signal_path(
        self,
        kb_name: str,
        design_id: str,
        from_entity: str,
        to_entity: str,
        max_depth: int = 8,
    ) -> dict | None:
        """Trace a signal path between two entities via shared nets.

        ``from_entity``/``to_entity`` may be a refdes (``U3``) or a module
        name/id (expanded to the module's instances). Returns the shortest path
        as ordered refdes plus per-hop ``{from, to, net, net_type}``, or
        ``None`` when no path exists within ``max_depth`` hops.
        """
        design = self.store.load(kb_name, design_id)
        if design is None or not from_entity or not to_entity:
            return None
        sources = self._resolve_entity_to_refdes(design, from_entity)
        targets = self._resolve_entity_to_refdes(design, to_entity)
        if not sources or not targets:
            return None
        adjacency = self._instance_adjacency(design)
        membership = self._module_membership(design)
        prev: dict[str, tuple[str | None, str, str]] = {ref: (None, "", "") for ref in sources}
        queue: deque[str] = deque(sources)
        found = next((ref for ref in targets if ref in prev), None)
        while queue and found is None:
            current = queue.popleft()
            for peer, net_name, net_type in adjacency.get(current, []):
                if peer in prev:
                    continue
                prev[peer] = (current, net_name, net_type)
                if peer in targets:
                    found = peer
                    break
                queue.append(peer)
        if found is None:
            return None
        path_refdes: list[str] = []
        hops: list[dict] = []
        node: str | None = found
        while node is not None:
            path_refdes.append(node)
            parent, net_name, net_type = prev[node]
            if parent is not None:
                hops.append({
                    "from": parent,
                    "to": node,
                    "net": net_name,
                    "net_type": net_type,
                    "from_module": _first_module_name(parent, membership),
                    "to_module": _first_module_name(node, membership),
                })
            node = parent
        path_refdes.reverse()
        hops.reverse()
        module_path: list[str] = []
        seen_modules: set[str] = set()
        for ref in path_refdes:
            name = _first_module_name(ref, membership)
            if name and name not in seen_modules:
                seen_modules.add(name)
                module_path.append(name)
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "from_entity": from_entity,
            "to_entity": to_entity,
            "found": True,
            "hop_count": len(hops),
            "path": path_refdes,
            "hops": hops,
            "module_path": module_path,
        }

    def get_module_pdf_region(self, kb_name: str, design_id: str, module_id_or_name: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        module = self._find_module_by_id_or_name(design, module_id_or_name)
        if module is None:
            return None
        regions = [
            {
                "page_number": region.page_number,
                "bbox": region.bbox,
                "confidence": region.confidence,
                "strategy": region.strategy,
            }
            for region in design.module_regions
            if region.module_id == module.module_id
        ]
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "module_id": module.module_id,
            "module": module.name,
            "region_count": len(regions),
            "regions": regions,
        }

    def get_module_screenshot(self, kb_name: str, design_id: str, module_id_or_name: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        module = self._find_module_by_id_or_name(design, module_id_or_name)
        if module is None:
            return None
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", module.module_id)[:128] or "module"
        screenshots = self.store.list_module_screenshots(kb_name, design_id)
        matches = [path for path in screenshots if os.path.basename(path).startswith(safe_id)]
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "module_id": module.module_id,
            "module": module.name,
            "available": bool(matches),
            "screenshot_path": matches[0] if matches else None,
            "screenshot_count": len(matches),
        }

    def get_cross_reference_status(self, kb_name: str, design_id: str) -> dict | None:
        design = self.store.load(kb_name, design_id)
        if design is None:
            return None
        refs = design.cross_references
        refdes_set = {self._normalize_refdes(ref.edf_refdes) for ref in refs}
        instance_refdes = {self._normalize_refdes(inst.refdes) for inst in design.instances}
        mapped = refdes_set & instance_refdes
        confidences = [ref.confidence for ref in refs if ref.confidence is not None]
        return {
            "kb_name": kb_name,
            "design_id": design.design_id,
            "circuit_id": design.design_id,
            "source_files": [file.file_name for file in design.files],
            "has_cross_references": bool(refs),
            "cross_reference_count": len(refs),
            "mapped_instance_count": len(mapped),
            "unmapped_instance_count": len(instance_refdes - refdes_set),
            "coverage": round(len(mapped) / len(instance_refdes), 3) if instance_refdes else 0.0,
            "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
            "strategies": sorted({ref.strategy for ref in refs}),
        }

    def aggregate_circuit_results(
        self,
        kb_name: str,
        circuit_results: list[dict],
        reduce_strategy: str = "group_by_circuit",
    ) -> dict:
        """Reduce per-circuit query results into a grouped/merged structure.

        plan §4.7: the default view groups by circuit/source_file and never
        merges entities across circuits. The ``merge`` strategy additionally
        unions similar items while preserving the original circuit_id /
        source_file on every row.
        """
        grouped: list[dict] = []
        for result in circuit_results or []:
            if not isinstance(result, dict):
                continue
            circuit_id = result.get("circuit_id") or result.get("design_id")
            source_files = result.get("source_files") or []
            grouped.append(
                {
                    "kb_name": kb_name,
                    "circuit_id": circuit_id,
                    "source_file": source_files[0] if source_files else (circuit_id or ""),
                    "source_files": source_files,
                    "result": result,
                }
            )
        summary: dict = {
            "kb_name": kb_name,
            "reduce_strategy": reduce_strategy,
            "circuit_count": len(grouped),
            "grouped": grouped,
        }
        if reduce_strategy == "merge":
            bucket: dict[str, dict] = {}
            for group in grouped:
                result = group.get("result") or {}
                for module in result.get("modules") or []:
                    key = self._normalize_entity(module.get("name") or module.get("module_id"))
                    if not key:
                        continue
                    entry = bucket.setdefault(
                        key, {"name": module.get("name") or module.get("module_id"), "sources": []}
                    )
                    entry["sources"].append(
                        {
                            "circuit_id": group["circuit_id"],
                            "source_file": group["source_file"],
                            "module_id": module.get("module_id"),
                        }
                    )
            summary["merged_modules"] = list(bucket.values())
        return summary

    def search_module_descriptions(
        self,
        kb_name: str,
        query: str,
        circuit_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Vector semantic search over module description docs.

        Powers plan §3.7 recovery ("降压电路" → power_stage) and cross-circuit
        module discovery. Restricted to ``circuit_ids`` when given so a
        single-circuit scope never leaks neighbours from other circuits.
        """
        if not query or not query.strip() or not self.vector_index.is_available():
            return []
        allowed = {cid for cid in (circuit_ids or [])} or None
        try:
            hits = self.vector_index.semantic_search(kb_name, query, top_k=max(limit, 12), kinds=[KIND_MODULE])
        except Exception:
            return []
        designs = self.store.list_designs(kb_name)
        rows: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            if allowed and hit.design_id not in allowed:
                continue
            key = (hit.design_id, hit.natural_id)
            if key in seen:
                continue
            seen.add(key)
            module = self._find_module(designs, hit.design_id, hit.natural_id)
            design = self._find_design(designs, hit.design_id)
            rows.append(
                {
                    "kb_name": kb_name,
                    "design_id": hit.design_id,
                    "circuit_id": hit.design_id,
                    "source_files": [f.file_name for f in design.files] if design else [],
                    "module_id": hit.natural_id,
                    "module_name": module.name if module else (hit.metadata.get("module_name") or hit.natural_id),
                    "score": round(hit.score, 4),
                    "instance_count": len(module.instances) if module else hit.metadata.get("instance_count", 0),
                    "description": (module.merged_description or module.connectivity_description or module.visual_description)
                    if module
                    else hit.document,
                }
            )
            if len(rows) >= limit:
                break
        return rows

    # ── signal-path helpers ───────────────────────────────────────────────

    def _resolve_entity_to_refdes(self, design, entity: str) -> set[str]:
        """Resolve a user-facing entity (refdes or module name/id) to refdes set."""
        normalized = self._normalize_refdes(entity)
        refs = {inst.refdes for inst in design.instances if self._normalize_refdes(inst.refdes) == normalized}
        if refs:
            return refs
        module = self._find_module_by_id_or_name(design, entity)
        if module:
            return {ref for ref in module.instances}
        return set()

    @staticmethod
    def _instance_adjacency(design) -> dict[str, list[tuple[str, str, str]]]:
        """Build refdes → [(peer_refdes, net_name, net_type)] from the netlist."""
        adjacency: dict[str, list[tuple[str, str, str]]] = {}
        for net in design.nets:
            refs = [conn.refdes for conn in net.connections if conn.refdes]
            for i, src in enumerate(refs):
                for dst in refs[i + 1:]:
                    if src == dst:
                        continue
                    adjacency.setdefault(src, []).append((dst, net.name, net.net_type))
                    adjacency.setdefault(dst, []).append((src, net.name, net.net_type))
        return adjacency

    def _semantic_supplement(
        self,
        kb_name: str,
        query: str,
        kinds: Sequence[str],
        limit: int,
    ):
        """Run the vector index for additional candidates. Empty on failure
        or when no embed model is bound — keyword path is unaffected."""
        try:
            if not self.vector_index.is_available():
                return []
            # Slightly over-fetch so that after de-dup against keyword hits
            # we still have something useful to add.
            return self.vector_index.semantic_search(
                kb_name,
                query,
                top_k=max(limit, 8),
                kinds=kinds,
            )
        except Exception:
            return []
