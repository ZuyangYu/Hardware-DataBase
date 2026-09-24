"""Lightweight EDIF 2.0.0 parser for OrCAD Capture exports.

SpyDrNet's EDIF parser raises ``NotImplementedError`` on many constructs that
OrCAD Capture's ``cap2edif`` writes (``dataOrigin (version ...)`` children,
``technology`` blocks padded with ``figureGroup``, ``interface`` blocks with
``symbol``/``joined``/etc.). We can't realistically patch every site upstream
ignores, so this module ships a focused, dependency-free scanner that pulls
exactly what the downstream pipeline consumes:

* component instances (``refdes``, ``library_cell``, part number,
  footprint, value, ERP number, properties)
* nets (``name`` + ``portRef (instanceRef X)`` connections)
* cell→port name maps so net joins can resolve into ``Pin(name=...)`` entries

The parser tokenises the s-expression once into a tree and then walks it
twice — once to harvest cell port lists, once to harvest instances and nets.
At ~10 MB the OrCAD file parses in seconds and uses linear memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from src.circuit.models import ComponentInstance, FieldProvenance, Net, Pin, PinRef


# ── tokeniser ───────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r"""
    \s* (?:                              # leading whitespace eaten between tokens
        (?P<lparen> \( )
      | (?P<rparen> \) )
      | "(?P<string> (?:\\.|[^"\\])* )"  # quoted string with escape support
      | (?P<symbol> [^\s()"]+ )          # bare symbol / number
    )
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> Iterable[tuple[str, str]]:
    """Yield ``(kind, value)`` token pairs.

    Kinds: ``"("`` / ``")"`` / ``"str"`` / ``"sym"``. Comments (``//`` line
    or ``/* … */``) and EDIF's rarely-used semicolon comments are not
    expected in OrCAD output; we keep the scanner deliberately strict.
    """
    pos = 0
    end = len(text)
    while pos < end:
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            # whitespace-only tail
            if not text[pos:].strip():
                return
            raise ValueError(f"EDIF tokeniser stuck at byte {pos}: {text[pos:pos+40]!r}")
        pos = match.end()
        if match.group("lparen"):
            yield ("(", "(")
        elif match.group("rparen"):
            yield (")", ")")
        elif match.group("string") is not None:
            yield ("str", match.group("string"))
        else:
            yield ("sym", match.group("symbol"))


# ── s-expression tree ──────────────────────────────────────────────────


@dataclass
class SExpr:
    """A node in the parsed s-expression tree.

    ``head`` is the leading symbol — e.g. ``"instance"``, ``"cell"``,
    ``"property"``. ``children`` is a mix of ``SExpr`` (nested forms),
    plain ``str`` (bare symbols or numbers) and ``("str", value)`` tuples
    (quoted strings, kept distinct so we don't confuse e.g. the symbol
    ``OrCAD`` with the string ``"OrCAD"``).
    """

    head: str
    children: list[Any]

    def find(self, name: str) -> "SExpr | None":
        for child in self.children:
            if isinstance(child, SExpr) and child.head == name:
                return child
        return None

    def find_all(self, name: str) -> list["SExpr"]:
        return [c for c in self.children if isinstance(c, SExpr) and c.head == name]

    def find_deep(self, name: str, limit: int = 1) -> list["SExpr"]:
        """Breadth-first search collecting up to ``limit`` descendants."""
        results: list[SExpr] = []
        stack: list[SExpr] = [self]
        while stack and len(results) < limit:
            node = stack.pop(0)
            for child in node.children:
                if isinstance(child, SExpr):
                    if child.head == name:
                        results.append(child)
                        if len(results) >= limit:
                            break
                    stack.append(child)
        return results


class _TokenStream:
    """Single-shared iterator with one-token pushback.

    Plain ``yield from`` adapters don't work for nested ``_parse_sexpr``
    calls because each call would advance a private generator, leaving the
    outer loop's state stale. A small wrapper with explicit ``next``/``push``
    keeps a single underlying iterator so nested parses cooperate.
    """

    def __init__(self, source):
        self._iter = iter(source)
        self._buffer: list[tuple[str, str]] = []

    def next(self) -> tuple[str, str]:
        if self._buffer:
            return self._buffer.pop()
        return next(self._iter)

    def push(self, token: tuple[str, str]) -> None:
        self._buffer.append(token)


def _parse_sexpr(tokens: "_TokenStream") -> SExpr:
    """Consume a token stream starting at ``(`` and return the SExpr node."""
    kind, value = tokens.next()
    if kind != "(":
        raise ValueError(f"expected '(' but got {kind} {value!r}")
    kind, value = tokens.next()
    if kind != "sym":
        raise ValueError(f"expected head symbol but got {kind} {value!r}")
    node = SExpr(head=value, children=[])
    while True:
        kind, value = tokens.next()
        if kind == ")":
            return node
        if kind == "(":
            tokens.push((kind, value))
            node.children.append(_parse_sexpr(tokens))
        elif kind == "str":
            node.children.append(("str", value))
        else:  # symbol or number
            node.children.append(value)


def parse_edif_file(path: str) -> SExpr:
    """Tokenise + s-expression parse an EDIF file. Returns the root node."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    tokens = _TokenStream(_tokenize(text))
    root = _parse_sexpr(tokens)
    if root.head.lower() != "edif":
        raise ValueError(f"not an EDIF file: root node head is {root.head!r}")
    return root


# ── value helpers ──────────────────────────────────────────────────────


def _as_text(child: Any) -> str | None:
    """Return ``child`` as a plain string for either a quoted token or a
    bare symbol. Use ``_as_quoted`` when you specifically need the quoted
    (display) form."""
    if isinstance(child, tuple) and len(child) == 2 and child[0] == "str":
        return str(child[1])
    if isinstance(child, str):
        return child
    return None


def _as_quoted(child: Any) -> str | None:
    """Return ``child`` only when it's a quoted string token; otherwise None."""
    if isinstance(child, tuple) and len(child) == 2 and child[0] == "str":
        return str(child[1])
    return None


def _as_symbol(child: Any) -> str | None:
    """Return ``child`` only when it's a bare symbol; otherwise None."""
    if isinstance(child, str):
        return child
    return None


def _stringDisplay_value(node: SExpr) -> str | None:
    """A ``(stringDisplay "value" ...)`` node — return ``"value"``."""
    for child in node.children:
        value = _as_text(child)
        if value is not None:
            return value
    return None


def _designator_value(node: SExpr) -> str | None:
    """Extract the designator's underlying string. OrCAD writes either
    ``(designator "U1")`` or ``(designator (stringDisplay "U1" ...))``.
    """
    for child in node.children:
        text = _as_text(child)
        if text is not None:
            return text
        if isinstance(child, SExpr) and child.head == "stringDisplay":
            return _stringDisplay_value(child)
    return None


def _string_node_value(node: SExpr) -> str | None:
    """``(string "VALUE")`` → ``"VALUE"`` (also handles bare-symbol form)."""
    for child in node.children:
        text = _as_text(child)
        if text is not None:
            return text
    return None


# ── property extraction (handles OrCAD's GBK-encoded ``%nn%%mm%`` runs) ─


_ENCODED_GBK_RE = re.compile(r"%((?:\d+%%)+\d+)%")


def _decode_orcad_text(value: str | None) -> str | None:
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        try:
            data = bytes(int(code) for code in match.group(1).split("%%"))
            return data.decode("gbk", errors="replace")
        except Exception:  # pragma: no cover - defensive
            return match.group(0)

    return _ENCODED_GBK_RE.sub(replace, value).replace("37%", "%")


def _collect_property_map(parent: SExpr) -> dict[str, str]:
    """Extract OrCAD ``(property ...)`` entries into ``{display_name: value}``.

    OrCAD writes properties as ``(property (rename SYMBOL "Display Name") (string "VALUE"))``.
    Prefer the quoted display name (``"Part Number"``) over the bare symbol
    (``PART_NUMBER``) so downstream lookups can use the human-readable form
    consistently. Falls back to the symbol when no display name is present
    (some OrCAD properties have only the ``rename SYMBOL`` form), and finally
    to the first child symbol when ``rename`` is absent altogether.
    """
    props: dict[str, str] = {}
    for prop in parent.find_all("property"):
        name: str | None = None
        rename = prop.find("rename")
        if rename is not None and rename.children:
            # Look for a quoted display name first.
            for child in rename.children:
                quoted = _as_quoted(child)
                if quoted:
                    name = quoted
                    break
            # No quoted form — accept the leading symbol identifier.
            if name is None:
                name = _as_symbol(rename.children[0])
        else:
            head_child = prop.children[0] if prop.children else None
            name = _as_quoted(head_child) or _as_symbol(head_child)
        value_node = prop.find("string")
        value = _string_node_value(value_node) if value_node is not None else None
        if value is None:
            # Some OrCAD properties carry their value via stringDisplay.
            sd = prop.find("stringDisplay")
            if sd is not None:
                value = _stringDisplay_value(sd)
        if name and value is not None:
            props[name] = _decode_orcad_text(value) or value
    return props


# ── EDIF → instances / nets ────────────────────────────────────────────


def _collect_cell_pins(root: SExpr) -> dict[tuple[str, str], list[str]]:
    """Map ``(library, cell)`` → ordered list of port names.

    We index by ``library`` because OrCAD allows duplicate cell names across
    libraries (e.g. ``PIN_SHORT``); ``viewRef → cellRef → libraryRef`` carry
    the qualifier on the instance side.
    """
    cell_pins: dict[tuple[str, str], list[str]] = {}
    for library in root.find_all("library") + root.find_all("external"):
        library_name = _node_name(library)
        if not library_name:
            continue
        for cell in library.find_all("cell"):
            cell_name = _node_name(cell)
            if not cell_name:
                continue
            pin_names: list[str] = []
            # Walk view → interface → port.
            for view in cell.find_all("view"):
                interface = view.find("interface")
                if interface is None:
                    continue
                for port in interface.find_all("port"):
                    pin_name = _port_name(port)
                    if pin_name:
                        pin_names.append(pin_name)
            if pin_names:
                cell_pins[(library_name, cell_name)] = pin_names
    return cell_pins


def _strip_edif_amp(name: Any) -> Any:
    """Strip the leading ``&`` EDIF uses to escape identifiers that begin with
    a digit. Applied at every header-name extraction site so downstream code
    (net classification, module naming, cross-reference) never sees the raw
    escape. Non-string values pass through unchanged."""
    if isinstance(name, str) and name.startswith("&"):
        return name[1:]
    return name


def _node_name(node: SExpr) -> str | None:
    """A library/cell/view header is ``(library NAME ...)`` or
    ``(library (rename NAME "Display") ...)``.

    Strips OrCAD's leading ``&`` escape so e.g. ``(net &0V75_ACQ ...)`` and
    ``(page &04_EYEQ6_DDR ...)`` are surfaced as ``0V75_ACQ`` / ``04_EYEQ6_DDR``.
    """
    if not node.children:
        return None
    first = node.children[0]
    if isinstance(first, str):
        return _strip_edif_amp(first)
    if isinstance(first, tuple) and first[0] == "str":
        return _strip_edif_amp(first[1])
    if isinstance(first, SExpr) and first.head == "rename" and first.children:
        head = first.children[0]
        text = _as_text(head) or (head if isinstance(head, str) else None)
        return _strip_edif_amp(text)
    return None


def _port_name(port: SExpr) -> str | None:
    """OrCAD ports come as ``(port NAME ...)``, ``(port (rename …))``, or
    occasionally ``(port (array (rename …) N))`` for bundles. We collapse
    arrays into a single base name to match the instance-side portRef."""
    if not port.children:
        return None
    first = port.children[0]
    if isinstance(first, SExpr):
        if first.head == "rename":
            return _rename_value(first)
        if first.head == "array":
            return _node_name(first)
    return _strip_edif_amp(_as_text(first))


def _rename_value(rename: SExpr) -> str | None:
    if not rename.children:
        return None
    first = rename.children[0]
    text = _as_text(first) or (first if isinstance(first, str) else None)
    return _strip_edif_amp(text)


@dataclass
class _RawInstance:
    instance_id: str
    refdes: str | None
    library: str | None
    cell: str | None
    page_name: str | None
    properties: dict[str, str]


def _walk_instances(root: SExpr) -> list[_RawInstance]:
    """Recursively collect ``(instance ...)`` blocks, threading the enclosing
    ``(page <NAME> ...)`` context through so each instance knows which
    schematic page it belongs to.

    OrCAD writes page names with a leading ``&`` when the name starts with a
    digit (EDIF escape rule), e.g. ``(page &02_DEBUG ...)``. ``_node_name``
    already strips that escape, so we just propagate whatever it returns.
    """
    results: list[_RawInstance] = []

    def _visit(node: SExpr, current_page: str | None):
        if node.head == "page":
            current_page = _node_name(node) or current_page
        if node.head == "instance":
            instance_id = _node_name(node) or ""
            designator = node.find("designator")
            refdes_raw = _designator_value(designator) if designator else None
            refdes = _decode_orcad_text(refdes_raw) if refdes_raw else None
            view_ref = node.find("viewRef")
            cell_ref = view_ref.find("cellRef") if view_ref else None
            library_ref = cell_ref.find("libraryRef") if cell_ref else None
            cell_name = _node_name(cell_ref) if cell_ref else None
            library_name = _node_name(library_ref) if library_ref else None
            if refdes:
                instance_properties = _collect_property_map(node)
                # Propagate the enclosing page so partitioning can group by
                # schematic page even though OrCAD only writes the page name
                # on the page-border instance / on the (page) wrapper.
                if current_page and "Page Name" not in instance_properties:
                    instance_properties["Page Name"] = current_page
                results.append(
                    _RawInstance(
                        instance_id=instance_id,
                        refdes=refdes,
                        library=library_name,
                        cell=cell_name,
                        page_name=current_page,
                        properties=instance_properties,
                    )
                )
        for child in node.children:
            if isinstance(child, SExpr):
                _visit(child, current_page)

    _visit(root, current_page=None)
    return results


@dataclass
class _RawConnection:
    instance_id: str | None  # None for top-level/border ports
    pin_name: str | None


def _walk_nets(root: SExpr) -> list[tuple[str, list[_RawConnection]]]:
    """Collect ``(net NAME (joined (portRef PIN (instanceRef INSnn)) ...))``."""
    nets: list[tuple[str, list[_RawConnection]]] = []

    def _visit(node: SExpr):
        if node.head == "net":
            name = _node_name(node)
            if name is not None:
                joined = node.find("joined")
                connections: list[_RawConnection] = []
                if joined is not None:
                    for ref in joined.find_all("portRef"):
                        pin_name = None
                        if ref.children:
                            first = ref.children[0]
                            pin_name = _as_text(first) or (
                                first if isinstance(first, str) else None
                            )
                            if pin_name is None and isinstance(first, SExpr):
                                pin_name = _node_name(first)
                            pin_name = _strip_edif_amp(pin_name)
                        instance_id = None
                        inst_ref = ref.find("instanceRef")
                        if inst_ref and inst_ref.children:
                            first = inst_ref.children[0]
                            instance_id = _as_text(first) or (
                                first if isinstance(first, str) else None
                            )
                            instance_id = _strip_edif_amp(instance_id)
                        connections.append(_RawConnection(instance_id, pin_name))
                nets.append((name, connections))
        for child in node.children:
            if isinstance(child, SExpr):
                _visit(child)

    _visit(root)
    return nets


# ── public API ─────────────────────────────────────────────────────────


def parse_orcad_edif(
    file_path: str,
    source_label: str | None = None,
) -> tuple[list[ComponentInstance], list[Net]]:
    """Parse an OrCAD-flavoured EDIF and return ``(instances, nets)``.

    ``source_label`` is recorded in ``ComponentInstance.provenance`` and
    defaults to the file's basename when omitted.
    """
    root = parse_edif_file(file_path)
    cell_pins = _collect_cell_pins(root)
    raw_instances = _walk_instances(root)
    raw_nets = _walk_nets(root)

    source = source_label or file_path.rsplit("/", 1)[-1]

    # Build instance lookup by ``instance_id`` so net joins can resolve their
    # ``instanceRef`` back to a ``refdes``.
    instance_by_id: dict[str, _RawInstance] = {}
    for raw in raw_instances:
        if raw.instance_id and raw.instance_id not in instance_by_id:
            instance_by_id[raw.instance_id] = raw

    # Materialise ComponentInstance objects (one per unique refdes; OrCAD
    # splits multi-part packages into multiple instances sharing the same
    # base refdes, e.g. ``U400-1`` ``U400-2`` — keep them distinct).
    def _prop(props: dict[str, str], *keys: str) -> str | None:
        for key in keys:
            value = props.get(key)
            if value not in (None, ""):
                return value
        return None

    instances_by_refdes: dict[str, ComponentInstance] = {}
    for raw in raw_instances:
        if not raw.refdes:
            continue
        pin_names = cell_pins.get((raw.library or "", raw.cell or ""), [])
        pins = [Pin(name=pn, net=None) for pn in pin_names] if pin_names else []
        existing = instances_by_refdes.get(raw.refdes)
        # OrCAD writes most labels as both a display name and a SYMBOL alias.
        # Probe display name first (matches the rest of the codebase's
        # ``Part Number`` style), then fall back to the symbol.
        part_number = _prop(
            raw.properties,
            "Part Number",
            "PART_NUMBER",
            "Manufacturer Part Number",
            "MANUFACTURER_PART_NUMBER",
        )
        footprint = _prop(raw.properties, "PCB Footprint", "PCB_FOOTPRINT")
        value = _prop(raw.properties, "Value", "VALUE")
        erp = _prop(raw.properties, "ERP NUM", "ERP_NUM")
        if existing is None:
            instances_by_refdes[raw.refdes] = ComponentInstance(
                refdes=raw.refdes,
                library_cell=raw.cell,
                part_number=part_number,
                footprint=footprint,
                value=value,
                erp_number=erp,
                pins=pins,
                properties=dict(raw.properties),
                provenance={
                    "refdes": FieldProvenance(source, "edif_lite"),
                    "pins": FieldProvenance(source, "edif_lite"),
                },
            )
        else:
            # Multi-part: merge pin lists and shallow-merge properties.
            if pins and not existing.pins:
                existing.pins = pins
            elif pins:
                existing.pins.extend(pins)
            for key, prop_value in raw.properties.items():
                existing.properties.setdefault(key, prop_value)
            # Fill in fields that were missing on the first part record.
            if existing.part_number is None and part_number is not None:
                existing.part_number = part_number
            if existing.footprint is None and footprint is not None:
                existing.footprint = footprint
            if existing.value is None and value is not None:
                existing.value = value
            if existing.erp_number is None and erp is not None:
                existing.erp_number = erp

    # Now wire nets and back-fill ``Pin.net`` so downstream consumers keep
    # working without a second lookup table. Connections are deduplicated by
    # ``(refdes, pin)`` — OrCAD writes the same join on every page that
    # touches a net, so without this we'd double-count fan-out on global nets
    # like GND.
    net_connections: dict[str, list[PinRef]] = {}
    net_seen: dict[str, set[tuple[str, str]]] = {}
    for name, connections in raw_nets:
        bucket_refs = net_connections.setdefault(name, [])
        seen = net_seen.setdefault(name, set())
        for conn in connections:
            instance = instance_by_id.get(conn.instance_id) if conn.instance_id else None
            if instance is None or not instance.refdes:
                continue
            pin = conn.pin_name or "?"
            key = (instance.refdes, pin)
            if key in seen:
                continue
            seen.add(key)
            bucket_refs.append(PinRef(refdes=instance.refdes, pin=pin))
            comp = instances_by_refdes.get(instance.refdes)
            if comp is not None:
                # Update first matching pin name; if absent, append.
                for pin_model in comp.pins:
                    if pin_model.name == pin and pin_model.net is None:
                        pin_model.net = name
                        break
                else:
                    comp.pins.append(Pin(name=pin, net=name))

    instances = sorted(instances_by_refdes.values(), key=lambda inst: inst.refdes)
    nets = [Net(name=name, connections=refs) for name, refs in sorted(net_connections.items())]
    return instances, nets
