"""Extract embedded OOXML child documents from container workbooks/documents.

P1-1: a parent ``.xlsx``/``.docx`` may embed real Word/Excel payloads under
``xl/embeddings/`` / ``word/embeddings/``. ``container_inspector`` only counts
them; this module turns each payload into a routable child file with a stable,
human-readable name so the normal ingestion pipelines can index it
(docx -> RAGFlow, xlsx -> spreadsheet table pipeline).

Identity is content based (SHA-256), never name based: embedded entries are
named ``Microsoft_Word_DocumentN.docx`` and numbering restarts in every parent.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field

from src.ingestion.container_inspector import EMBEDDED_PREFIXES

EMBEDDED_CHILD_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx"})
EMBEDDED_TAG = "[嵌入]"
TITLE_MAX_CHARS = 60
NAME_HASH_SUFFIX_CHARS = 6
_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_PARAGRAPH_RE = re.compile(r"<w:p[ >].*?</w:p>", re.DOTALL)
_TEXT_RUN_RE = re.compile(r"<w:t[^>]*>([^<]*)</w:t>")


@dataclass
class EmbeddedObject:
    entry_name: str
    original_name: str
    extension: str
    data: bytes
    content_hash: str
    title: str = ""

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class EmbeddedExtraction:
    objects: list[EmbeddedObject] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def has_objects(self) -> bool:
        return bool(self.objects)


def _clean_text(value: str) -> str:
    text = _INVALID_NAME_CHARS.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _docx_first_paragraph(data: bytes) -> str:
    """Return the first non-empty paragraph text of an embedded docx payload.

    Word splits a heading across multiple ``w:t`` runs (``5.`` + ``3`` + space +
    ``摄像头模块``), so runs are joined per paragraph before cleaning. The
    payload's ``docProps/app.xml`` titles are empty in the corpus we verified,
    hence the first paragraph is the reliable display-name source.
    """

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document = archive.read("word/document.xml").decode("utf-8", "ignore")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""
    for paragraph in _PARAGRAPH_RE.findall(document):
        text = _clean_text("".join(_TEXT_RUN_RE.findall(paragraph)))
        if text:
            return text[:TITLE_MAX_CHARS]
    return ""


def _payload_title(extension: str, data: bytes, original_name: str) -> str:
    if extension == ".docx":
        return _docx_first_paragraph(data)
    return ""


def _child_extension(original_name: str) -> str:
    return os.path.splitext(original_name)[1].lower()


def extract_embedded_objects(file_path: str) -> EmbeddedExtraction:
    """Read extractable OOXML payloads from a container file.

    Non-OOXML payloads (for example OLE ``.bin`` objects) cannot be routed to
    the document or spreadsheet pipelines and are reported in ``skipped`` while
    the inspector keeps counting them.
    """

    extraction = EmbeddedExtraction()
    if not zipfile.is_zipfile(file_path):
        return extraction
    try:
        with zipfile.ZipFile(file_path) as archive:
            entries = [
                name
                for name in archive.namelist()
                if name.startswith(EMBEDDED_PREFIXES) and not name.endswith("/")
            ]
            for entry_name in entries:
                original_name = os.path.basename(entry_name)
                extension = _child_extension(original_name)
                if extension not in EMBEDDED_CHILD_EXTENSIONS:
                    extraction.skipped.append(entry_name)
                    continue
                try:
                    data = archive.read(entry_name)
                except (KeyError, OSError, zipfile.BadZipFile):
                    extraction.skipped.append(entry_name)
                    continue
                if not zipfile.is_zipfile(io.BytesIO(data)):
                    extraction.skipped.append(entry_name)
                    continue
                extraction.objects.append(
                    EmbeddedObject(
                        entry_name=entry_name,
                        original_name=original_name,
                        extension=extension,
                        data=data,
                        content_hash=hashlib.sha256(data).hexdigest(),
                        title=_payload_title(extension, data, original_name),
                    )
                )
    except (OSError, zipfile.BadZipFile):
        return extraction
    return extraction


def build_child_name(
    parent_name: str,
    embedded: EmbeddedObject,
    used_names: set[str],
) -> str:
    """Derive a unique, human-readable child name: ``父名 [嵌入] 标题.ext``.

    The original embedded file name is generic and repeats across parents, so it
    is only a fallback when no title can be recovered. A short content hash is
    appended only when the same parent yields two different payloads with the
    same visible name.
    """

    parent_stem = _clean_text(os.path.splitext(parent_name)[0]) or "文档"
    fallback = _clean_text(os.path.splitext(embedded.original_name)[0]) or "嵌入对象"
    title = _clean_text(embedded.title) or fallback
    name = f"{parent_stem} {EMBEDDED_TAG} {title}{embedded.extension}"
    if name in used_names:
        suffix = embedded.content_hash[:NAME_HASH_SUFFIX_CHARS]
        name = f"{parent_stem} {EMBEDDED_TAG} {title} #{suffix}{embedded.extension}"
    used_names.add(name)
    return name


def write_embedded_child(embedded: EmbeddedObject, child_name: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, child_name)
    with open(out_path, "wb") as handle:
        handle.write(embedded.data)
    return out_path
