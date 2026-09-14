"""Deterministic validation of rendered document packages.

The artifact reviewer is deliberately independent from the semantic writer.
It reads only the generated package, the server-owned render manifest and the
frozen render specification.  A renderer may reject an unsafe write earlier;
this second pass still verifies the physical result before it can be
released.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.document_authoring.models import content_hash
from src.document_authoring.ooxml import validate_ooxml_package
from src.document_authoring.planning.review_contracts import ReviewIssue
from src.document_authoring.renderers.xlsm import NS, XlsmRenderer
from src.document_authoring.template_analysis import workbook_cell_coordinates


class ArtifactReviewReport(BaseModel):
    """Serializable result of one post-render artifact inspection."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    stage: str = "post_render"
    plan_id: str = ""
    plan_version: int | None = None
    plan_hash: str = ""
    model_hash: str
    document_model_hash: str = ""
    artifact_hash: str
    render_manifest_hash: str = ""
    format: str = "xlsx"
    status: str = "pass"
    issues: list[ReviewIssue] = Field(default_factory=list)
    report_hash: str = ""

    @model_validator(mode="after")
    def normalize_and_hash(self) -> "ArtifactReviewReport":
        if self.stage != "post_render":
            raise ValueError("artifact review reports must use post_render stage")
        self.plan_id = self.plan_id.strip()
        self.plan_hash = self.plan_hash.strip()
        self.model_hash = self.model_hash.strip()
        self.document_model_hash = self.document_model_hash.strip() or self.model_hash
        self.artifact_hash = self.artifact_hash.strip()
        self.render_manifest_hash = self.render_manifest_hash.strip()
        self.format = self.format.strip().lower().lstrip(".")
        self.status = _status_for(self.issues)
        expected = content_hash(self.model_dump(mode="json", exclude={"report_hash"}))
        if self.report_hash and self.report_hash != expected:
            raise ValueError("artifact review report hash does not match its contents")
        self.report_hash = expected
        return self

    @property
    def blocking(self) -> bool:
        return any(bool(issue.blocking) for issue in self.issues)

    @property
    def issue_codes(self) -> list[str]:
        return list(dict.fromkeys(issue.code for issue in self.issues))


class ArtifactReviewer:
    """Parse and validate XLSX/XLSM output against server-owned expectations."""

    def review(
        self,
        plan: Any,
        document_model: Any,
        render_result: Any,
        artifact_bytes: bytes,
    ) -> ArtifactReviewReport:
        spec = _render_spec(plan)
        format_name = _format_name(plan, render_result, spec)
        plan_id = str(_value(plan, "document_plan_id", _value(plan, "plan_id", "")) or "")
        plan_version = _value(plan, "version", _value(plan, "plan_version", None))
        plan_hash = str(_value(plan, "plan_hash", "") or "")
        model_hash = str(_value(document_model, "model_hash", "") or "")
        artifact_hash = hashlib.sha256(bytes(artifact_bytes)).hexdigest()
        manifest = _render_manifest(render_result)
        manifest_hash = str(manifest.get("manifest_hash") or "")
        issues: list[ReviewIssue] = []

        rendered_content = _value(render_result, "content", None)
        if rendered_content is not None and bytes(rendered_content) != bytes(artifact_bytes):
            issues.append(_issue(
                "artifact_content_hash_mismatch",
                suggested_action="reload_the_exact_rendered_artifact",
            ))
        if not manifest_hash:
            issues.append(_issue(
                "render_manifest_missing",
                suggested_action="persist_the_server_render_manifest",
            ))
        for key in ("policy_violations", "cell_policy_violations"):
            for violation in manifest.get(key, []) or []:
                issues.append(_issue(
                    "renderer_integrity_violation",
                    severity="critical",
                    suggested_action="block_release_and_inspect_renderer_manifest",
                    detail=str(violation),
                ))

        inventory: _WorkbookInventory | None = None
        if format_name in {"xlsx", "xlsm"}:
            try:
                validate_ooxml_package(
                    bytes(artifact_bytes), format_name, require_content_types=False,
                )
                inventory = _WorkbookInventory.from_bytes(bytes(artifact_bytes))
            except (ValueError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
                issues.append(_issue(
                    "malformed_ooxml",
                    severity="critical",
                    suggested_action="regenerate_from_the_frozen_template",
                    detail=str(exc),
                ))

        if inventory is not None:
            issues.extend(_review_workbook(inventory, spec, manifest))
            issues.extend(_review_active_content(
                inventory,
                format_name,
                spec,
                manifest,
            ))
        else:
            # DOCX/Markdown are still represented by a post-render gate.  The
            # existing format-specific renderer remains authoritative for
            # their package checks; this review consumes its manifest facts.
            if format_name not in {"xlsx", "xlsm"} and not artifact_bytes:
                issues.append(_issue(
                    "empty_artifact",
                    severity="critical",
                    suggested_action="regenerate_artifact",
                ))

        issues.extend(_review_model_hash_binding(plan, document_model, render_result))
        return ArtifactReviewReport(
            plan_id=plan_id,
            plan_version=plan_version,
            plan_hash=plan_hash,
            model_hash=model_hash,
            document_model_hash=model_hash,
            artifact_hash=artifact_hash,
            render_manifest_hash=manifest_hash,
            format=format_name,
            issues=_dedupe_issues(issues),
        )


class _WorkbookInventory:
    def __init__(self, package_parts: dict[str, bytes], sheets: dict[str, str], roots: dict[str, ET.Element]):
        self.package_parts = package_parts
        self.sheets = sheets
        self.roots = roots

    @classmethod
    def from_bytes(cls, content: bytes) -> "_WorkbookInventory":
        with zipfile.ZipFile(io.BytesIO(content), "r") as package:
            names = package.namelist()
            parts = {name: package.read(name) for name in names}
            sheets = XlsmRenderer._worksheet_part_map(package)
        roots = {name: ET.fromstring(parts[name]) for name in sheets.values()}
        return cls(parts, sheets, roots)

    def cell(self, sheet_name: str, ref: str) -> ET.Element | None:
        root = self.roots.get(self.sheets.get(sheet_name, ""))
        if root is None:
            return None
        normalized = ref.upper()
        return next(
            (
                cell for cell in root.findall(".//x:sheetData/x:row/x:c", NS)
                if str(cell.attrib.get("r", "")).upper() == normalized
            ),
            None,
        )

    def value(self, sheet_name: str, ref: str) -> str | None:
        cell = self.cell(sheet_name, ref)
        if cell is None:
            return None
        if cell.find("x:f", NS) is not None:
            return None
        if cell.attrib.get("t") == "inlineStr":
            return "".join(text.text or "" for text in cell.findall(".//x:t", NS))
        return cell.findtext("x:v", default="", namespaces=NS) or None

    def formula(self, sheet_name: str, ref: str) -> str | None:
        cell = self.cell(sheet_name, ref)
        if cell is None:
            return None
        formula = cell.find("x:f", NS)
        return None if formula is None else (formula.text or "")

    def merged_ranges(self, sheet_name: str) -> set[str]:
        root = self.roots.get(self.sheets.get(sheet_name, ""))
        if root is None:
            return set()
        return {
            str(node.attrib.get("ref", "")).upper()
            for node in root.findall(".//x:mergeCell", NS)
            if node.attrib.get("ref")
        }


def _review_workbook(
    inventory: _WorkbookInventory,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    expected_sheets = [str(item) for item in spec.get("expected_sheets", []) or []]
    for sheet in expected_sheets:
        if sheet not in inventory.sheets:
            issues.append(_issue(
                "wrong_physical_sheet", severity="critical",
                suggested_action="bind_to_a_registered_template_sheet",
                detail=sheet,
            ))

    changes = list(manifest.get("cell_changes", []) or [])
    allowlisted = _allowlisted_cells(spec)
    for change in changes:
        sheet = str(change.get("sheet_name", "")).strip()
        ref = str(change.get("cell", "")).strip().upper()
        if sheet not in inventory.sheets:
            issues.append(_issue(
                "wrong_physical_sheet", severity="critical", unit_id=change.get("semantic_unit_id"),
                row_key=change.get("row_key"), suggested_action="use_the_registered_sheet_binding",
                detail=f"{sheet}!{ref}",
            ))
        else:
            try:
                workbook_cell_coordinates(ref)
            except ValueError:
                issues.append(_issue(
                    "wrong_physical_row", severity="critical", unit_id=change.get("semantic_unit_id"),
                    row_key=change.get("row_key"), suggested_action="use_the_registered_cell_binding",
                    detail=f"{sheet}!{ref}",
                ))
            else:
                if inventory.cell(sheet, ref) is None:
                    issues.append(_issue(
                        "wrong_physical_row", severity="critical", unit_id=change.get("semantic_unit_id"),
                        row_key=change.get("row_key"), suggested_action="use_the_registered_cell_binding",
                        detail=f"{sheet}!{ref}",
                    ))
        if allowlisted and f"{sheet}!{ref}" not in allowlisted:
            issues.append(_issue(
                "non_allowlisted_cell", severity="critical", unit_id=change.get("semantic_unit_id"),
                row_key=change.get("row_key"), suggested_action="remove_the_unregistered_write",
                detail=f"{sheet}!{ref}",
            ))

    issues.extend(_review_expected_bindings(inventory, spec, changes))
    issues.extend(_review_static_cells(inventory, spec.get("static_cells", {}) or {}))
    issues.extend(_review_formula_cells(inventory, spec.get("formula_cells", {}) or {}))
    issues.extend(_review_merge_ranges(inventory, spec.get("merged_ranges", {}) or {}))
    issues.extend(_review_required_values(inventory, spec))
    return issues


def _review_expected_bindings(
    inventory: _WorkbookInventory,
    spec: Mapping[str, Any],
    changes: list[Mapping[str, Any]],
) -> list[ReviewIssue]:
    expected = spec.get("expected_bindings") or spec.get("physical_bindings") or {}
    if not isinstance(expected, Mapping):
        return []
    observed: dict[str, set[str]] = {}
    for change in changes:
        unit_id = str(change.get("semantic_unit_id", "")).strip()
        if unit_id:
            observed.setdefault(unit_id, set()).add(
                f"{change.get('sheet_name', '')}!{str(change.get('cell', '')).upper()}"
            )
    issues: list[ReviewIssue] = []
    for unit_id, raw in expected.items():
        locations = _locations(raw)
        actual = observed.get(str(unit_id), set())
        if locations and actual != locations:
            issues.append(_issue(
                "wrong_physical_mapping", severity="critical", unit_id=str(unit_id),
                suggested_action="rebind_to_the_frozen_physical_region",
                detail=f"expected={sorted(locations)} actual={sorted(actual)}",
            ))
    return issues


def _review_static_cells(inventory: _WorkbookInventory, static: Any) -> list[ReviewIssue]:
    if not isinstance(static, Mapping):
        return []
    issues: list[ReviewIssue] = []
    for locator, expected in static.items():
        sheet, ref = _split_locator(locator)
        if sheet not in inventory.sheets:
            issues.append(_issue("wrong_physical_sheet", severity="critical", detail=str(locator)))
            continue
        actual = inventory.value(sheet, ref)
        if actual != str(expected):
            issues.append(_issue(
                "static_content_overwrite", severity="critical",
                suggested_action="restore_the_frozen_static_content",
                detail=f"{sheet}!{ref}",
            ))
    return issues


def _review_formula_cells(inventory: _WorkbookInventory, formulas: Any) -> list[ReviewIssue]:
    if not isinstance(formulas, Mapping):
        return []
    issues: list[ReviewIssue] = []
    for locator, expected in formulas.items():
        sheet, ref = _split_locator(locator)
        actual = inventory.formula(sheet, ref) if sheet in inventory.sheets else None
        expected_formula = str(expected).lstrip("=")
        if actual != expected_formula:
            issues.append(_issue(
                "formula_changed", severity="critical",
                suggested_action="restore_the_frozen_formula_region",
                detail=f"{sheet}!{ref}",
            ))
    return issues


def _review_merge_ranges(inventory: _WorkbookInventory, merged: Any) -> list[ReviewIssue]:
    if not isinstance(merged, Mapping):
        return []
    issues: list[ReviewIssue] = []
    for sheet, expected_values in merged.items():
        expected = {str(value).upper() for value in expected_values or []}
        actual = inventory.merged_ranges(str(sheet)) if str(sheet) in inventory.sheets else set()
        if actual != expected:
            issues.append(_issue(
                "merge_changed", severity="critical",
                suggested_action="restore_the_frozen_merge_layout",
                detail=f"{sheet}: expected={sorted(expected)} actual={sorted(actual)}",
            ))
    return issues


def _review_required_values(inventory: _WorkbookInventory, spec: Mapping[str, Any]) -> list[ReviewIssue]:
    required = spec.get("required_values", {}) or {}
    limits = spec.get("max_cell_lengths") or spec.get("cell_length_limits") or {}
    issues: list[ReviewIssue] = []
    if isinstance(required, Mapping):
        for locator, expected in required.items():
            sheet, ref = _split_locator(locator)
            actual = inventory.value(sheet, ref) if sheet in inventory.sheets else None
            if actual != str(expected):
                issues.append(_issue(
                    "required_value_mismatch", severity="critical",
                    suggested_action="regenerate_the_required_value",
                    detail=f"{sheet}!{ref}",
                ))
    if isinstance(limits, Mapping):
        for locator, limit in limits.items():
            try:
                max_length = int(limit)
            except (TypeError, ValueError):
                continue
            sheet, ref = _split_locator(locator)
            actual = inventory.value(sheet, ref) if sheet in inventory.sheets else None
            expected = required.get(locator) if isinstance(required, Mapping) else None
            if (
                (actual is not None and len(actual) > max_length)
                or (expected is not None and len(str(expected)) > max_length)
            ):
                issues.append(_issue(
                    "overflow_or_truncation", severity="critical",
                    suggested_action="use_bounded_layout_rework_or_human_review",
                    detail=f"{sheet}!{ref}",
                ))
    return issues


def _review_active_content(
    inventory: _WorkbookInventory,
    format_name: str,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> list[ReviewIssue]:
    if format_name != "xlsm":
        return []
    names = set(inventory.package_parts)
    macro_parts = {name for name in names if name.lower().endswith("vbaproject.bin") or "/vba" in name.lower()}
    external_parts = {name for name in names if name.startswith("xl/externalLinks/")}
    macro_policy = str(spec.get("macro_policy", "quarantine")).strip().casefold()
    external_policy = str(spec.get("external_link_policy", "strip")).strip().casefold()
    issues: list[ReviewIssue] = []
    if macro_parts and macro_policy != "preserve":
        issues.append(_issue(
            "macro_policy_violation", severity="critical",
            suggested_action="strip_or_explicitly_approve_macro_content",
            detail=", ".join(sorted(macro_parts)),
        ))
    if external_parts and external_policy != "preserve":
        issues.append(_issue(
            "external_link_policy_violation", severity="critical",
            suggested_action="strip_or_explicitly_approve_external_links",
            detail=", ".join(sorted(external_parts)),
        ))
    before = manifest.get("before_parts") or manifest.get("before_package_parts") or {}
    after = manifest.get("after_parts") or manifest.get("after_package_parts") or {}
    for part in sorted(macro_parts | external_parts):
        if isinstance(before, Mapping) and isinstance(after, Mapping) and part in before and part in after:
            if before[part] != after[part]:
                issue_code = "macro_package_changed" if part in macro_parts else "external_link_package_changed"
                issues.append(_issue(
                    issue_code, severity="critical",
                    suggested_action="preserve_active_content_bytes_or_block_release",
                    detail=part,
                ))
    return issues


def _review_model_hash_binding(plan: Any, document_model: Any, render_result: Any) -> list[ReviewIssue]:
    model_hash = str(_value(document_model, "model_hash", "") or "")
    manifest = _render_manifest(render_result)
    declared = str(
        manifest.get("document_model_hash")
        or manifest.get("model_hash")
        or ""
    )
    if declared and declared != model_hash:
        return [_issue(
            "document_model_hash_mismatch", severity="critical",
            suggested_action="render_again_from_the_bound_document_model",
        )]
    plan_hash = str(_value(plan, "plan_hash", "") or "")
    manifest_plan = str(manifest.get("plan_hash") or "")
    if manifest_plan and plan_hash and manifest_plan != plan_hash:
        return [_issue(
            "document_plan_hash_mismatch", severity="critical",
            suggested_action="discard_the_stale_render_candidate",
        )]
    return []


def _render_spec(plan: Any) -> dict[str, Any]:
    value = _value(plan, "render_spec", {}) or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _render_manifest(render_result: Any) -> dict[str, Any]:
    value = _value(render_result, "integrity_manifest", {}) or {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _format_name(plan: Any, render_result: Any, spec: Mapping[str, Any]) -> str:
    candidate = spec.get("format") or _value(plan, "target_format", None)
    if not candidate:
        security = _value(render_result, "security_report", None)
        candidate = _value(security, "format", None)
    return str(candidate or "xlsx").strip().lower().lstrip(".")


def _allowlisted_cells(spec: Mapping[str, Any]) -> set[str]:
    values = spec.get("allowlisted_cells") or spec.get("allowlist") or []
    if isinstance(values, Mapping):
        values = [f"{sheet}!{ref}" for sheet, refs in values.items() for ref in (refs or [])]
    result: set[str] = set()
    for value in values:
        try:
            sheet, ref = _split_locator(value)
        except ValueError:
            continue
        result.add(f"{sheet}!{ref}")
    return result


def _locations(raw: Any) -> set[str]:
    if isinstance(raw, Mapping):
        raw = raw.get("cells") or raw.get("locations") or raw.get("target_cells") or []
    if isinstance(raw, str):
        raw = [raw]
    return {
        f"{_split_locator(value)[0]}!{_split_locator(value)[1]}"
        for value in (raw or [])
        if isinstance(value, str) and "!" in value
    }


def _split_locator(value: Any) -> tuple[str, str]:
    text = str(value)
    if "!" not in text:
        raise ValueError("physical cell locator must contain sheet!cell")
    sheet, ref = text.rsplit("!", 1)
    if not sheet.strip() or not ref.strip():
        raise ValueError("physical cell locator must contain sheet!cell")
    return sheet.strip(), ref.strip().upper()


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _issue(
    code: str,
    *,
    severity: str = "error",
    unit_id: Any = None,
    row_key: Any = None,
    column_id: Any = None,
    suggested_action: str = "",
    detail: str | None = None,
) -> ReviewIssue:
    action = suggested_action
    if detail and not action:
        action = "inspect_the_deterministic_review_detail"
    return ReviewIssue(
        stage="post_render", code=code, severity=severity,
        unit_id=str(unit_id).strip() if unit_id else None,
        row_key=str(row_key).strip() if row_key else None,
        column_id=str(column_id).strip() if column_id else None,
        suggested_action=action,
    )


def _status_for(issues: list[ReviewIssue]) -> str:
    if any(issue.blocking for issue in issues):
        return "blocked"
    return "needs_review" if issues else "pass"


def _dedupe_issues(issues: list[ReviewIssue]) -> list[ReviewIssue]:
    result: list[ReviewIssue] = []
    seen: set[tuple[Any, ...]] = set()
    for issue in issues:
        key = (issue.code, issue.unit_id, issue.row_key, issue.column_id, issue.suggested_action)
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


__all__ = ["ArtifactReviewReport", "ArtifactReviewer"]
