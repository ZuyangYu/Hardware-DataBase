"""Versioned, format-neutral planning contracts for document authoring.

The planning package is intentionally independent from the current execution
graph.  It contains the values that a planner may persist and the validation
needed before a proposal can be confirmed.
"""

from .models import (
    ArtifactSpec,
    CoverageContract,
    CoverageRequirement,
    DependencyEdge,
    DeliverableSpec,
    DocumentPlan,
    GeneratedStructureSource,
    LayoutContract,
    LayoutSource,
    OutlineUnitSpec,
    OutputSpec,
    PlanDiff,
    PlanIssue,
    ProvidedTemplateSource,
    SemanticUnitPlan,
    SourceScopeSpec,
    StructureContract,
    SystemRecipeSource,
    TableRequirement,
    TemplateContract,
    UnitTaskSpec,
    planning_content_hash,
)
from .registry import (
    CapabilityRegistries,
    DomainStrategy,
    DomainStrategyDescriptor,
    DomainStrategyRegistry,
    LayoutAdapter,
    LayoutAdapterDescriptor,
    LayoutAdapterRegistry,
    RendererCapability,
    RendererCapabilityDescriptor,
    RendererCapabilityRegistry,
    build_builtin_registries,
)
from .sources import FrozenSourceRef, FrozenSourceScope, SourceType
from .diff import diff_document_plans
from .legacy import legacy_brief_to_output_spec
from .service import DocumentPlanningService, LegacyTemplatePlanningAdapter

__all__ = [
    "ArtifactSpec",
    "CoverageContract",
    "CoverageRequirement",
    "DependencyEdge",
    "DeliverableSpec",
    "DocumentPlan",
    "GeneratedStructureSource",
    "LayoutContract",
    "LayoutSource",
    "OutlineUnitSpec",
    "OutputSpec",
    "PlanDiff",
    "PlanIssue",
    "ProvidedTemplateSource",
    "SemanticUnitPlan",
    "SourceScopeSpec",
    "StructureContract",
    "SystemRecipeSource",
    "TableRequirement",
    "TemplateContract",
    "UnitTaskSpec",
    "planning_content_hash",
    "CapabilityRegistries",
    "DomainStrategy",
    "DomainStrategyDescriptor",
    "DomainStrategyRegistry",
    "FrozenSourceRef",
    "FrozenSourceScope",
    "LayoutAdapter",
    "LayoutAdapterDescriptor",
    "LayoutAdapterRegistry",
    "RendererCapability",
    "RendererCapabilityDescriptor",
    "RendererCapabilityRegistry",
    "SourceType",
    "build_builtin_registries",
    "diff_document_plans",
    "DocumentPlanningService",
    "LegacyTemplatePlanningAdapter",
    "legacy_brief_to_output_spec",
]
