import os
import re
from dataclasses import dataclass


DOCS_GROUP = "文档资料"
MATERIAL_GROUP = "物料数据"
UNKNOWN_GROUP = "未分类"

SOURCE_GROUPS = (DOCS_GROUP, MATERIAL_GROUP, UNKNOWN_GROUP)


@dataclass(frozen=True)
class SourceGroupClassification:
    group: str
    confidence: float
    reason: str


_MATERIAL_PATTERNS = [
    r"\bbom\b",
    r"bill[-_\s]?of[-_\s]?materials",
    r"\bavl\b",
    r"\bmpn\b",
    r"part[-_\s]?list",
    r"material",
    r"supplier",
    r"vendor",
    r"manufacturer",
    r"物料",
    r"料号",
    r"替代料",
    r"供应商",
    r"厂商",
]

_DOC_PATTERNS = [
    r"datasheet",
    r"data[-_\s]?sheet",
    r"manual",
    r"reference",
    r"errata",
    r"application[-_\s]?note",
    r"\ban\d+",
    r"spec",
    r"guide",
    r"文档",
    r"手册",
    r"规格书",
    r"数据手册",
    r"应用笔记",
]

_MATERIAL_EXTENSIONS = {".csv", ".xls", ".xlsx"}
_DOC_EXTENSIONS = {".pdf", ".txt", ".md", ".docx", ".html", ".htm"}


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def classify_source_group(filename: str) -> SourceGroupClassification:
    """Classify an uploaded file into the first two supported project data domains."""
    lowered = filename.lower()
    _, ext = os.path.splitext(lowered)

    if _matches_any(lowered, _MATERIAL_PATTERNS):
        return SourceGroupClassification(MATERIAL_GROUP, 0.9, "filename matched material/BOM keywords")

    if _matches_any(lowered, _DOC_PATTERNS):
        return SourceGroupClassification(DOCS_GROUP, 0.85, "filename matched document keywords")

    if ext in _MATERIAL_EXTENSIONS:
        return SourceGroupClassification(MATERIAL_GROUP, 0.7, "spreadsheet-like extension")

    if ext in _DOC_EXTENSIONS:
        return SourceGroupClassification(DOCS_GROUP, 0.6, "document-like extension")

    return SourceGroupClassification(UNKNOWN_GROUP, 0.2, "no source-group rule matched")


def safe_source_group(group: str | None) -> str:
    return group if group in SOURCE_GROUPS else UNKNOWN_GROUP
