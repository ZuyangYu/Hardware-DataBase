"""Minimal, allowlisted OOXML workbook renderer for P2a.

It changes only explicitly registered cells in worksheet parts.  In
particular, it does not load/save through a generic workbook object model,
which can discard VBA, controls, relationships or unknown OOXML parts.
"""

from __future__ import annotations

import copy
import hashlib
import io
import posixpath
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from src.document_authoring.models import (
    RendererPolicy,
    TemplateSecurityReport,
    WorkbookFillPlan,
    WorkbookRegionSchema,
    content_hash,
)
from src.document_authoring.template_analysis import (
    workbook_cell_coordinates,
    workbook_value_hash,
)


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"x": MAIN_NS, "r": REL_NS, "pr": PKG_REL_NS}
ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", REL_NS)

_FORMULA_PREFIXES = ("=", "+", "-", "@")


@dataclass(frozen=True)
class XlsmRenderResult:
    content: bytes
    security_report: TemplateSecurityReport
    integrity_manifest: dict[str, Any]


class XlsmRenderer:
    def inspect(self, content: bytes, fmt: str = "xlsm") -> TemplateSecurityReport:
        with zipfile.ZipFile(io.BytesIO(content), "r") as package:
            parts = {info.filename: hashlib.sha256(package.read(info.filename)).hexdigest() for info in package.infolist()}
            names = set(parts)
            macro_parts = sorted(name for name in names if name.endswith("vbaProject.bin") or "/vba" in name.lower())
            external_links = sorted(name for name in names if name.startswith("xl/externalLinks/"))
            embedded_parts = sorted(
                name for name in names
                if "/embeddings/" in name or name.startswith("xl/activeX/") or name.startswith("xl/ctrlProps/")
            )
            rel_parts = {
                name: parts[name]
                for name in names
                if name.endswith(".rels")
            }
        active = bool(macro_parts or external_links or embedded_parts)
        return TemplateSecurityReport(
            report_id=f"template-security-{content_hash(parts)[:16]}",
            content_hash=hashlib.sha256(content).hexdigest(),
            format=fmt, parts=parts, relationship_parts=rel_parts,
            macro_parts=macro_parts, external_links=external_links, embedded_parts=embedded_parts,
            active_content_status="requires_approval" if active else "clean",
        )

    def render(
        self,
        template_content: bytes,
        regions: list[WorkbookRegionSchema],
        fill_plan: WorkbookFillPlan,
        policy: RendererPolicy,
        *,
        security_approved: bool = False,
    ) -> XlsmRenderResult:
        report = self.inspect(template_content)
        self._validate_active_content(report, policy, security_approved)
        if len({region.region_id for region in regions}) != len(regions):
            raise ValueError("duplicate workbook region ids are not allowed")
        region_by_id = {region.region_id: region for region in regions}
        fills = []
        seen_region_ids: set[str] = set()
        seen_locators: set[tuple[str, str]] = set()
        for fill in fill_plan.fills:
            region = region_by_id.get(fill.region_id)
            if region is None:
                raise ValueError(f"fill references an unregistered region: {fill.region_id}")
            locator = (region.sheet_name, str(region.locator.get("cell", "")).upper())
            if fill.region_id in seen_region_ids or locator in seen_locators:
                raise ValueError(f"duplicate workbook fill target: {fill.region_id}")
            seen_region_ids.add(fill.region_id)
            seen_locators.add(locator)
            if region.write_policy not in {"deterministic_only", "validated_draft"}:
                raise PermissionError(f"region is not renderer-writable: {fill.region_id}")
            if region.role in {"formula", "human_input", "human_approval", "locked_template", "legacy_example"}:
                raise PermissionError(f"region role may not be machine-written: {region.role}")
            value = str(fill.value)
            if policy.reject_formula_like_text and value.startswith(_FORMULA_PREFIXES):
                # In OOXML an inline string is not calculated, but rejecting it
                # makes formula/link injection impossible across spreadsheet clients.
                raise ValueError(f"formula-like text is not allowed in generated content: {fill.region_id}")
            fills.append((region, value, fill.semantic_unit_id))

        long_value_counts: dict[str, int] = {}
        for _region, value, _semantic_unit_id in fills:
            if len(value) >= 80:
                long_value_counts[value] = long_value_counts.get(value, 0) + 1
        if any(count > 1 for count in long_value_counts.values()):
            raise ValueError("abnormal duplicate long value fan-out is not allowed")

        sheet_fills: dict[str, list[tuple[WorkbookRegionSchema, str]]] = {}
        for region, value, _semantic_unit_id in fills:
            sheet_fills.setdefault(region.sheet_name, []).append((region, value))

        source = io.BytesIO(template_content)
        output = io.BytesIO()
        changed_parts: set[str] = set()
        cell_changes: list[dict[str, Any]] = []
        with zipfile.ZipFile(source, "r") as src, zipfile.ZipFile(output, "w") as dst:
            worksheet_map = self._worksheet_part_map(src)
            shared_strings = (
                self._shared_strings(src.read("xl/sharedStrings.xml"))
                if "xl/sharedStrings.xml" in src.namelist()
                else []
            )
            required_sheets = set(sheet_fills)
            missing_sheets = required_sheets - set(worksheet_map)
            if missing_sheets:
                raise ValueError(f"template sheets not found: {sorted(missing_sheets)}")
            for region, value, semantic_unit_id in fills:
                ref = str(region.locator["cell"]).upper()
                baseline_value = self._cell_value(
                    src.read(worksheet_map[region.sheet_name]),
                    ref,
                    shared_strings,
                )
                baseline_hash = workbook_value_hash(baseline_value)
                if (
                    region.expected_value_hash is None
                    and baseline_value is not None
                ):
                    raise PermissionError(
                        f"workbook region has no frozen baseline for non-empty cell: "
                        f"{region.sheet_name}!{ref}"
                    )
                if (
                    region.expected_value_hash is not None
                    and baseline_hash != region.expected_value_hash
                ):
                    raise PermissionError(f"workbook cell baseline changed: {region.sheet_name}!{ref}")
                if (
                    region.expected_value_hash is not None
                    and baseline_value is not None
                    and not region.allow_nonempty_overwrite
                ):
                    raise PermissionError(
                        f"non-empty workbook overwrite is not authorized: {region.sheet_name}!{ref}"
                    )
                cell_changes.append({
                    "sheet_name": region.sheet_name,
                    "cell": ref,
                    "baseline_value_hash": baseline_hash,
                    "generated_value_hash": workbook_value_hash(value),
                    "baseline_empty": baseline_value is None,
                    "semantic_unit_id": semantic_unit_id,
                    "region_id": region.region_id,
                })
            replacements: dict[str, bytes] = {}
            for sheet_name, fill_values in sheet_fills.items():
                part = worksheet_map[sheet_name]
                replacements[part] = self._patch_worksheet(src.read(part), fill_values)
                changed_parts.add(part)
            for info in src.infolist():
                data = replacements.get(info.filename)
                if data is None:
                    data = src.read(info.filename)
                cloned = copy.copy(info)
                # Zip timestamps/attributes remain; only target worksheet
                # contents change.  This retains unknown package parts intact.
                dst.writestr(cloned, data)

        rendered = output.getvalue()
        after = self.inspect(rendered)
        if after.active_content_status != "clean":
            raise ValueError("generated artifact contains active content")
        manifest = self._integrity_manifest(
            report,
            after,
            changed_parts,
            policy,
            cell_changes,
        )
        if manifest["policy_violations"]:
            raise ValueError("renderer integrity policy rejected output: " + "; ".join(manifest["policy_violations"]))
        return XlsmRenderResult(content=rendered, security_report=after, integrity_manifest=manifest)

    @staticmethod
    def _shared_strings(content: bytes) -> list[str]:
        root = ET.fromstring(content)
        return [
            "".join(text.text or "" for text in item.findall(".//x:t", NS)).strip()
            for item in root.findall("x:si", NS)
        ]

    @staticmethod
    def _cell_value(
        worksheet: bytes,
        ref: str,
        shared_strings: list[str],
    ) -> str | None:
        root = ET.fromstring(worksheet)
        cell = next(
            (
                candidate
                for candidate in root.findall(".//x:sheetData/x:row/x:c", NS)
                if candidate.attrib.get("r", "").upper() == ref
            ),
            None,
        )
        if cell is None or cell.find("x:f", NS) is not None:
            return None
        cell_type = cell.attrib.get("t", "")
        if cell_type == "inlineStr":
            return (
                "".join(text.text or "" for text in cell.findall(".//x:t", NS)).strip()
                or None
            )
        value = cell.findtext("x:v", default="", namespaces=NS).strip()
        if cell_type == "s":
            try:
                return shared_strings[int(value)] or None
            except (ValueError, IndexError):
                return None
        if cell_type == "b":
            return "TRUE" if value == "1" else "FALSE"
        return value or None

    @staticmethod
    def _validate_active_content(report: TemplateSecurityReport, policy: RendererPolicy, security_approved: bool) -> None:
        if report.active_content_status == "clean":
            return
        if not security_approved:
            raise PermissionError("template active content requires an approved security review")
        if report.content_hash not in policy.allowlisted_template_hashes:
            raise PermissionError("renderer policy does not allow this template content hash")
        active = {
            "macro": (report.macro_parts, policy.macro_policy),
            "external link": (report.external_links, policy.external_link_policy),
            "embedded object": (report.embedded_parts, policy.embedded_object_policy),
        }
        blocked = [name for name, (parts, action) in active.items() if parts and action != "preserve"]
        if blocked:
            raise PermissionError(f"active content policy is not preserve-approved: {', '.join(blocked)}")

    @staticmethod
    def _worksheet_part_map(package: zipfile.ZipFile) -> dict[str, str]:
        workbook = ET.fromstring(package.read("xl/workbook.xml"))
        rels = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
        targets = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in rels.findall(f"{{{PKG_REL_NS}}}Relationship")
        }
        result: dict[str, str] = {}
        for sheet in workbook.findall("x:sheets/x:sheet", NS):
            relationship_id = sheet.attrib.get(f"{{{REL_NS}}}id")
            target = targets.get(relationship_id or "")
            if not target:
                continue
            result[sheet.attrib["name"]] = posixpath.normpath(posixpath.join("xl", target))
        return result

    @staticmethod
    def _patch_worksheet(source: bytes, fills: list[tuple[WorkbookRegionSchema, str]]) -> bytes:
        root = ET.fromstring(source)
        sheet_data = root.find("x:sheetData", NS)
        if sheet_data is None:
            sheet_data = ET.SubElement(root, f"{{{MAIN_NS}}}sheetData")
        rows = {int(row.attrib["r"]): row for row in sheet_data.findall("x:row", NS) if row.attrib.get("r", "").isdigit()}
        for region, value in fills:
            ref = str(region.locator["cell"]).upper()
            try:
                _column_number, row_number = workbook_cell_coordinates(ref)
            except ValueError:
                raise ValueError(f"invalid A1 cell locator: {ref}")
            row = rows.get(row_number)
            if row is None:
                row = ET.Element(f"{{{MAIN_NS}}}row", {"r": str(row_number)})
                inserted = False
                for index, candidate in enumerate(list(sheet_data)):
                    if int(candidate.attrib.get("r", "0")) > row_number:
                        sheet_data.insert(index, row)
                        inserted = True
                        break
                if not inserted:
                    sheet_data.append(row)
                rows[row_number] = row
            cell = next((candidate for candidate in row.findall("x:c", NS) if candidate.attrib.get("r") == ref), None)
            if cell is None:
                cell = ET.Element(f"{{{MAIN_NS}}}c", {"r": ref})
                row.append(cell)
            if region.preserve_formula or cell.find("x:f", NS) is not None:
                raise ValueError(f"renderer will not replace a formula cell: {ref}")
            for child in list(cell):
                cell.remove(child)
            cell.attrib["t"] = "inlineStr"
            inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
            text = ET.SubElement(inline, f"{{{MAIN_NS}}}t")
            if value[:1].isspace() or value[-1:].isspace():
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = value
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _integrity_manifest(
        before: TemplateSecurityReport,
        after: TemplateSecurityReport,
        changed_parts: set[str],
        policy: RendererPolicy,
        cell_changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        before_names, after_names = set(before.parts), set(after.parts)
        changed = sorted(
            name for name in before_names & after_names
            if before.parts[name] != after.parts[name]
        )
        policy_violations: list[str] = []
        if before_names != after_names:
            policy_violations.append("OOXML part set changed")
        if set(changed) != changed_parts:
            policy_violations.append("unexpected OOXML parts changed")
        allowed = tuple(policy.allowed_changed_parts)
        for name in changed:
            if not name.startswith(allowed):
                policy_violations.append(f"changed part outside policy: {name}")
        for sensitive in (before.macro_parts, before.external_links, before.embedded_parts):
            for name in sensitive:
                if before.parts.get(name) != after.parts.get(name):
                    policy_violations.append(f"active-content part changed: {name}")
        if before.relationship_parts != after.relationship_parts:
            policy_violations.append("relationship parts changed")
        return {
            "before_content_hash": before.content_hash,
            "after_content_hash": after.content_hash,
            "changed_parts": changed,
            "part_count_before": len(before.parts),
            "part_count_after": len(after.parts),
            "policy_violations": policy_violations,
            "cell_changes": cell_changes,
            "manifest_hash": content_hash({
                "before": before.parts,
                "after": after.parts,
                "changed": changed,
                "cell_changes": cell_changes,
            }),
        }
