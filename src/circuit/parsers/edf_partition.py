"""Group EDIF instances into "modules" for the circuit browser.

OrCAD/Capture exports carry a per-instance ``Page Name`` property — the
schematic page each component lives on. That's what an engineer thinks of as
a "module" (the power tree page, the MCU page, the DDR page, …), so we use
it as the primary grouping signal. When ``Page Name`` is missing we fall
back to the legacy refdes-prefix heuristic so SpyDrNet-parsed netlists keep
working.

The module name is built from:
1. the (cleaned-up) page name, e.g. ``22_REVISION2`` → ``Page 22 — REVISION2``;
2. an optional descriptor derived from the dominant cell category on that
   page (MCU / DDR / PMIC / ETH / PASSIVES …) so the browser shows
   functional context rather than just a page number.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from src.circuit.models import CircuitModule, ComponentInstance, Net


_REF_PREFIX_RE = re.compile(r"^[A-Za-z]+")
_PAGE_NAME_PROPS = ("Page Name", "PAGE_NAME", "Page", "PAGE")


def _instance_page_key(refdes: str) -> str:
    digits = "".join(ch for ch in refdes if ch.isdigit())
    if len(digits) >= 3:
        return digits[:-2] or "0"
    return "misc"


def _instance_page_name(inst: ComponentInstance) -> str | None:
    properties = inst.properties or {}
    for key in _PAGE_NAME_PROPS:
        value = properties.get(key)
        if value:
            return str(value).strip()
    return None


_CATEGORY_RULES = [
    # (label, regex matching cell name / part description)
    ("MCU/SoC", re.compile(r"(MCU|EYEQ|EQ6|SOC|CORTEX|PROCESSOR|SEQ6L)", re.I)),
    ("DDR/Memory", re.compile(r"(DDR|LPDDR|FLASH|EEPROM|NOR|NAND|MEMORY)", re.I)),
    ("Power/PMIC", re.compile(r"(PMIC|REGULATOR|LDO|DCDC|BUCK|TPS|LM|MP\d|POWER)", re.I)),
    ("Ethernet/PHY", re.compile(r"(ETH|PHY|RGMII|MAC|MARVELL|REALTEK|88E)", re.I)),
    ("CAN/LIN", re.compile(r"(CAN|LIN|TJA|TLE)", re.I)),
    ("Clock/Oscillator", re.compile(r"(CLK|CRYSTAL|XTAL|OSC|PLL|SI5\d)", re.I)),
    ("USB", re.compile(r"USB", re.I)),
    ("MIPI/Camera", re.compile(r"(MIPI|CSI|CAMERA|SERDES|MAX9|FPDLINK|GMSL)", re.I)),
    ("Connector/Header", re.compile(r"(CONN|HEADER|JACK|RJ45|FFC)", re.I)),
    ("LED/Indicator", re.compile(r"^LED", re.I)),
    ("Diode/TVS", re.compile(r"(DIODE|TVS|SCHOTTKY|ZENER)", re.I)),
    ("Transistor/MOSFET", re.compile(r"(MOSFET|NFET|PFET|BJT|TRANSISTOR)", re.I)),
]


def _categorise(instances: list[ComponentInstance]) -> str | None:
    """Pick the dominant functional label for a group of instances.

    Looks at the cell name first (e.g. ``SEQ6L_02588_HI_HI_1_1`` → MCU/SoC)
    and falls back to the ``Description`` / ``Part Type`` property. Returns
    ``None`` when nothing matches confidently — better to omit the descriptor
    than to slap on a wrong label.
    """
    label_counts: Counter[str] = Counter()
    for inst in instances:
        haystacks = [
            inst.library_cell or "",
            (inst.properties or {}).get("Description", ""),
            (inst.properties or {}).get("DESCRIPTION", ""),
            (inst.properties or {}).get("Part Type", ""),
            (inst.properties or {}).get("PART_TYPE", ""),
        ]
        text = " ".join(haystacks)
        for label, pattern in _CATEGORY_RULES:
            if pattern.search(text):
                label_counts[label] += 1
                break
    if not label_counts:
        return None
    label, _ = label_counts.most_common(1)[0]
    return label


def _pretty_page_name(raw: str) -> str:
    """Normalise OrCAD's page names — ``22_REVISION2`` → ``Page 22 (REVISION2)``."""
    text = raw.strip()
    match = re.match(r"^(\d+)[_\- ]+(.+)$", text)
    if match:
        return f"Page {match.group(1)} ({match.group(2)})"
    if text.isdigit():
        return f"Page {text}"
    return text


def _module_name(page_name: str, instances: list[ComponentInstance], dominant_prefix: str) -> str:
    """Compose a human-friendly module name."""
    base = _pretty_page_name(page_name)
    category = _categorise(instances)
    if category:
        return f"{base} · {category}"
    # Last resort: surface the dominant refdes prefix so two pages full of
    # discretes can still be told apart.
    return f"{base} · {dominant_prefix}*"


def _partition_by_page(instances: list[ComponentInstance], nets: list[Net]) -> list[CircuitModule]:
    groups: dict[str, list[ComponentInstance]] = defaultdict(list)
    instance_to_group: dict[str, str] = {}
    for inst in instances:
        page = _instance_page_name(inst)
        if not page:
            return []  # signal "fall through to refdes grouping"
        groups[page].append(inst)
        instance_to_group[inst.refdes] = page

    group_nets: dict[str, set[str]] = defaultdict(set)
    for net in nets:
        touched = {instance_to_group.get(conn.refdes) for conn in net.connections}
        for page in touched:
            if page:
                group_nets[page].add(net.name)

    modules: list[CircuitModule] = []
    for index, page in enumerate(sorted(groups), start=1):
        members = groups[page]
        prefix_counts: Counter[str] = Counter()
        for inst in members:
            match = _REF_PREFIX_RE.match(inst.refdes)
            prefix_counts[match.group(0).upper() if match else "X"] += 1
        dominant = prefix_counts.most_common(1)[0][0] if prefix_counts else "X"
        name = _module_name(page, members, dominant)
        modules.append(
            CircuitModule(
                module_id=f"edf_page_{index:02d}",
                name=name,
                strategy="orcad_page_name",
                instances=sorted(inst.refdes for inst in members),
                nets=sorted(group_nets.get(page, set())),
                connectivity_description=(
                    f"{name}: {len(members)} instances, "
                    f"{len(group_nets.get(page, set()))} related nets."
                ),
            )
        )
    return modules


def _partition_by_refdes_page(instances: list[ComponentInstance], nets: list[Net]) -> list[CircuitModule]:
    """Legacy fallback when no ``Page Name`` is available. Same logic as the
    original implementation, kept verbatim so SpyDrNet-only flows behave
    exactly as before."""
    groups: dict[str, list[str]] = defaultdict(list)
    instance_to_group: dict[str, str] = {}
    for inst in instances:
        group_id = _instance_page_key(inst.refdes)
        groups[group_id].append(inst.refdes)
        instance_to_group[inst.refdes] = group_id

    group_nets: dict[str, set[str]] = defaultdict(set)
    for net in nets:
        touched_groups = {instance_to_group.get(conn.refdes) for conn in net.connections}
        for group_id in touched_groups:
            if group_id:
                group_nets[group_id].add(net.name)

    by_refdes = {inst.refdes: inst for inst in instances}
    modules: list[CircuitModule] = []
    for index, group_id in enumerate(sorted(groups), start=1):
        members = [by_refdes[r] for r in groups[group_id] if r in by_refdes]
        prefix_counts: Counter[str] = Counter()
        for refdes in groups[group_id]:
            match = _REF_PREFIX_RE.match(refdes)
            prefix_counts[match.group(0).upper() if match else "X"] += 1
        dominant = prefix_counts.most_common(1)[0][0] if prefix_counts else "X"
        category = _categorise(members)
        base = f"Group {group_id}"
        name = f"{base} · {category}" if category else f"{base} · {dominant}*"
        modules.append(
            CircuitModule(
                module_id=f"edf_ref_page_{index}",
                name=name,
                strategy="refdes_page",
                instances=sorted(groups[group_id]),
                nets=sorted(group_nets.get(group_id, set())),
                connectivity_description=(
                    f"{name}: {len(groups[group_id])} instances, "
                    f"{len(group_nets.get(group_id, set()))} related nets."
                ),
            )
        )
    return modules


def partition_by_refdes_page(instances: list[ComponentInstance], nets: list[Net]) -> list[CircuitModule]:
    """Public entry point.

    Tries the OrCAD ``Page Name`` strategy first (modules → schematic
    pages). If no instance carries a page name we fall back to the legacy
    refdes-prefix bucketing so SpyDrNet-only outputs keep their old shape.
    The public function name is preserved for backwards compatibility with
    the rest of the codebase.
    """
    by_page = _partition_by_page(instances, nets)
    if by_page:
        return by_page
    return _partition_by_refdes_page(instances, nets)
