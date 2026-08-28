import os
import shutil
from dataclasses import dataclass, field

import src.settings
from src.core.logger import log
from src.services.document_archive import DocumentArchiveManager
from src.services.spreadsheet_index_service import SpreadsheetIndexService


@dataclass
class PipelineAssetCleanupResult:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class PipelineAssetCleanupService:
    """Clean local assets owned by pipeline-based backends."""

    def __init__(
        self,
        archive: DocumentArchiveManager | None = None,
        spreadsheets: SpreadsheetIndexService | None = None,
        conversations=None,
    ):
        self.archive = archive or DocumentArchiveManager()
        self.spreadsheets = spreadsheets or SpreadsheetIndexService()
        if conversations is None:
            from src.external_conversations.store import ExternalConversationStore

            conversations = ExternalConversationStore()
        self.conversations = conversations

    def cleanup_knowledge_base(self, kb_name: str, department_id: str | int | None = None) -> PipelineAssetCleanupResult:
        result = PipelineAssetCleanupResult()
        if department_id not in (None, ""):
            self._remove_tree(
                self.archive.kb_path(kb_name, department_id=department_id),
                "department archive",
                result.errors,
            )
            self._remove_tree(
                self.spreadsheets.kb_index_path(department_id, kb_name, create=False),
                "spreadsheet index",
                result.errors,
            )
            try:
                conversation_scope = self.conversations.scope_dir(department_id, kb_name, create=False)
            except Exception:
                conversation_scope = None
            self._remove_tree(conversation_scope, "external conversations", result.errors)

        self._remove_tree(
            os.path.join(src.settings.PIPELINE_ARCHIVE_ROOT, kb_name),
            "legacy pipeline archive",
            result.errors,
        )
        legacy_ragflow_root = getattr(src.settings, "RAGFLOW_FILE_ROOT", "")
        if legacy_ragflow_root and os.path.abspath(legacy_ragflow_root) != os.path.abspath(src.settings.PIPELINE_ARCHIVE_ROOT):
            self._remove_tree(
                os.path.join(legacy_ragflow_root, kb_name),
                "legacy ragflow archive",
                result.errors,
            )
        return result

    def _remove_tree(self, path: str, label: str, errors: list[str]):
        if not os.path.exists(path):
            return
        try:
            shutil.rmtree(path)
            log(f"Deleted pipeline asset directory: {label}")
        except Exception as exc:
            errors.append(f"{label}: {exc}")
