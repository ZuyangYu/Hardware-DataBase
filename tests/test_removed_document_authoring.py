"""Guards for the removed document-authoring feature.

The feature was deleted end to end (backend package, API surface, agent tools,
worker role, storage tables, frontend UI).  These assertions keep it from
silently growing back through a partial revert or a stale import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import src.settings as settings
from src.api.schemas import CreateTurnRequest


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOKENS = (
    "document_authoring",
    "document_generation",
    "DocumentGeneration",
    "DocumentAuthoring",
    "DocumentContext",
    "document_flow",
    "AGENT_DOCUMENT_TOOLS",
    "DOCUMENT_AUTHORING",
)


def test_document_authoring_package_is_not_importable() -> None:
    assert importlib.util.find_spec("src.document_authoring") is None


def test_create_turn_contract_has_no_document_fields() -> None:
    fields = CreateTurnRequest.model_fields
    assert "document_context" not in fields
    assert "document_flow" not in fields


def test_removed_settings_are_absent() -> None:
    for name in (
        "AGENT_DOCUMENT_TOOLS_ENABLED",
        "DOCUMENT_AUTHORING_AGENT_MODE_ENABLED",
        "DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND",
        "DOCUMENT_TASK_WRITE_ENABLED",
        "DOCUMENT_PLANNING_V2_ENABLED",
    ):
        assert not hasattr(settings, name), name


def test_source_tree_has_no_removed_feature_references() -> None:
    offenders: list[str] = []
    for base in (ROOT / "src", ROOT / "frontend" / "src"):
        for path in base.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for token in FORBIDDEN_TOKENS:
                if token in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert offenders == []
