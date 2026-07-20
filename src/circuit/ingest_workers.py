from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from llama_index.core.schema import TextNode

from src.circuit.image_cache import ImageCache
from src.circuit.models import CircuitDesign
from src.circuit.orchestrator import CircuitOrchestrator
from src.circuit.parsers.edf_parser import EdfParser
from src.circuit.parsers.pdf_schematic_parser import PdfSchematicParser
from src.circuit.store import CircuitStore, make_design_id
from src.core.logger import error as _error, log as _info
from src.ingestion.source_groups import SCHEMATIC_GROUP


PARSE_LOG_FILENAME = "parse.log"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_parse_log(
    kb_name: str,
    design_id: str,
    *,
    filename: str,
    file_path: str,
    source_type: str,
    stage_log: list[dict[str, Any]],
    warnings: list[str],
    outcome: str,
    error_message: str = "",
    error_traceback: str = "",
    extra: dict[str, Any] | None = None,
) -> str | None:
    """Persist a per-design parse trace to ``<design_dir>/parse.log``.

    Always best-effort — never raises. Returns the path written, or None if
    the design directory could not be created.
    """
    try:
        store = CircuitStore()
        design_dir = store.design_dir(kb_name, design_id, create=True)
    except Exception as dir_exc:  # pragma: no cover - defensive
        _error(f"parse.log: cannot resolve design dir for {kb_name}/{design_id}: {dir_exc}")
        return None

    target = os.path.join(design_dir, PARSE_LOG_FILENAME)
    payload = {
        "timestamp": _utcnow(),
        "kb_name": kb_name,
        "design_id": design_id,
        "filename": filename,
        "file_path": file_path,
        "source_type": source_type,
        "outcome": outcome,
        "error": error_message,
        "traceback": error_traceback,
        "warnings": warnings,
        "stages": stage_log,
        "extra": extra or {},
    }
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
            fh.write("\n---\n")
    except OSError as write_exc:  # pragma: no cover - disk full / perm
        _error(f"parse.log: write failed for {target}: {write_exc}")
        return None
    return target


def _summary_nodes(design: CircuitDesign, filename: str) -> list[TextNode]:
    power_nets = [net.name for net in design.nets if net.net_type in {"power", "ground"}]
    clock_nets = [net.name for net in design.nets if net.net_type == "clock"]
    text = "\n".join(
        [
            f"Circuit design: {design.design_id}",
            f"Status: {design.status}",
            f"Instances: {len(design.instances)}",
            f"Nets: {len(design.nets)}",
            f"Modules: {len(design.modules)}",
            f"Power/Ground nets: {', '.join(power_nets[:50])}",
            f"Clock nets: {', '.join(clock_nets[:30])}",
            "",
            "Top modules:",
            *[
                f"- {module.name}: {len(module.instances)} instances, {len(module.nets)} nets"
                for module in design.modules[:30]
            ],
        ]
    )
    return [
        TextNode(
            text=text,
            metadata={
                "file_name": filename,
                "source_type": "edf_netlist",
                "design_id": design.design_id,
                "instance_count": len(design.instances),
                "net_count": len(design.nets),
                "module_count": len(design.modules),
            },
        )
    ]


def parse_edf_netlist(
    file_path: str,
    filename: str,
    kb_name: str,
    progress_callback: Callable[[int, str], None] | None = None,
) -> list[TextNode]:
    design_id = make_design_id(filename)
    if progress_callback:
        progress_callback(42, "Parsing EDF netlist")
    _info(f"[EDF parse] {filename}: kicked off (kb={kb_name}, design_id={design_id})")
    parser = EdfParser(file_path, progress_callback=progress_callback)
    started = time.time()
    try:
        instances, nets, modules = parser.parse()
    except Exception as exc:
        # Persist the failure trace so the UI / operator can read it back.
        tb = traceback.format_exc()
        _error(f"[EDF parse] {filename}: aborted after {time.time()-started:.2f}s — {type(exc).__name__}: {exc}")
        log_path = _write_parse_log(
            kb_name,
            design_id,
            filename=filename,
            file_path=file_path,
            source_type="edf_netlist",
            stage_log=list(parser.stage_log),
            warnings=list(parser.warnings),
            outcome="failed",
            error_message=f"{type(exc).__name__}: {exc}",
            error_traceback=tb,
            extra={"elapsed_seconds": round(time.time() - started, 3)},
        )
        log_hint = f"，详见 {log_path}" if log_path else ""
        raise RuntimeError(
            f"EDF 解析失败 ({type(exc).__name__}): {exc}{log_hint}"
        ) from exc

    elapsed = round(time.time() - started, 3)
    if progress_callback:
        progress_callback(58, "Persisting circuit graph")

    design = CircuitOrchestrator().apply_edf_parse(
        kb_name=kb_name,
        filename=filename,
        file_path=file_path,
        instances=instances,
        nets=nets,
        modules=modules,
        warnings=parser.warnings,
    )
    if progress_callback:
        progress_callback(65, f"EDF parsed: {len(instances)} instances, {len(nets)} nets")
    _info(
        f"[EDF parse] {filename}: success in {elapsed}s — "
        f"{len(instances)} instances / {len(nets)} nets / {len(modules)} modules"
    )
    _write_parse_log(
        kb_name,
        design.design_id,
        filename=filename,
        file_path=file_path,
        source_type="edf_netlist",
        stage_log=list(parser.stage_log),
        warnings=list(parser.warnings),
        outcome="success",
        extra={
            "elapsed_seconds": elapsed,
            "instances": len(instances),
            "nets": len(nets),
            "modules": len(modules),
        },
    )
    return _summary_nodes(design, filename)


def parse_schematic_pdf(
    file_path: str,
    filename: str,
    kb_name: str,
    progress_callback: Callable[[int, str], None] | None = None,
) -> list[TextNode]:
    design_id = make_design_id(filename)
    if progress_callback:
        progress_callback(42, "Parsing schematic PDF text layer")
    _info(f"[PDF parse] {filename}: kicked off (kb={kb_name}, design_id={design_id})")
    parser = PdfSchematicParser(file_path)
    started = time.time()
    try:
        pages = parser.parse()
    except Exception as exc:
        tb = traceback.format_exc()
        _error(f"[PDF parse] {filename}: aborted after {time.time()-started:.2f}s — {type(exc).__name__}: {exc}")
        log_path = _write_parse_log(
            kb_name,
            design_id,
            filename=filename,
            file_path=file_path,
            source_type="pdf_schematic",
            stage_log=[],
            warnings=list(getattr(parser, "warnings", []) or []),
            outcome="failed",
            error_message=f"{type(exc).__name__}: {exc}",
            error_traceback=tb,
            extra={"elapsed_seconds": round(time.time() - started, 3)},
        )
        log_hint = f"，详见 {log_path}" if log_path else ""
        raise RuntimeError(
            f"原理图 PDF 解析失败 ({type(exc).__name__}): {exc}{log_hint}"
        ) from exc

    elapsed = round(time.time() - started, 3)
    if progress_callback:
        progress_callback(58, "Persisting schematic pages")
    design = CircuitOrchestrator().apply_pdf_parse(
        kb_name=kb_name,
        filename=filename,
        file_path=file_path,
        pages=pages,
        warnings=parser.warnings,
    )
    # Mirror the schematic into the design's pdf_cache/ so downstream tools
    # (ImageCropper, schematic viewer) can find the source without depending on
    # the volatile circuit_uploads/ archive.
    try:
        ImageCache().cache_pdf(kb_name, design.design_id, file_path)
    except Exception as exc:  # pragma: no cover - cache is best-effort
        design.parse_warnings = sorted(set(design.parse_warnings + [f"pdf_cache: {exc}"]))
    nodes = []
    for page in pages:
        text = page.text or f"Schematic page {page.page_number} has no extractable text layer."
        nodes.append(
            TextNode(
                text=text,
                metadata={
                    "file_name": filename,
                    "source_group": SCHEMATIC_GROUP,
                    "source_type": "pdf_schematic",
                    "design_id": design.design_id,
                    "page_label": str(page.page_number),
                    "label_count": len(page.labels),
                    "cross_reference_count": len(design.cross_references),
                },
            )
        )
    if progress_callback:
        progress_callback(65, f"Schematic PDF parsed: {len(pages)} pages")
    _info(f"[PDF parse] {filename}: success in {elapsed}s — {len(pages)} pages")
    _write_parse_log(
        kb_name,
        design.design_id,
        filename=filename,
        file_path=file_path,
        source_type="pdf_schematic",
        stage_log=[],
        warnings=list(parser.warnings),
        outcome="success",
        extra={"elapsed_seconds": elapsed, "pages": len(pages)},
    )
    return nodes
