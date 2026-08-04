"""Allowlisted, byte-preserving DOCX OOXML renderer.

The renderer deliberately edits only ``word/document.xml`` in a copied ZIP
package.  It never opens a Word object model, so unrelated OOXML parts,
relationships, active content, and unknown extensions remain byte-identical.
"""

from __future__ import annotations

import copy
import hashlib
import io
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import escape

from src.document_authoring.models import (
    DocxFillPlan,
    RendererPolicy,
    TemplateSecurityReport,
    content_hash,
)
from src.document_authoring.renderers.xlsm import XlsmRenderer
from src.document_authoring.template_analysis import DocxRegionSchema


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": WORD_NS, "pr": PACKAGE_REL_NS}
ET.register_namespace("w", WORD_NS)


@dataclass(frozen=True)
class DocxRenderResult:
    content: bytes
    security_report: TemplateSecurityReport
    integrity_manifest: dict[str, Any]


class DocxRenderer:
    """Render registered DOCX text regions while retaining package bytes elsewhere."""

    def inspect(self, content: bytes) -> TemplateSecurityReport:
        with zipfile.ZipFile(io.BytesIO(content), "r") as package:
            names_in_order = [info.filename for info in package.infolist()]
            if len(names_in_order) != len(set(names_in_order)):
                raise ValueError("DOCX package contains duplicate ZIP member names")
            parts = {
                info.filename: hashlib.sha256(package.read(info.filename)).hexdigest()
                for info in package.infolist()
            }
            names = set(parts)
            macro_parts = sorted(name for name in names if name.lower().endswith("vbaproject.bin") or "/vba" in name.lower())
            external_links = sorted(
                name for name in names
                if name.endswith(".rels") and self._has_external_relationship(package.read(name))
            )
            embedded_parts = sorted(
                name for name in names
                if "/embeddings/" in name.lower() or "/activex/" in name.lower()
            )
            relationship_parts = {name: parts[name] for name in names if name.endswith(".rels")}
        active = bool(macro_parts or external_links or embedded_parts)
        return TemplateSecurityReport(
            report_id=f"template-security-{content_hash(parts)[:16]}",
            content_hash=hashlib.sha256(content).hexdigest(),
            format="docx",
            parts=parts,
            relationship_parts=relationship_parts,
            macro_parts=macro_parts,
            external_links=external_links,
            embedded_parts=embedded_parts,
            active_content_status="requires_approval" if active else "clean",
        )

    def render(
        self,
        template_content: bytes,
        regions: list[DocxRegionSchema],
        fill_plan: DocxFillPlan,
        policy: RendererPolicy,
        *,
        security_approved: bool = False,
    ) -> DocxRenderResult:
        before = self.inspect(template_content)
        XlsmRenderer._validate_active_content(before, policy, security_approved)
        region_by_id = {region.region_id: region for region in regions}
        fills: list[tuple[DocxRegionSchema, str]] = []
        for fill in fill_plan.fills:
            region = region_by_id.get(fill.region_id)
            if region is None:
                raise ValueError(f"fill references an unregistered region: {fill.region_id}")
            if region.write_policy not in {"deterministic_only", "validated_draft"}:
                raise PermissionError(f"region may not be machine-written: {fill.region_id}")
            if region.role in {"formula", "human_input", "human_approval", "locked_template", "legacy_example"}:
                raise PermissionError(f"region role may not be machine-written: {region.role}")
            fills.append((region, str(fill.value)))

        source = io.BytesIO(template_content)
        output = io.BytesIO()
        changed_parts: set[str] = set()
        with zipfile.ZipFile(source, "r") as src:
            if "word/document.xml" not in src.namelist():
                raise ValueError("DOCX package is missing word/document.xml")
            document = src.read("word/document.xml")
            replacement = document if not fills else self._patch_document(document, fills, regions)
            if replacement != document:
                changed_parts.add("word/document.xml")
            with zipfile.ZipFile(output, "w") as dst:
                for info in src.infolist():
                    data = replacement if info.filename == "word/document.xml" else src.read(info.filename)
                    dst.writestr(copy.copy(info), data)

        rendered = output.getvalue()
        after = self.inspect(rendered)
        if after.active_content_status != "clean":
            raise ValueError("generated artifact contains active content")
        manifest = self._integrity_manifest(before, after, changed_parts, policy)
        if manifest["policy_violations"]:
            raise ValueError("renderer integrity policy rejected output: " + "; ".join(manifest["policy_violations"]))
        return DocxRenderResult(content=rendered, security_report=after, integrity_manifest=manifest)

    @staticmethod
    def _has_external_relationship(rels_xml: bytes) -> bool:
        root = ET.fromstring(rels_xml)
        return any(
            relation.attrib.get("TargetMode", "").lower() == "external"
            for relation in root.findall("pr:Relationship", NS)
        )

    @classmethod
    def _patch_document(
        cls,
        source: bytes,
        fills: list[tuple[DocxRegionSchema, str]],
        regions: list[DocxRegionSchema],
    ) -> bytes:
        root = ET.fromstring(source)
        all_texts = root.findall(".//w:t", NS)
        text_ranges = cls._text_ranges(source)
        if len(all_texts) != len(text_ranges):
            raise ValueError("DOCX text-node inventory does not match its XML bytes")
        text_indexes = {id(text): index for index, text in enumerate(all_texts)}
        protected_text_ids: set[int] = set()
        for region in regions:
            if cls._machine_writable(region):
                continue
            protected_text_ids.update(id(text) for text in cls._resolve_region(root, region).findall(".//w:t", NS))
        replacements: dict[int, str] = {}
        for region, value in fills:
            target = cls._resolve_region(root, region)
            texts = target.findall(".//w:t", NS)
            if not texts:
                raise ValueError(f"DOCX region has no writable text nodes: {region.region_id}")
            if any(id(text) in protected_text_ids for text in texts):
                raise PermissionError(f"DOCX fill overlaps a protected human-only region: {region.region_id}")
            for index, text in enumerate(texts):
                text_index = text_indexes[id(text)]
                if text_index in replacements:
                    raise PermissionError(f"DOCX fills overlap the same text node: {region.region_id}")
                replacements[text_index] = value if index == 0 else ""
        return cls._replace_raw_text(source, text_ranges, replacements)

    @staticmethod
    def _machine_writable(region: DocxRegionSchema) -> bool:
        return (
            region.write_policy in {"deterministic_only", "validated_draft"}
            and region.role not in {"formula", "human_input", "human_approval", "locked_template", "legacy_example"}
        )

    @classmethod
    def _resolve_region(cls, root: ET.Element, region: DocxRegionSchema) -> ET.Element:
        locator = region.locator
        if set(locator) == {"paragraph_index"}:
            index = cls._non_negative_index(locator["paragraph_index"], region.region_id)
            body = root.find("w:body", NS)
            paragraphs = [] if body is None else [node for node in body if node.tag == f"{{{WORD_NS}}}p"]
            return cls._single_indexed_target(paragraphs, index, region.region_id)
        if set(locator) == {"table_index", "row_index", "cell_index"}:
            body = root.find("w:body", NS)
            tables = [] if body is None else [node for node in body if node.tag == f"{{{WORD_NS}}}tbl"]
            table = cls._single_indexed_target(
                tables, cls._non_negative_index(locator["table_index"], region.region_id), region.region_id,
            )
            row = cls._single_indexed_target(
                table.findall("w:tr", NS), cls._non_negative_index(locator["row_index"], region.region_id), region.region_id,
            )
            return cls._single_indexed_target(
                row.findall("w:tc", NS), cls._non_negative_index(locator["cell_index"], region.region_id), region.region_id,
            )
        if set(locator) == {"content_control_tag"}:
            return cls._content_control(root, "w:tag", locator["content_control_tag"], region.region_id)
        if set(locator) == {"content_control_id"}:
            return cls._content_control(root, "w:id", locator["content_control_id"], region.region_id)
        raise ValueError(f"DOCX region has an unsupported explicit locator: {region.region_id}")

    @staticmethod
    def _non_negative_index(value: Any, region_id: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"DOCX region has an invalid locator index: {region_id}")
        try:
            index = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"DOCX region has an invalid locator index: {region_id}") from exc
        if index < 0:
            raise ValueError(f"DOCX region has an invalid locator index: {region_id}")
        return index

    @staticmethod
    def _single_indexed_target(nodes: list[ET.Element], index: int, region_id: str) -> ET.Element:
        try:
            return nodes[index]
        except IndexError as exc:
            raise ValueError(f"DOCX locator does not exist in template: {region_id}") from exc

    @staticmethod
    def _content_control(root: ET.Element, property_tag: str, value: Any, region_id: str) -> ET.Element:
        matches = []
        for control in root.findall(".//w:sdt", NS):
            properties = control.find("w:sdtPr", NS)
            property_node = properties.find(property_tag, NS) if properties is not None else None
            if property_node is not None and property_node.attrib.get(f"{{{WORD_NS}}}val") == str(value):
                matches.append(control)
        if len(matches) != 1:
            raise ValueError(f"DOCX content-control locator is not unique in template: {region_id}")
        return matches[0]

    @staticmethod
    def _text_ranges(source: bytes) -> list[tuple[int, int, int, int]]:
        """Return raw byte ranges for each ``w:t`` element in document order."""

        parser = expat.ParserCreate(namespace_separator="|")
        text_name = f"{WORD_NS}|t"
        records: list[list[int | None]] = []
        stack: list[list[int | None]] = []

        def start_element(name: str, _attrs: dict[str, str]) -> None:
            if name != text_name:
                return
            start = parser.CurrentByteIndex
            start_end = DocxRenderer._tag_end(source, start)
            if source[start_end - 1:start_end] == b"/":
                raise ValueError("DOCX renderer will not expand a self-closing text node")
            record: list[int | None] = [start, start_end + 1, None, None]
            records.append(record)
            stack.append(record)

        def end_element(name: str) -> None:
            if name != text_name:
                return
            if not stack:
                raise ValueError("DOCX text-node XML is malformed")
            record = stack.pop()
            record[2] = parser.CurrentByteIndex
            record[3] = DocxRenderer._tag_end(source, parser.CurrentByteIndex) + 1

        parser.StartElementHandler = start_element
        parser.EndElementHandler = end_element
        try:
            parser.Parse(source, True)
        except expat.ExpatError as exc:
            raise ValueError("DOCX document XML is malformed") from exc
        if stack or any(value is None for record in records for value in record):
            raise ValueError("DOCX text-node XML is malformed")
        return [tuple(value for value in record if value is not None) for record in records]

    @staticmethod
    def _tag_end(source: bytes, start: int) -> int:
        quote: int | None = None
        for index in range(start, len(source)):
            char = source[index]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in {ord('"'), ord("'")}:
                quote = char
            elif char == ord(">"):
                return index
        raise ValueError("DOCX XML contains an unterminated tag")

    @staticmethod
    def _replace_raw_text(
        source: bytes,
        text_ranges: list[tuple[int, int, int, int]],
        replacements: dict[int, str],
    ) -> bytes:
        patches: list[tuple[int, int, bytes]] = []
        for index, value in replacements.items():
            tag_start, content_start, content_end, _tag_end = text_ranges[index]
            encoded = escape(value).encode("utf-8")
            if value[:1].isspace() or value[-1:].isspace():
                start_tag = source[tag_start:content_start]
                if b"xml:space" not in start_tag:
                    start_tag = start_tag[:-1] + b' xml:space="preserve">'
                patches.append((tag_start, content_end, start_tag + encoded))
            else:
                patches.append((content_start, content_end, encoded))
        for start, end, replacement in sorted(patches, reverse=True):
            source = source[:start] + replacement + source[end:]
        return source

    @staticmethod
    def _integrity_manifest(
        before: TemplateSecurityReport,
        after: TemplateSecurityReport,
        changed_parts: set[str],
        policy: RendererPolicy,
    ) -> dict[str, Any]:
        before_names, after_names = set(before.parts), set(after.parts)
        changed = sorted(name for name in before_names & after_names if before.parts[name] != after.parts[name])
        policy_violations: list[str] = []
        if before_names != after_names:
            policy_violations.append("OOXML part set changed")
        if set(changed) != changed_parts:
            policy_violations.append("unexpected OOXML parts changed")
        if any(name != "word/document.xml" for name in changed):
            policy_violations.append("changed part outside DOCX allowlist")
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
            "manifest_hash": content_hash({"before": before.parts, "after": after.parts, "changed": changed}),
        }
