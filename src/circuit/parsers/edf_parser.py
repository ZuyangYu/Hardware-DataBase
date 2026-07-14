from __future__ import annotations

import importlib
import logging
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from src.circuit.models import ComponentInstance, FieldProvenance, Net, Pin, PinRef
from src.circuit.parsers.edf_partition import partition_by_refdes_page
from src.circuit.parsers.edf_power import classify_net_name
from src.core.logger import log as _info, warn as _warn, error as _error


_ENCODED_GBK_RE = re.compile(r"%((?:\d+%%)+\d+)%")


def decode_edf_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)

    def decode_match(match: re.Match[str]) -> str:
        try:
            data = bytes(int(code) for code in match.group(1).split("%%"))
            return data.decode("gbk", errors="replace")
        except Exception:
            return match.group(0)

    return _ENCODED_GBK_RE.sub(decode_match, text).replace("37%", "%")


_SPYDRNET_PATCHED = False


def _patch_spydrnet_edif_parser() -> None:
    """Make SpyDrNet's EDIF parser tolerant of OrCAD Capture extensions.

    Upstream's ``parse_dataOrigin`` / ``parse_userData`` raise
    ``NotImplementedError`` the moment they see nested children, and
    ``parse_technology`` only accepts a single ``numberDefinition`` child —
    OrCAD's ``cap2edif`` writes ``(dataOrigin "OrCAD Capture" (version ...))``
    and a ``technology`` block packed with ``figureGroup`` / ``property``
    siblings, both of which trip the path immediately.

    Strategy: keep the construct keyword consumed (so the outer parser stays
    in sync), then skip every remaining child construct until the closing
    paren. The resulting netlist is identical for our purposes — we only
    consume libraries/cells/instances/nets downstream, never the cosmetic
    technology metadata.
    """
    global _SPYDRNET_PATCHED
    if _SPYDRNET_PATCHED:
        return
    try:
        from spydrnet.parsers.edif.parser import (  # type: ignore
            EdifParser as _Up,
            DATA_ORIGIN,
            USER_DATA,
            TECHNOLOGY,
        )
    except Exception:
        return

    def _skip_construct(method_name: str, keyword: str):
        original = getattr(_Up, method_name, None)

        def _skip(self):
            self.expect(keyword)
            # ``skip_until_next_construct`` walks tokens balancing parens and
            # stops one token before the matching close ``)`` — exactly what
            # we want here. Handles both leaf tokens (e.g. the bare
            # ``"OrCAD Capture"`` string after ``dataOrigin``) and nested
            # s-expressions (``(figureGroup ...)``, ``(version ...)`` etc.).
            self.skip_until_next_construct()

        if original is not None:
            setattr(_Up, f"_orig_{method_name}", original)
        setattr(_Up, method_name, _skip)

    _skip_construct("parse_dataOrigin", DATA_ORIGIN)
    _skip_construct("parse_userData", USER_DATA)
    _skip_construct("parse_technology", TECHNOLOGY)
    _SPYDRNET_PATCHED = True


def _repo_spydrnet_path() -> Path:
    return Path(__file__).resolve().parents[4] / "spydrnet"


def _load_spydrnet():
    """Load the workspace SpyDrNet checkout before any installed package."""
    repo_spydrnet = _repo_spydrnet_path()
    repo_package = repo_spydrnet / "spydrnet" / "__init__.py"
    if repo_package.is_file():
        repo_path = str(repo_spydrnet)
        sys.path[:] = [entry for entry in sys.path if entry != repo_path]
        sys.path.insert(0, repo_path)
        importlib.invalidate_caches()

        # An earlier import may have selected site-packages before the parser
        # runs. Drop only SpyDrNet modules so this parser consistently uses the
        # workspace checkout selected above.
        loaded = sys.modules.get("spydrnet")
        loaded_file = str(getattr(loaded, "__file__", "")) if loaded else ""
        if loaded_file and not loaded_file.startswith(repo_path):
            for module_name in tuple(sys.modules):
                if module_name == "spydrnet" or module_name.startswith("spydrnet."):
                    sys.modules.pop(module_name, None)

    sdn = importlib.import_module("spydrnet")  # type: ignore
    _patch_spydrnet_edif_parser()
    return sdn


def _property_map(instance) -> dict[str, Any]:
    props = {}
    raw_props = instance.data.get("EDIF.properties")
    if isinstance(raw_props, list):
        for item in raw_props:
            if not isinstance(item, dict):
                continue
            key = item.get("original_identifier") or item.get("identifier")
            if key:
                props[str(key)] = decode_edf_text(item.get("value"))
    return props


def _first_text(*values: Any) -> str | None:
    for value in values:
        decoded = decode_edf_text(value)
        if decoded and decoded != "None":
            return decoded
    return None


class EdfParser:
    """EDF/EDIF netlist parser based on SpyDrNet."""

    def __init__(self, file_path: str, progress_callback: Callable[[int, str], None] | None = None):
        self.file_path = file_path
        self.netlist = None
        self.warnings: list[str] = []
        self.stage_log: list[dict[str, Any]] = []
        self._progress_callback = progress_callback

    def _emit(self, percent: int | None, stage: str, *, level: str = "info") -> None:
        """Record a parse-stage event to the project logger and progress UI."""
        filename = Path(self.file_path).name
        message = f"[EDF parse] {filename}: {stage}"
        if level == "warn":
            _warn(message)
        elif level == "error":
            _error(message)
        else:
            _info(message)
        if percent is not None and self._progress_callback:
            try:
                self._progress_callback(percent, stage)
            except Exception:  # progress callback must not break parsing
                pass

    def _stage(self, name: str, percent_before: int, percent_after: int):
        """Context manager: log enter/exit + elapsed time + capture exceptions."""

        class _Stage:
            def __init__(inner, parser):
                inner.parser = parser

            def __enter__(inner):
                inner.t0 = time.time()
                inner.parser._emit(percent_before, f"{name} 开始")
                return inner

            def __exit__(inner, exc_type, exc, tb):
                elapsed = time.time() - inner.t0
                entry = {"stage": name, "elapsed_seconds": round(elapsed, 3)}
                if exc is not None:
                    entry["error"] = f"{exc_type.__name__}: {exc}"
                    entry["traceback"] = "".join(traceback.format_exception(exc_type, exc, tb))
                    inner.parser.stage_log.append(entry)
                    inner.parser._emit(
                        None,
                        f"{name} 失败（{elapsed:.2f}s）: {entry['error']}",
                        level="error",
                    )
                    return False  # re-raise; caller decides how to surface it
                inner.parser.stage_log.append(entry)
                inner.parser._emit(percent_after, f"{name} 完成（{elapsed:.2f}s）")
                return False

        return _Stage(self)

    def parse(self) -> tuple[list[ComponentInstance], list[Net], list]:
        file_size = Path(self.file_path).stat().st_size if Path(self.file_path).exists() else -1
        self._emit(40, f"读取 EDF（{file_size} bytes）")

        spydrnet_failed = False
        instances: list[ComponentInstance] = []
        nets: list[Net] = []

        try:
            with self._stage("加载 SpyDrNet", 41, 43):
                sdn = _load_spydrnet()
            logging.disable(logging.WARNING)
            try:
                with self._stage("SpyDrNet 解析", 44, 55):
                    self.netlist = sdn.parse(self.file_path)
            finally:
                logging.disable(logging.NOTSET)
        except Exception as exc:
            # SpyDrNet is brittle on OrCAD Capture EDIF (NotImplementedError /
            # "Parse error" lines). Fall through to the lightweight parser so
            # the upload pipeline still produces a CircuitDesign.
            spydrnet_failed = True
            self.warnings.append(
                f"SpyDrNet fallback: {type(exc).__name__}: {exc}; retrying with edif_lite parser."
            )
            self._emit(
                None,
                f"SpyDrNet 解析失败（{type(exc).__name__}），切换轻量解析器 edif_lite",
                level="warn",
            )

        if not spydrnet_failed:
            with self._stage("抽取元件实例", 56, 60):
                instances = self._extract_instances()
            self._emit(60, f"抽取到 {len(instances)} 个实例")
            with self._stage("抽取网络", 61, 63):
                nets = self._extract_nets(instances)
            self._emit(63, f"抽取到 {len(nets)} 个网络")

        # SpyDrNet sometimes "succeeds" but yields no instances on
        # OrCAD-flavoured files (it silently drops blocks it doesn't
        # understand). Treat that as a fallback trigger too.
        if spydrnet_failed or not instances:
            if not spydrnet_failed and not instances:
                self._emit(
                    None,
                    "SpyDrNet 解析后未抽到实例，自动切换 edif_lite 解析器",
                    level="warn",
                )
                self.warnings.append(
                    "SpyDrNet produced zero instances; switched to edif_lite parser."
                )
            with self._stage("edif_lite 解析", 44, 60):
                from src.circuit.parsers.edif_lite_parser import parse_orcad_edif

                instances, nets = parse_orcad_edif(
                    self.file_path, source_label=Path(self.file_path).name
                )
            self._emit(
                60, f"edif_lite 抽取到 {len(instances)} 个实例 / {len(nets)} 个网络"
            )
            for net in nets:
                net.net_type = classify_net_name(net.name)

        with self._stage("划分模块", 64, 65):
            modules = partition_by_refdes_page(instances, nets)
        self._emit(65, f"划分到 {len(modules)} 个模块")
        # If we successfully parsed via edif_lite (which propagates ``Page Name``)
        # we expect ``orcad_page_name`` modules. Falling through to the legacy
        # refdes-prefix bucketing means the page-name signal was dropped
        # somewhere — surface that so it doesn't silently degrade UX.
        if instances and modules and all(
            m.strategy == "refdes_page" for m in modules
        ):
            any_page_name = any(
                (inst.properties or {}).get("Page Name") for inst in instances
            )
            if any_page_name:
                self.warnings.append(
                    "Module partitioning fell back to refdes-prefix grouping "
                    "even though instances carry Page Name — partitioner bug?"
                )
                self._emit(
                    None,
                    "模块划分降级到 refdes 桶（实例已有 Page Name），请检查 _partition_by_page",
                    level="warn",
                )
        if not instances:
            self.warnings.append("EDF file produced no component instances with pins (after fallback).")
        return instances, nets, modules

    def _extract_instances(self) -> list[ComponentInstance]:
        by_refdes: dict[str, ComponentInstance] = {}
        source = Path(self.file_path).name
        for library in self.netlist.libraries:
            for definition in library.get_definitions():
                for instance in definition.children:
                    pins = list(instance.pins)
                    refdes = _first_text(
                        instance.data.get("EDIF.designator"),
                        instance.data.get("EDIF.designator.stringDisplay"),
                        instance.data.get(".NAME"),
                        instance.name,
                    )
                    if not refdes or not pins:
                        continue
                    props = _property_map(instance)
                    pin_models = []
                    for pin in pins:
                        wire_name = pin.wire.cable.name if pin.wire is not None else None
                        port_names = [
                            _first_text(
                                getattr(port, "name", None),
                                getattr(port, "data", {}).get("EDIF.designator"),
                            )
                            for port in pin.get_ports()
                        ]
                        pin_name = next((name for name in port_names if name), None)
                        pin_models.append(Pin(name=pin_name or "unknown", net=wire_name))

                    current = by_refdes.get(refdes)
                    if current is None:
                        # OrCAD writes most properties as both a display name
                        # (``"Part Number"``) and an uppercase SYMBOL alias
                        # (``PART_NUMBER``). Probe both so SpyDrNet-parsed
                        # files surface the same fields as edif_lite.
                        def _prop(*keys: str) -> Any:
                            for key in keys:
                                value = props.get(key)
                                if value not in (None, ""):
                                    return value
                            return None

                        by_refdes[refdes] = ComponentInstance(
                            refdes=refdes,
                            library_cell=getattr(instance.reference, "name", None),
                            part_number=_prop(
                                "Part Number",
                                "PART_NUMBER",
                                "Manufacturer Part Number",
                                "MANUFACTURER_PART_NUMBER",
                            ),
                            footprint=_prop("PCB Footprint", "PCB_FOOTPRINT"),
                            value=_first_text(
                                instance.data.get("EDIF.properties.stringDisplay"),
                                _prop("Value", "VALUE"),
                            ),
                            erp_number=_prop("ERP NUM", "ERP_NUM"),
                            pins=pin_models,
                            properties=props,
                            provenance={
                                "refdes": FieldProvenance(source, "spydrnet"),
                                "pins": FieldProvenance(source, "spydrnet"),
                            },
                        )
                    else:
                        current.pins.extend(pin_models)
                        current.properties.update({k: v for k, v in props.items() if v and k not in current.properties})
        return sorted(by_refdes.values(), key=lambda inst: inst.refdes)

    def _extract_nets(self, instances: list[ComponentInstance]) -> list[Net]:
        connections: dict[str, list[PinRef]] = {}
        for inst in instances:
            for pin in inst.pins:
                if not pin.net:
                    continue
                connections.setdefault(pin.net, []).append(PinRef(refdes=inst.refdes, pin=pin.name))
        return [
            Net(name=name, connections=refs, net_type=classify_net_name(name))
            for name, refs in sorted(connections.items())
        ]
