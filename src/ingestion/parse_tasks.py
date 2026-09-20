"""Parse-task schema shared with the document/RAGFlow backend.

The former in-process ``ParseTaskManager`` (Streamlit-era JSON task runner) was
removed; background parsing is owned by the durable worker and the
``pipeline_documents`` ledger. Only the ``ParseTask`` value object remains,
because ``RAGFlowBackend`` still carries it across its adapter boundary.
"""

import time
from dataclasses import dataclass, field

from src.pipelines.document_rag.schemas import TASK_STATUS_QUEUED


@dataclass
class ParseTask:
    id: str
    kb_name: str
    source_path: str
    original_name: str
    source_group: str
    created_by: str = ""
    status: str = TASK_STATUS_QUEUED
    progress: int = 0
    stage: str = "等待解析"
    message: str = ""
    result: str = ""
    document_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
