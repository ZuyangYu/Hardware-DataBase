"""Fail-closed sanitization for OOXML template packages."""

from __future__ import annotations

import copy
import posixpath
import xml.parsers.expat as expat
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, LargeZipFile, ZipFile, ZipInfo

SourceFormat = Literal["xlsx", "xlsm", "docx"]
SafeFormat = Literal["xlsx", "docx"]

_CONTENT_TYPES_PART = "[Content_Types].xml"
_ROOT_RELATIONSHIPS_PART = "_rels/.rels"
_CONTENT_TYPE_NAMESPACES = {
    "http://purl.oclc.org/ooxml/package/content-types",
    "http://schemas.openxmlformats.org/package/2006/content-types",
}
_PACKAGE_RELATIONSHIP_NAMESPACES = {
    "http://purl.oclc.org/ooxml/package/relationships",
    "http://schemas.openxmlformats.org/package/2006/relationships",
}
_RELATIONSHIP_NAMESPACES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
}
_OFFICE_DOCUMENT_RELATIONSHIP_TYPES = {
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
}
_OFFICE_DOCUMENT_NAMESPACES = {
    "http://purl.oclc.org/ooxml/spreadsheetml/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "urn:schemas-microsoft-com:office:office",
}
_ACTIVE_PATH_FRAGMENTS = (
    "/activex/",
    "/ctrlprops/",
    "/customui/",
    "/embeddings/",
    "/externallinks/",
    "/macrosheets/",
)
_ACTIVE_RELATIONSHIP_TYPES = {
    "activexcontrol",
    "activexcontrolbinary",
    "control",
    "ctrlprop",
    "customui",
    "externallink",
    "externallinkpath",
    "macrosheet",
    "oleobject",
    "vbadata",
    "vbaproject",
}
_ACTIVE_CONTENT_TYPE_MARKERS = (
    "activex",
    "controlproperties",
    "externallink",
    "macroenabled",
    "macrosheet",
    "oleobject",
    "vba",
    "visio",
)
_ACTIVE_XML_ELEMENTS = {
    "control",
    "controls",
    "externalreference",
    "externalreferences",
    "object",
    "oleobject",
    "oleobjects",
}
_MAIN_PARTS = {
    "xlsx": "xl/workbook.xml",
    "xlsm": "xl/workbook.xml",
    "docx": "word/document.xml",
}
_MAIN_ROOT_QNAMES = {
    "xlsx": {
        "{http://purl.oclc.org/ooxml/spreadsheetml/main}workbook",
        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook",
    },
    "xlsm": {
        "{http://purl.oclc.org/ooxml/spreadsheetml/main}workbook",
        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook",
    },
    "docx": {
        "{http://purl.oclc.org/ooxml/wordprocessingml/main}document",
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document",
    },
}
_SAFE_MAIN_CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
}
_MAX_COMPRESSED_PACKAGE_BYTES = 128 * 1024 * 1024
_MAX_ENTRIES = 10_000
_MAX_ENTRY_BYTES = 64 * 1024 * 1024
_MAX_UNCOMPRESSED_PACKAGE_BYTES = 256 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 500
_MIN_RATIO_CHECK_BYTES = 1024 * 1024
_MAX_XML_BYTES = 16 * 1024 * 1024
_MAX_XML_DEPTH = 256


class _ForbiddenXmlConstruct(Exception):
    pass


class TemplateSanitizationError(ValueError):
    """Raised when a package cannot be proven safe after sanitization."""


@dataclass(frozen=True)
class SanitizedTemplate:
    content: bytes
    format: SafeFormat
    removed_parts: list[str]
    removed_relationships: list[str]


@dataclass
class _PackageEntry:
    info: ZipInfo
    content: bytes


@dataclass
class _Package:
    entries: dict[str, _PackageEntry]
    comment: bytes


@dataclass(frozen=True)
class _Relationship:
    relationship_id: str
    relationship_type: str
    target: str
    external: bool


@dataclass
class _Removal:
    parts: set[str]
    relationship_ids: dict[str, set[str]]

    @property
    def relationships(self) -> set[str]:
        return {
            f"{part}#{relationship_id}"
            for part, relationship_ids in self.relationship_ids.items()
            for relationship_id in relationship_ids
        }


def sanitize_template(content: bytes, source_format: SourceFormat) -> SanitizedTemplate:
    """Remove active content from an OOXML package and validate the safe result."""
    if source_format not in _MAIN_PARTS:
        raise TemplateSanitizationError(f"unsupported source format: {source_format}")

    package = _read_package(content)
    _validate_package_structure(package, source_format)
    removal = _active_content_removal_set(package, source_format)
    retained = _remove_parts_and_relationships(package, removal)
    _remove_content_type_entries(retained, removal.parts)
    _remove_relationship_consumers(retained, removal.relationship_ids)
    safe_format = _target_format(source_format)
    _normalize_main_content_type(retained, safe_format)
    sanitized = _write_package(retained)
    _validate_sanitized_package(sanitized, safe_format)
    return SanitizedTemplate(
        content=sanitized,
        format=safe_format,
        removed_parts=sorted(removal.parts),
        removed_relationships=sorted(removal.relationships),
    )


def _target_format(source_format: SourceFormat) -> SafeFormat:
    return "docx" if source_format == "docx" else "xlsx"


def _read_package(content: bytes) -> _Package:
    if len(content) > _MAX_COMPRESSED_PACKAGE_BYTES:
        raise TemplateSanitizationError("ZIP resource limit exceeded: compressed package size")
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            _validate_zip_resource_limits(infos)
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise TemplateSanitizationError("malformed ZIP: duplicate package entries")
            entries: dict[str, _PackageEntry] = {}
            for info in infos:
                _validate_entry_name(info.filename)
                if info.is_dir():
                    entries[info.filename] = _PackageEntry(copy.copy(info), b"")
                    continue
                entries[info.filename] = _PackageEntry(
                    copy.copy(info),
                    archive.read(info),
                )
            return _Package(entries=entries, comment=archive.comment)
    except TemplateSanitizationError:
        raise
    except (BadZipFile, LargeZipFile, OSError, RuntimeError, ValueError) as exc:
        raise TemplateSanitizationError(f"malformed ZIP package: {exc}") from exc


def _validate_entry_name(name: str) -> None:
    path = PurePosixPath(name)
    comparable_name = name.rstrip("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or "//" in name
        or posixpath.normpath(comparable_name) != comparable_name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise TemplateSanitizationError(f"malformed ZIP entry path: {name!r}")


def _validate_zip_resource_limits(infos: list[ZipInfo]) -> None:
    if len(infos) > _MAX_ENTRIES:
        raise TemplateSanitizationError("ZIP resource limit exceeded: too many entries")
    total_size = 0
    for info in infos:
        if info.file_size > _MAX_ENTRY_BYTES:
            raise TemplateSanitizationError(
                f"ZIP resource limit exceeded: entry too large: {info.filename}",
            )
        total_size += info.file_size
        if total_size > _MAX_UNCOMPRESSED_PACKAGE_BYTES:
            raise TemplateSanitizationError(
                "ZIP resource limit exceeded: uncompressed package size",
            )
        if (
            info.file_size >= _MIN_RATIO_CHECK_BYTES
            and info.file_size / max(info.compress_size, 1) > _MAX_COMPRESSION_RATIO
        ):
            raise TemplateSanitizationError(
                f"ZIP resource limit exceeded: compression ratio: {info.filename}",
            )


def _parse_xml(content: bytes, part_name: str) -> ElementTree.Element:
    if len(content) > _MAX_XML_BYTES:
        raise TemplateSanitizationError(f"XML resource limit exceeded in {part_name}")
    _validate_xml_safety(content, part_name)
    try:
        return ElementTree.fromstring(content)
    except (ElementTree.ParseError, ValueError) as exc:
        raise TemplateSanitizationError(f"malformed XML in {part_name}: {exc}") from exc


def _validate_xml_safety(content: bytes, part_name: str) -> None:
    parser = expat.ParserCreate()
    depth = 0

    def reject_dtd_or_entity(*_args: object) -> None:
        raise _ForbiddenXmlConstruct

    def start_element(_name: str, _attributes: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > _MAX_XML_DEPTH:
            raise TemplateSanitizationError(
                f"XML resource limit exceeded in {part_name}: nesting depth",
            )

    def end_element(_name: str) -> None:
        nonlocal depth
        depth -= 1

    parser.StartDoctypeDeclHandler = reject_dtd_or_entity
    parser.EntityDeclHandler = reject_dtd_or_entity
    parser.UnparsedEntityDeclHandler = reject_dtd_or_entity
    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    try:
        parser.Parse(content, True)
    except _ForbiddenXmlConstruct as exc:
        raise TemplateSanitizationError(
            f"malformed XML in {part_name}: DTDs and entities are forbidden",
        ) from exc
    except TemplateSanitizationError:
        raise
    except expat.ExpatError as exc:
        raise TemplateSanitizationError(f"malformed XML in {part_name}: {exc}") from exc


def _validate_package_structure(package: _Package, source_format: SourceFormat | SafeFormat) -> None:
    required_parts = {
        _CONTENT_TYPES_PART,
        _ROOT_RELATIONSHIPS_PART,
        _MAIN_PARTS[source_format],
    }
    missing = sorted(required_parts - package.entries.keys())
    if missing:
        raise TemplateSanitizationError(f"missing package root: {', '.join(missing)}")

    content_types = _content_types(package)
    for part_name, entry in package.entries.items():
        if _part_is_xml(part_name, content_types):
            _parse_xml(entry.content, part_name)
    _validate_main_part(package, source_format)

    relationships_by_part = _relationships_by_part(package)
    main_part = _MAIN_PARTS[source_format]
    office_document_relationships = [
        relationship
        for relationship in relationships_by_part[_ROOT_RELATIONSHIPS_PART]
        if relationship.relationship_type in _OFFICE_DOCUMENT_RELATIONSHIP_TYPES
    ]
    if len(office_document_relationships) != 1:
        raise TemplateSanitizationError(
            f"package must contain exactly one package root relationship for {main_part}",
        )
    main_relationship = office_document_relationships[0]
    if (
        main_relationship.external
        or _resolve_relationship_target(
            _ROOT_RELATIONSHIPS_PART,
            main_relationship.target,
        )
        != main_part
    ):
        raise TemplateSanitizationError(f"invalid package root relationship for {main_part}")

    for relationships_part, relationships in relationships_by_part.items():
        owner = _owner_for_relationship_part(relationships_part)
        if owner is not None and owner not in package.entries:
            raise TemplateSanitizationError(
                f"dangling relationship part {relationships_part}: owner {owner} is missing",
            )
        for relationship in relationships:
            if relationship.external:
                continue
            target = _resolve_relationship_target(relationships_part, relationship.target)
            if target not in package.entries:
                raise TemplateSanitizationError(
                    f"dangling relationship {relationships_part}#{relationship.relationship_id}: "
                    f"target {target} is missing",
                )

    _validate_relationship_consumers(package, relationships_by_part, content_types)


def _validate_main_part(package: _Package, source_format: SourceFormat | SafeFormat) -> None:
    main_part = _MAIN_PARTS[source_format]
    root = _parse_xml(package.entries[main_part].content, main_part)
    if root.tag not in _MAIN_ROOT_QNAMES[source_format]:
        raise TemplateSanitizationError(
            f"invalid main part root for {source_format}: {root.tag}",
        )


def _relationships_by_part(package: _Package) -> dict[str, list[_Relationship]]:
    return {
        part_name: _parse_relationships(entry.content, part_name)
        for part_name, entry in package.entries.items()
        if part_name.endswith(".rels")
    }


def _parse_relationships(content: bytes, part_name: str) -> list[_Relationship]:
    root = _parse_xml(content, part_name)
    relationship_namespace = _namespace(root.tag)
    if (
        _local_name(root.tag) != "Relationships"
        or relationship_namespace not in _PACKAGE_RELATIONSHIP_NAMESPACES
    ):
        raise TemplateSanitizationError(
            f"malformed XML in {part_name}: invalid relationships namespace or root",
        )

    relationships: list[_Relationship] = []
    seen_ids: set[str] = set()
    for element in root:
        if (
            _local_name(element.tag) != "Relationship"
            or _namespace(element.tag) != relationship_namespace
        ):
            raise TemplateSanitizationError(
                f"malformed XML in {part_name}: unexpected relationships element",
            )
        relationship_id = element.attrib.get("Id", "")
        relationship_type = element.attrib.get("Type", "")
        target = element.attrib.get("Target", "")
        if not relationship_id or not relationship_type or not target:
            raise TemplateSanitizationError(
                f"malformed XML in {part_name}: incomplete relationship",
            )
        if relationship_id in seen_ids:
            raise TemplateSanitizationError(
                f"malformed XML in {part_name}: duplicate relationship id {relationship_id}",
            )
        seen_ids.add(relationship_id)
        target_mode = element.attrib.get("TargetMode", "")
        if target_mode and target_mode.lower() != "external":
            raise TemplateSanitizationError(
                f"malformed XML in {part_name}: invalid TargetMode {target_mode}",
            )
        relationships.append(
            _Relationship(
                relationship_id=relationship_id,
                relationship_type=relationship_type,
                target=target,
                external=target_mode.lower() == "external",
            ),
        )
    return relationships


def _resolve_relationship_target(relationships_part: str, target: str) -> str:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        raise TemplateSanitizationError(
            f"dangling internal relationship in {relationships_part}: URI target requires External mode",
        )
    target_path = unquote(parsed.path)
    if not target_path or "\\" in target_path or "\x00" in target_path:
        raise TemplateSanitizationError(
            f"dangling relationship in {relationships_part}: invalid target {target!r}",
        )
    owner = _owner_for_relationship_part(relationships_part)
    base = "" if owner is None else posixpath.dirname(owner)
    if target_path.startswith("/"):
        resolved = posixpath.normpath(target_path.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(base, target_path))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise TemplateSanitizationError(
            f"dangling relationship in {relationships_part}: target escapes package",
        )
    return resolved


def _owner_for_relationship_part(part_name: str) -> str | None:
    if part_name == _ROOT_RELATIONSHIPS_PART:
        return None
    directory, separator, filename = part_name.rpartition("/_rels/")
    if not separator or not filename.endswith(".rels"):
        raise TemplateSanitizationError(f"malformed relationship part path: {part_name}")
    owner_filename = filename.removesuffix(".rels")
    return f"{directory}/{owner_filename}" if directory else owner_filename


def _relationship_part_for_owner(owner: str) -> str:
    directory = posixpath.dirname(owner)
    filename = posixpath.basename(owner)
    return f"{directory}/_rels/{filename}.rels" if directory else f"_rels/{filename}.rels"


def _active_content_removal_set(package: _Package, source_format: SourceFormat) -> _Removal:
    content_types = _content_types(package)
    main_part = _MAIN_PARTS[source_format]
    relationships_by_part = _relationships_by_part(package)
    active_vml_parts = _active_vml_parts(package, content_types, relationships_by_part)
    parts = {
        part_name
        for part_name in package.entries
        if part_name not in {_CONTENT_TYPES_PART, _ROOT_RELATIONSHIPS_PART, main_part}
        and (
            _is_active_part_name(part_name)
            or _is_active_content_type(content_types.get(part_name, ""))
            or part_name in active_vml_parts
        )
    }
    relationship_ids: dict[str, set[str]] = {}

    changed = True
    while changed:
        changed = False
        for relationships_part, relationships in relationships_by_part.items():
            owner = _owner_for_relationship_part(relationships_part)
            if owner is not None and owner in parts and relationships_part not in parts:
                parts.add(relationships_part)
                changed = True
            if relationships_part in parts:
                ids = relationship_ids.setdefault(relationships_part, set())
                before = len(ids)
                ids.update(relationship.relationship_id for relationship in relationships)
                changed |= len(ids) != before
                continue

            for relationship in relationships:
                target = (
                    None
                    if relationship.external
                    else _resolve_relationship_target(relationships_part, relationship.target)
                )
                remove_relationship = (
                    relationship.external
                    or _is_active_relationship_type(relationship.relationship_type)
                    or target in parts
                )
                if not remove_relationship:
                    continue
                ids = relationship_ids.setdefault(relationships_part, set())
                if relationship.relationship_id not in ids:
                    ids.add(relationship.relationship_id)
                    changed = True
                if (
                    target is not None
                    and _is_active_relationship_type(relationship.relationship_type)
                    and target not in parts
                ):
                    parts.add(target)
                    changed = True

        for part_name in tuple(parts):
            relationship_part = _relationship_part_for_owner(part_name)
            if relationship_part in package.entries and relationship_part not in parts:
                parts.add(relationship_part)
                changed = True

    return _Removal(parts=parts, relationship_ids=relationship_ids)


def _content_types(package: _Package) -> dict[str, str]:
    root = _parse_xml(package.entries[_CONTENT_TYPES_PART].content, _CONTENT_TYPES_PART)
    content_type_namespace = _namespace(root.tag)
    if (
        _local_name(root.tag) != "Types"
        or content_type_namespace not in _CONTENT_TYPE_NAMESPACES
    ):
        raise TemplateSanitizationError(
            "malformed XML in [Content_Types].xml: invalid content type namespace or root",
        )

    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for element in root:
        if _namespace(element.tag) != content_type_namespace:
            raise TemplateSanitizationError(
                "malformed XML in [Content_Types].xml: invalid element namespace",
            )
        local_name = _local_name(element.tag).lower()
        content_type = element.attrib.get("ContentType", "")
        if local_name == "default":
            extension = element.attrib.get("Extension", "").lower()
            if not extension or not content_type or extension in defaults:
                raise TemplateSanitizationError("malformed XML in [Content_Types].xml: invalid Default")
            defaults[extension] = content_type
        elif local_name == "override":
            part_name = _normalize_content_type_part_name(element.attrib.get("PartName", ""))
            if not content_type or part_name in overrides:
                raise TemplateSanitizationError("malformed XML in [Content_Types].xml: invalid Override")
            overrides[part_name] = content_type
        else:
            raise TemplateSanitizationError(
                "malformed XML in [Content_Types].xml: unexpected element",
            )

    missing_overrides = sorted(overrides.keys() - package.entries.keys())
    if missing_overrides:
        raise TemplateSanitizationError(
            f"dangling content type override: {', '.join(missing_overrides)}",
        )

    result = dict(overrides)
    for part_name in package.entries:
        if part_name in result or part_name.endswith("/") or "." not in part_name:
            continue
        extension = part_name.rsplit(".", 1)[1].lower()
        if extension in defaults:
            result[part_name] = defaults[extension]
    uncovered_parts = sorted(
        part_name
        for part_name in package.entries
        if part_name != _CONTENT_TYPES_PART
        and not part_name.endswith("/")
        and part_name not in result
    )
    if uncovered_parts:
        raise TemplateSanitizationError(
            f"missing content type for package part: {', '.join(uncovered_parts)}",
        )
    return result


def _normalize_content_type_part_name(part_name: str) -> str:
    decoded = unquote(part_name)
    relative_name = decoded.removeprefix("/")
    normalized = posixpath.normpath(relative_name)
    if (
        not part_name.startswith("/")
        or decoded.count("/") != part_name.count("/")
        or "\\" in decoded
        or "?" in part_name
        or "#" in part_name
        or "//" in decoded
        or any(segment in {"", ".", ".."} for segment in relative_name.split("/"))
        or not normalized
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or normalized != relative_name
    ):
        raise TemplateSanitizationError(
            f"malformed XML in [Content_Types].xml: invalid PartName {part_name!r}",
        )
    return normalized


def _is_active_part_name(part_name: str) -> bool:
    lowered = f"/{part_name.lower()}"
    return (
        any(fragment in lowered for fragment in _ACTIVE_PATH_FRAGMENTS)
        or lowered.endswith("/vbaproject.bin")
        or lowered.endswith("/vbaprojectsignature.bin")
        or lowered.endswith("/vbadata.xml")
    )


def _is_active_content_type(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(marker in lowered for marker in _ACTIVE_CONTENT_TYPE_MARKERS)


def _is_active_relationship_type(relationship_type: str) -> bool:
    return _relationship_type_name(relationship_type) in _ACTIVE_RELATIONSHIP_TYPES


def _relationship_type_name(relationship_type: str) -> str:
    return relationship_type.rstrip("/").rsplit("/", 1)[-1].lower()


def _active_vml_parts(
    package: _Package,
    content_types: dict[str, str],
    relationships_by_part: dict[str, list[_Relationship]],
) -> set[str]:
    candidates = {
        part_name
        for part_name in package.entries
        if part_name.lower().endswith(".vml")
        or "vmldrawing" in content_types.get(part_name, "").lower()
    }
    for relationships_part, relationships in relationships_by_part.items():
        candidates.update(
            _resolve_relationship_target(relationships_part, relationship.target)
            for relationship in relationships
            if not relationship.external
            and _relationship_type_name(relationship.relationship_type) == "vmldrawing"
        )
    return {
        part_name
        for part_name in candidates
        if _xml_part_contains_active_vml(part_name, package.entries[part_name].content)
    }


def _xml_part_contains_active_vml(part_name: str, content: bytes) -> bool:
    root = _parse_xml(content, part_name)
    for element in root.iter():
        local_name = _local_name(element.tag).lower()
        if local_name == "oleobject":
            return True
        if local_name == "clientdata" and element.attrib.get("ObjectType", "").lower() != "note":
            return True
    return False


def _remove_parts_and_relationships(package: _Package, removal: _Removal) -> _Package:
    entries = {
        part_name: _PackageEntry(copy.copy(entry.info), entry.content)
        for part_name, entry in package.entries.items()
        if part_name not in removal.parts
    }
    retained = _Package(entries=entries, comment=package.comment)
    for relationships_part, relationship_ids in removal.relationship_ids.items():
        if relationships_part not in retained.entries or not relationship_ids:
            continue
        entry = retained.entries[relationships_part]
        root = _parse_xml(entry.content, relationships_part)
        for element in list(root):
            if element.attrib.get("Id") in relationship_ids:
                root.remove(element)
        entry.content = _serialize_xml(root)
    return retained


def _remove_relationship_consumers(
    package: _Package,
    removed_relationship_ids: dict[str, set[str]],
) -> None:
    content_types = _content_types(package)
    for relationships_part, relationship_ids in removed_relationship_ids.items():
        owner = _owner_for_relationship_part(relationships_part)
        if (
            owner is None
            or owner not in package.entries
            or not _part_is_xml(owner, content_types)
            or not relationship_ids
        ):
            continue
        entry = package.entries[owner]
        root = _parse_xml(entry.content, owner)
        if _remove_consumer_elements(root, relationship_ids):
            entry.content = _serialize_xml(root)


def _remove_consumer_elements(root: ElementTree.Element, relationship_ids: set[str]) -> bool:
    changed = False
    for parent in root.iter():
        for child in list(parent):
            if _element_references_relationship(child, relationship_ids):
                parent.remove(child)
                changed = True

    pruned = True
    while pruned:
        pruned = False
        for parent in root.iter():
            for child in list(parent):
                if (
                    len(child) == 0
                    and not child.attrib
                    and not (child.text or "").strip()
                    and _local_name(child.tag).lower() in {
                        "controls",
                        "externalreferences",
                        "hyperlinks",
                        "object",
                        "oleobjects",
                    }
                ):
                    parent.remove(child)
                    pruned = True
                    changed = True
    return changed


def _element_references_relationship(
    element: ElementTree.Element,
    relationship_ids: set[str],
) -> bool:
    return any(
        value in relationship_ids
        and _namespace(attribute) in _RELATIONSHIP_NAMESPACES
        for attribute, value in element.attrib.items()
    )


def _remove_content_type_entries(package: _Package, removed_parts: set[str]) -> None:
    entry = package.entries[_CONTENT_TYPES_PART]
    root = _parse_xml(entry.content, _CONTENT_TYPES_PART)
    changed = False
    for element in list(root):
        local_name = _local_name(element.tag).lower()
        if local_name != "override":
            continue
        part_name = _normalize_content_type_part_name(element.attrib.get("PartName", ""))
        if part_name in removed_parts:
            root.remove(element)
            changed = True

    retained_extensions = {
        part_name.rsplit(".", 1)[1].lower()
        for part_name in package.entries
        if "." in part_name and not part_name.endswith("/")
    }
    removed_extensions = {
        part_name.rsplit(".", 1)[1].lower()
        for part_name in removed_parts
        if "." in part_name and not part_name.endswith("/")
    }
    for element in list(root):
        if _local_name(element.tag).lower() != "default":
            continue
        extension = element.attrib.get("Extension", "").lower()
        content_type = element.attrib.get("ContentType", "")
        if extension not in retained_extensions and (
            extension in removed_extensions or _is_active_content_type(content_type)
        ):
            root.remove(element)
            changed = True

    if changed:
        entry.content = _serialize_xml(root)


def _normalize_main_content_type(package: _Package, safe_format: SafeFormat) -> None:
    entry = package.entries[_CONTENT_TYPES_PART]
    root = _parse_xml(entry.content, _CONTENT_TYPES_PART)
    main_part = _MAIN_PARTS[safe_format]
    safe_content_type = _SAFE_MAIN_CONTENT_TYPES[safe_format]
    found = False
    changed = False
    for element in root:
        if _local_name(element.tag).lower() != "override":
            continue
        part_name = _normalize_content_type_part_name(element.attrib.get("PartName", ""))
        if part_name != main_part:
            continue
        found = True
        if element.attrib.get("ContentType") != safe_content_type:
            element.set("ContentType", safe_content_type)
            changed = True
    if not found:
        raise TemplateSanitizationError(f"missing package root content type: {main_part}")
    if changed:
        entry.content = _serialize_xml(root)


def _write_package(package: _Package) -> bytes:
    output = BytesIO()
    try:
        with ZipFile(output, "w") as archive:
            for entry in package.entries.values():
                archive.writestr(copy.copy(entry.info), entry.content)
            archive.comment = package.comment
    except (BadZipFile, LargeZipFile, OSError, RuntimeError, ValueError) as exc:
        raise TemplateSanitizationError(f"could not write sanitized ZIP package: {exc}") from exc
    return output.getvalue()


def _validate_sanitized_package(content: bytes, safe_format: SafeFormat) -> None:
    package = _read_package(content)
    _validate_package_structure(package, safe_format)
    content_types = _content_types(package)
    relationships_by_part = _relationships_by_part(package)
    active_vml_parts = _active_vml_parts(package, content_types, relationships_by_part)
    for part_name, entry in package.entries.items():
        if _is_active_part_name(part_name) or _is_active_content_type(content_types.get(part_name, "")):
            raise TemplateSanitizationError(f"residual active content part: {part_name}")
        if part_name in active_vml_parts:
            raise TemplateSanitizationError(f"residual active VML content: {part_name}")
        if _part_is_xml(part_name, content_types) and not part_name.endswith(".rels"):
            _validate_no_active_xml_elements(entry.content, part_name)

    for relationships_part, relationships in relationships_by_part.items():
        for relationship in relationships:
            if relationship.external:
                raise TemplateSanitizationError(
                    f"residual active external relationship: "
                    f"{relationships_part}#{relationship.relationship_id}",
                )
            if _is_active_relationship_type(relationship.relationship_type):
                raise TemplateSanitizationError(
                    f"residual active relationship: "
                    f"{relationships_part}#{relationship.relationship_id}",
                )


def _validate_no_active_xml_elements(content: bytes, part_name: str) -> None:
    root = _parse_xml(content, part_name)
    for element in root.iter():
        if (
            _local_name(element.tag).lower() in _ACTIVE_XML_ELEMENTS
            and _namespace(element.tag) in _OFFICE_DOCUMENT_NAMESPACES
        ):
            raise TemplateSanitizationError(f"residual active XML content in {part_name}")


def _validate_relationship_consumers(
    package: _Package,
    relationships_by_part: dict[str, list[_Relationship]],
    content_types: dict[str, str],
) -> None:
    for part_name, entry in package.entries.items():
        if not _part_is_xml(part_name, content_types) or part_name.endswith(".rels"):
            continue
        root = _parse_xml(entry.content, part_name)
        referenced_ids = {
            value
            for element in root.iter()
            for attribute, value in element.attrib.items()
            if _namespace(attribute) in _RELATIONSHIP_NAMESPACES
        }
        if not referenced_ids:
            continue
        relationship_part = _relationship_part_for_owner(part_name)
        available_ids = {
            relationship.relationship_id
            for relationship in relationships_by_part.get(relationship_part, [])
        }
        dangling_ids = sorted(referenced_ids - available_ids)
        if dangling_ids:
            raise TemplateSanitizationError(
                f"dangling relationship reference in {part_name}: {', '.join(dangling_ids)}",
            )


def _is_xml_part(part_name: str) -> bool:
    lowered = part_name.lower()
    return (
        part_name == _CONTENT_TYPES_PART
        or lowered.endswith(".xml")
        or lowered.endswith(".rels")
        or lowered.endswith(".vml")
    )


def _part_is_xml(part_name: str, content_types: dict[str, str]) -> bool:
    content_type = content_types.get(part_name, "").lower()
    return (
        _is_xml_part(part_name)
        or content_type in {"application/xml", "text/xml"}
        or content_type.endswith("+xml")
    )


def _serialize_xml(root: ElementTree.Element) -> bytes:
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _namespace(name: str) -> str:
    return name[1:].split("}", 1)[0] if name.startswith("{") else ""
