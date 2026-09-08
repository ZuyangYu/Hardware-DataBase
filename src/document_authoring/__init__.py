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
from src.document_authoring.evidence import (
    AttachmentEvidenceProvider,
    CompositeDocumentEvidenceProvider,
    DocumentEvidenceProvider,
    KnowledgeBaseEvidenceProvider,
)
from src.document_authoring.conversion import (
    TemplateArtifactConversionService,
    TemplateConversionError,
    TemplateConversionResult,
)
from src.document_authoring.tasks import DocumentTask, DocumentTaskService, DocumentTaskStore
from src.document_authoring.reviews import DocumentReview, DocumentReviewStore
from src.document_authoring.revisions import (
    ArtifactRevision,
    ArtifactRevisionStore,
    DocumentRevisionService,
)
from src.document_authoring.requirement_resolver import (
    EvidenceCoverage,
    RequirementResolutionResult,
    RequirementResolver,
    UnresolvedRequirement,
)
from src.document_authoring.document_model import (
    Citation,
    CrossReferenceBlock,
    DocumentModel,
    ListBlock,
    MissingItem,
    ParagraphBlock,
    SectionBlock,
    TypedTableBlock,
)
from src.document_authoring.render_bindings import (
    LegacyFillPlanAdapter,
    RenderBinding,
    RenderBindingResolver,
    RenderBindingSet,
    legacy_fill_plan_from_model,
)

__all__ = [
    "DeterministicRuleSpec",
    "DocumentArtifact",
    "DocumentUnitDraft",
    "DocumentAuthoringStore",
    "DocumentEvidenceProvider",
    "DocumentGenerationService",
    "DocumentSchema",
    "DocumentWorkOrder",
    "AttachmentEvidenceProvider",
    "CompositeDocumentEvidenceProvider",
    "DocxRegionSchema",
    "HarnessPolicy",
    "ReviewItemSchema",
    "TemplateUnitBinding",
    "TemplateAnalysis",
    "TemplateAnalysisSuggestion",
    "TemplateAnalysisUnit",
    "TemplateVersion",
    "WorkbookRegionSchema",
    "KnowledgeBaseEvidenceProvider",
    "TemplateArtifactConversionService",
    "TemplateConversionError",
    "TemplateConversionResult",
    "DocumentTask",
    "DocumentTaskService",
    "DocumentTaskStore",
    "DocumentReview",
    "DocumentReviewStore",
    "ArtifactRevision",
    "ArtifactRevisionStore",
    "DocumentRevisionService",
    "EvidenceCoverage",
    "RequirementResolutionResult",
    "RequirementResolver",
    "UnresolvedRequirement",
    "Citation",
    "CrossReferenceBlock",
    "DocumentModel",
    "ListBlock",
    "MissingItem",
    "ParagraphBlock",
    "SectionBlock",
    "TypedTableBlock",
    "LegacyFillPlanAdapter",
    "RenderBinding",
    "RenderBindingResolver",
    "RenderBindingSet",
    "legacy_fill_plan_from_model",
]
