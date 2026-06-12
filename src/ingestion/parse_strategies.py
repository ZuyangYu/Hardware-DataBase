from typing import Callable

from llama_index.core.schema import BaseNode

from src.ingestion.docling_parser import parse_file
from src.ingestion.source_groups import DOCS_GROUP, IMPLEMENTED_PARSE_GROUPS, MATERIAL_GROUP, safe_source_group


class ParseStrategyNotImplemented(NotImplementedError):
    pass


def parse_by_source_group(
    file_path: str,
    filename: str,
    kb_name: str,
    source_group: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> list[BaseNode]:
    group = safe_source_group(source_group)

    if group in {DOCS_GROUP, MATERIAL_GROUP}:
        return parse_file(file_path, filename, kb_name, source_group=group, progress_callback=progress_callback)

    implemented = "、".join(sorted(IMPLEMENTED_PARSE_GROUPS))
    raise ParseStrategyNotImplemented(
        f"{group} 的解析策略尚未接入，当前已支持: {implemented}"
    )
