import os
import re

import config.settings


_KB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class InvalidKnowledgeBaseName(ValueError):
    pass


def validate_kb_name(kb_name: str) -> str:
    name = str(kb_name or "").strip()
    if not name:
        raise InvalidKnowledgeBaseName("Knowledge base name cannot be empty.")
    if name in {".", ".."} or not _KB_NAME_RE.fullmatch(name):
        raise InvalidKnowledgeBaseName(
            "Knowledge base name may only contain letters, numbers, underscores, hyphens and dots."
        )
    if ".." in name:
        raise InvalidKnowledgeBaseName("Knowledge base name cannot contain consecutive dots.")
    return name


def safe_child_path(root: str, *parts: str, create: bool = False) -> str:
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(os.path.join(root_abs, *parts))
    if os.path.commonpath([root_abs, path_abs]) != root_abs:
        raise InvalidKnowledgeBaseName("Resolved path escapes the configured storage root.")
    if create:
        os.makedirs(path_abs, exist_ok=True)
    return path_abs


def get_kb_data_path(kb_name: str, create: bool = False) -> str:
    return safe_child_path(config.settings.DATA_ROOT, validate_kb_name(kb_name), create=create)


def get_kb_index_storage_path(kb_name: str, create: bool = True) -> str:
    return safe_child_path(config.settings.STORAGE_DIR, "index_stores", validate_kb_name(kb_name), create=create)
