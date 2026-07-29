from __future__ import annotations

import os

import config.settings
from fastapi import APIRouter, Depends, HTTPException, Query

from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.store import CircuitStore, make_design_id
from src.core.app_logs import AppLogService
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser
from src.ingestion.kb_paths import validate_kb_name
from src.test_data.query_engine import TestDataQueryEngine

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    CircuitDesignDetailResponse,
    CircuitDesignsResponse,
    CircuitParseLogResponse,
    OkResponse,
    SchematicDesignsResponse,
    SchematicPageResponse,
    SpreadsheetLedgerResponse,
    StructuredRowsResponse,
)

router = APIRouter(tags=["structured"])

PARSE_LOG_FILENAME = "parse.log"
PARSE_LOG_TAIL_BYTES = 64 * 1024


def _require_kb_permission(user: AuthUser, kb_name: str, permission: str, auth: AuthService):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, permission):
        raise HTTPException(status_code=403, detail=f"{permission} permission required")
    return ctx


def _profile_summary(metadata: dict | None) -> dict:
    profile = (metadata or {}).get("spreadsheet_profile") or {}
    sheets = profile.get("sheets") or []
    return {
        "sheet_count": len(sheets),
        "row_count": sum(int(sheet.get("non_empty_row_count") or sheet.get("row_count") or 0) for sheet in sheets),
        "cell_count": sum(int(sheet.get("non_empty_cell_count") or 0) for sheet in sheets),
        "semantic_row_count": sum(int(sheet.get("semantic_row_count") or 0) for sheet in sheets),
        "block_count": sum(int(sheet.get("text_block_count") or 0) for sheet in sheets),
        "object_count": sum(int(sheet.get("object_count") or 0) for sheet in sheets),
        "profile": profile,
    }


def _spreadsheet_status(status: str) -> str:
    return {
        "completed": "已解析",
        "failed": "失败",
        "parsing": "解析中",
        "pending": "待解析",
        "uploaded": "待解析",
        "cancelled": "已取消",
    }.get(status or "", status or "-")


@router.get("/kbs/{kb_name}/structured/spreadsheets", response_model=SpreadsheetLedgerResponse)
def list_spreadsheets(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx = _require_kb_permission(user, kb_name, "read", auth)
    infos = pipeline.list_file_infos(kb_name, ctx=ctx)
    rows: list[dict] = []
    for info in infos:
        if getattr(info, "processor_kind", "") != "spreadsheet_table":
            continue
        metadata = info.metadata or {}
        summary = _profile_summary(metadata)
        profile = summary["profile"]
        rows.append(
            {
                "file_id": info.id,
                "file_name": info.name,
                "status": getattr(info, "status", ""),
                "status_label": _spreadsheet_status(getattr(info, "status", "")),
                "sheet_count": summary["sheet_count"],
                "row_count": summary["row_count"],
                "cell_count": summary["cell_count"],
                "semantic_row_count": summary["semantic_row_count"],
                "block_count": summary["block_count"],
                "object_count": summary["object_count"],
                "record_id": metadata.get("store_id", ""),
                "kb_id": profile.get("kb_id", ""),
                "archive_path": getattr(info, "local_path", "") or metadata.get("local_path", ""),
                "sheets": profile.get("sheets") or [],
            }
        )
    totals = {
        "file_count": len(rows),
        "sheet_count": sum(int(row["sheet_count"]) for row in rows),
        "semantic_row_count": sum(int(row["semantic_row_count"]) for row in rows),
        "pending_count": sum(1 for row in rows if row["status"] != "completed"),
    }
    return SpreadsheetLedgerResponse(totals=totals, rows=rows)


@router.get("/kbs/{kb_name}/structured/circuit-designs", response_model=CircuitDesignsResponse)
def list_circuit_designs(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "read", auth)
    engine = CircuitQueryEngine()
    designs = engine.list_designs(kb_name)
    known_ids = {str(design.get("design_id") or "") for design in designs}
    failed_logs = [
        {"design_id": design_id, "log_path": path}
        for design_id, path in _list_design_dirs_with_log(kb_name)
        if design_id not in known_ids
    ]
    return CircuitDesignsResponse(designs=designs, failed_logs=failed_logs)


@router.get("/kbs/{kb_name}/structured/circuit-designs/{design_id}", response_model=CircuitDesignDetailResponse)
def get_circuit_design(
    kb_name: str,
    design_id: str,
    net_query: str = "",
    instance_query: str = "",
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "read", auth)
    engine = CircuitQueryEngine()
    summary = engine.get_design_summary(kb_name, design_id)
    if not summary:
        raise HTTPException(status_code=404, detail="circuit design not found")
    modules = summary.get("modules") or []
    nets = [
        row for row in engine.search_nets(kb_name, net_query, limit=200)
        if row.get("design_id") == design_id
    ]
    instances = [
        row for row in engine.search_instances(kb_name, instance_query, limit=200)
        if row.get("design_id") == design_id
    ]
    cross_refs = [
        row for row in engine.search_cross_references(kb_name, limit=200)
        if row.get("design_id") == design_id
    ]
    return CircuitDesignDetailResponse(
        summary=summary,
        modules=modules,
        nets=nets,
        instances=instances,
        cross_references=cross_refs,
    )


@router.delete("/kbs/{kb_name}/structured/circuit-designs/{design_id}", response_model=OkResponse)
def delete_circuit_design(
    kb_name: str,
    design_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "write", auth)
    store = CircuitStore()
    try:
        removed = store.delete_design(kb_name, design_id)
        _delete_circuit_upload_archive(kb_name, design_id)
    except Exception as exc:
        _record_design_delete(user, kb_name, design_id, success=False, error_message=str(exc))
        raise HTTPException(status_code=400, detail=f"删除设计失败: {exc}") from exc
    if not removed:
        raise HTTPException(status_code=404, detail="circuit design not found")
    _record_design_delete(user, kb_name, design_id, success=True)
    return OkResponse(ok=True, message=f"已删除设计: {design_id}")


@router.get("/kbs/{kb_name}/structured/circuit-designs/{design_id}/parse-log", response_model=CircuitParseLogResponse)
def get_circuit_parse_log(
    kb_name: str,
    design_id: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "read", auth)
    path = _parse_log_path(kb_name, design_id)
    if not os.path.exists(path):
        return CircuitParseLogResponse(exists=False, path=path)
    try:
        size = os.path.getsize(path)
        truncated = size > PARSE_LOG_TAIL_BYTES
        with open(path, "rb") as fh:
            if truncated:
                fh.seek(-PARSE_LOG_TAIL_BYTES, os.SEEK_END)
            content = fh.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"读取解析日志失败: {exc}") from exc
    if truncated:
        content = "(已截断,仅显示最近 64KB)\n\n" + content
    return CircuitParseLogResponse(exists=True, path=path, size=size, truncated=truncated, content=content)


@router.get("/kbs/{kb_name}/structured/modules", response_model=StructuredRowsResponse)
def list_modules(
    kb_name: str,
    design_id: str = "",
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "read", auth)
    engine = CircuitQueryEngine()
    design_ids = [design_id] if design_id else [str(d.get("design_id") or "") for d in engine.list_designs(kb_name)]
    rows: list[dict] = []
    for did in design_ids:
        if not did:
            continue
        result = engine.list_modules(kb_name, did)
        for module in (result or {}).get("modules") or []:
            row = dict(module)
            row.setdefault("design_id", did)
            rows.append(row)
    return StructuredRowsResponse(rows=rows)


@router.get("/kbs/{kb_name}/structured/test-reports", response_model=StructuredRowsResponse)
def list_test_reports(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "read", auth)
    return StructuredRowsResponse(rows=TestDataQueryEngine().list_reports(kb_name))


@router.get("/kbs/{kb_name}/structured/test-measurements", response_model=StructuredRowsResponse)
def list_test_measurements(
    kb_name: str,
    query: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "read", auth)
    rows = TestDataQueryEngine().search_measurements(kb_name, query=query, limit=limit)
    return StructuredRowsResponse(rows=rows)


@router.get("/kbs/{kb_name}/structured/schematics", response_model=SchematicDesignsResponse)
def list_schematics(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "read", auth)
    designs = []
    store = CircuitStore()
    for design in store.list_designs(kb_name):
        if not design.schematic_pages:
            continue
        designs.append(
            {
                "design_id": design.design_id,
                "status": str(design.status),
                "page_count": len(design.schematic_pages),
                "label_count": sum(len(page.labels) for page in design.schematic_pages),
                "module_region_count": len(design.module_regions),
                "pages": [
                    {
                        "page_number": page.page_number,
                        "width": page.width,
                        "height": page.height,
                        "label_count": len(page.labels),
                        "text_preview": (page.text or "")[:200],
                    }
                    for page in design.schematic_pages
                ],
            }
        )
    return SchematicDesignsResponse(designs=designs)


@router.get("/kbs/{kb_name}/structured/schematics/{design_id}/pages/{page_number}", response_model=SchematicPageResponse)
def get_schematic_page(
    kb_name: str,
    design_id: str,
    page_number: int,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _require_kb_permission(user, kb_name, "read", auth)
    store = CircuitStore()
    design = store.load(kb_name, design_id)
    if not design:
        raise HTTPException(status_code=404, detail="schematic design not found")
    page = next((item for item in design.schematic_pages if item.page_number == page_number), None)
    if not page:
        raise HTTPException(status_code=404, detail="schematic page not found")
    module_regions = [region for region in design.module_regions if region.page_number == page_number]
    return SchematicPageResponse(
        design_id=design.design_id,
        page_number=page.page_number,
        width=page.width,
        height=page.height,
        text=page.text or "",
        labels=[
            {"text": label.text, "kind": label.kind, "bbox": label.bbox}
            for label in page.labels[:500]
        ],
        module_regions=[
            {
                "module_id": region.module_id,
                "bbox": region.bbox,
                "confidence": region.confidence,
                "strategy": region.strategy,
            }
            for region in module_regions
        ],
        screenshots=store.list_module_screenshots(kb_name, design.design_id),
        pdf_cache=store.list_pdf_cache(kb_name, design.design_id),
    )


def _parse_log_path(kb_name: str, design_id: str) -> str:
    return os.path.join(CircuitStore().design_dir(kb_name, design_id), PARSE_LOG_FILENAME)


def _list_design_dirs_with_log(kb_name: str) -> list[tuple[str, str]]:
    store = CircuitStore()
    kb_root = os.path.join(store.root, validate_kb_name(kb_name))
    if not os.path.isdir(kb_root):
        return []
    entries: list[tuple[str, str]] = []
    for name in sorted(os.listdir(kb_root)):
        candidate = os.path.join(kb_root, name, PARSE_LOG_FILENAME)
        if os.path.exists(candidate):
            entries.append((name, candidate))
    return entries


def _delete_circuit_upload_archive(kb_name: str, design_id: str) -> None:
    safe_kb_name = validate_kb_name(kb_name)
    safe_design_id = make_design_id(design_id)
    archive_root = os.path.join(config.settings.STORAGE_DIR, "circuit_uploads", safe_kb_name)
    if not os.path.isdir(archive_root):
        return
    for group_name in os.listdir(archive_root):
        group_dir = os.path.join(archive_root, group_name)
        if not os.path.isdir(group_dir):
            continue
        for filename in os.listdir(group_dir):
            stem, _ext = os.path.splitext(filename)
            normalized = make_design_id(stem)
            if normalized == safe_design_id or normalized.startswith(f"{safe_design_id}_"):
                try:
                    os.remove(os.path.join(group_dir, filename))
                except OSError:
                    pass


def _record_design_delete(
    user: AuthUser,
    kb_name: str,
    design_id: str,
    *,
    success: bool,
    error_message: str = "",
) -> None:
    try:
        AppLogService().record_audit(
            action="delete_circuit_design",
            actor=user,
            target_type="circuit_design",
            target_id=design_id,
            kb_name=kb_name,
            success=success,
            error_message=error_message,
            metadata={"source": "api"},
        )
    except Exception:
        pass
