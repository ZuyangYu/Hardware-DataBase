"""Bounded, evidence-first document authoring services (P2a)."""

from src.document_authoring.models import (
    DeterministicRuleSpec,
    DocumentArtifact,
    DocumentUnitDraft,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    ReviewItemSchema,
    TemplateUnitBinding,
    TemplateVersion,
    WorkbookRegionSchema,
)
from src.document_authoring.template_analysis import (
    DocxRegionSchema,
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.work_order_store import DocumentAuthoringStore

__all__ = [
    "DeterministicRuleSpec",
    "DocumentArtifact",
    "DocumentUnitDraft",
    "DocumentAuthoringStore",
    "DocumentGenerationService",
    "DocumentSchema",
    "DocumentWorkOrder",
    "DocxRegionSchema",
    "HarnessPolicy",
    "ReviewItemSchema",
    "TemplateUnitBinding",
    "TemplateAnalysis",
    "TemplateAnalysisSuggestion",
    "TemplateAnalysisUnit",
    "TemplateVersion",
    "WorkbookRegionSchema",
]
