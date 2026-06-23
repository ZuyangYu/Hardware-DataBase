import os
import re
from dataclasses import dataclass


DOCS_GROUP = "文档资料"
MATERIAL_GROUP = "物料数据"
DESIGN_GROUP = "设计数据"
TEST_GROUP = "测试数据"
PROJECT_GROUP = "项目管理数据"
EXTERNAL_GROUP = "外部数据"
PEOPLE_GROUP = "人员与组织数据"
UNKNOWN_GROUP = "未分类"

SOURCE_GROUPS = (
    DESIGN_GROUP,
    MATERIAL_GROUP,
    DOCS_GROUP,
    TEST_GROUP,
    PROJECT_GROUP,
    EXTERNAL_GROUP,
    PEOPLE_GROUP,
    UNKNOWN_GROUP,
)

USER_SELECTABLE_SOURCE_GROUPS = (
    DESIGN_GROUP,
    MATERIAL_GROUP,
    DOCS_GROUP,
    TEST_GROUP,
    PROJECT_GROUP,
    EXTERNAL_GROUP,
    PEOPLE_GROUP,
)

SOURCE_GROUP_DESCRIPTIONS = {
    DESIGN_GROUP: "原理图、PCB、BOM、网表、约束、仿真文件",
    MATERIAL_GROUP: "器件参数、封装、供应商、生命周期、替代料信息",
    DOCS_GROUP: "Datasheet、规格书、手册、标准规范、应用笔记、参考资料",
    TEST_GROUP: "测试报告、测试记录、验证数据",
    PROJECT_GROUP: "计划、进度、任务、评审与会议资料",
    EXTERNAL_GROUP: "第三方资料、外部接口、客户或供应链输入",
    PEOPLE_GROUP: "人员、团队、角色、组织结构资料",
}

SOURCE_GROUP_DISPLAY_NAMES = {
    DOCS_GROUP: "规范手册资料",
}

IMPLEMENTED_PARSE_GROUPS = {DOCS_GROUP, MATERIAL_GROUP}


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


def display_source_group(group: str | None) -> str:
    safe_group = safe_source_group(group)
    return SOURCE_GROUP_DISPLAY_NAMES.get(safe_group, safe_group)
