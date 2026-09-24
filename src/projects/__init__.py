"""Project-first source governance domain.

The project package is intentionally independent from the individual RAG,
spreadsheet and circuit implementations.  It records the business identity and
approved scope of a source without forcing those pipelines into a new canonical
representation.
"""

from src.projects.access_service import ProjectAccessService
from src.projects.models import (
    BaselineItem,
    LogicalDocument,
    ProcessingArtifact,
    Project,
    ProjectBaseline,
    ProjectKnowledgeBinding,
    ProjectPrincipalBinding,
    ProjectSourceBinding,
    SourceAsset,
    SourceRegionPolicy,
    SourceSetSnapshot,
    SourceVersion,
)
from src.projects.service import ProjectService
from src.projects.store import ProjectStore
from src.projects.retrieval import ProjectEvidenceRetrievalService

__all__ = [
    "BaselineItem",
    "LogicalDocument",
    "ProcessingArtifact",
    "Project",
    "ProjectAccessService",
    "ProjectBaseline",
    "ProjectKnowledgeBinding",
    "ProjectPrincipalBinding",
    "ProjectService",
    "ProjectEvidenceRetrievalService",
    "ProjectSourceBinding",
    "ProjectStore",
    "SourceAsset",
    "SourceRegionPolicy",
    "SourceSetSnapshot",
    "SourceVersion",
]
