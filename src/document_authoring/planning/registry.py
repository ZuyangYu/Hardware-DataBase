"""Exact-version capability registries used by the planning compiler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import Field

from .models import NonEmptyId, PlanIssue, PlanningModel


@runtime_checkable
class DomainStrategy(Protocol):
    strategy_id: str
    version: str

    def identify_requirements(self, output_spec: Any, source_catalog: Any) -> Any:
        ...

    def compile_semantic_units(self, output_spec: Any, layout_contract: Any) -> Any:
        ...


@runtime_checkable
class LayoutAdapter(Protocol):
    adapter_id: str
    version: str

    def compile_layout_contract(self, output_spec: Any, template_analysis: Any = None) -> Any:
        ...


@runtime_checkable
class RendererCapability(Protocol):
    renderer_id: str
    version: str


class DomainStrategyDescriptor(PlanningModel):
    strategy_id: NonEmptyId
    version: NonEmptyId
    supported_document_types: list[NonEmptyId] = Field(default_factory=list, max_length=256)
    status: str = Field(default="available", pattern="^(available|unavailable|disabled)$")
    unavailable_reason: str | None = Field(default=None, max_length=1_000)


class LayoutAdapterDescriptor(PlanningModel):
    adapter_id: NonEmptyId
    version: NonEmptyId
    supported_formats: list[NonEmptyId] = Field(default_factory=list, max_length=64)
    supported_modes: list[NonEmptyId] = Field(default_factory=list, max_length=16)
    status: str = Field(default="available", pattern="^(available|unavailable|disabled)$")
    unavailable_reason: str | None = Field(default=None, max_length=1_000)


class RendererCapabilityDescriptor(PlanningModel):
    renderer_id: NonEmptyId
    version: NonEmptyId
    formats: list[NonEmptyId] = Field(default_factory=list, max_length=64)
    status: str = Field(default="available", pattern="^(available|unavailable|disabled)$")
    unavailable_reason: str | None = Field(default=None, max_length=1_000)


DescriptorT = TypeVar("DescriptorT", DomainStrategyDescriptor, LayoutAdapterDescriptor, RendererCapabilityDescriptor)


class _VersionedRegistry(Generic[DescriptorT]):
    descriptor_kind: str = "capability"
    id_field: str = "id"

    def __init__(self):
        self._descriptors: dict[tuple[str, str], DescriptorT] = {}

    def register(self, descriptor: DescriptorT, implementation: Any = None) -> DescriptorT:
        identifier = str(getattr(descriptor, self.id_field)).strip()
        version = str(descriptor.version).strip()
        key = (identifier, version)
        if key in self._descriptors:
            raise ValueError(f"duplicate {self.descriptor_kind} registration: {identifier}@{version}")
        self._descriptors[key] = descriptor
        if implementation is not None:
            # Implementations are process-local and never serialized into plans.
            setattr(self, "_implementations", getattr(self, "_implementations", {}))
            self._implementations[key] = implementation
        return descriptor

    def lookup(self, identifier: str, version: str) -> DescriptorT | None:
        return self._descriptors.get((str(identifier).strip(), str(version).strip()))

    get = lookup

    def implementation(self, identifier: str, version: str) -> Any | None:
        return getattr(self, "_implementations", {}).get(
            (str(identifier).strip(), str(version).strip())
        )

    def resolve(self, identifier: str, version: str) -> DescriptorT | PlanIssue:
        normalized_id = str(identifier).strip()
        normalized_version = str(version).strip()
        descriptor = self.lookup(normalized_id, normalized_version)
        if descriptor is None:
            return PlanIssue(
                code="capability_missing",
                severity="error",
                message=f"{self.descriptor_kind} {normalized_id}@{normalized_version} is not registered",
                path=f"{self.descriptor_kind}.{normalized_id}",
            )
        if descriptor.status != "available":
            reason = descriptor.unavailable_reason or f"status={descriptor.status}"
            return PlanIssue(
                code="capability_unavailable",
                severity="error",
                message=f"{self.descriptor_kind} {normalized_id}@{normalized_version} is unavailable: {reason}",
                path=f"{self.descriptor_kind}.{normalized_id}",
            )
        return descriptor

    def list(self) -> list[DescriptorT]:
        return [self._descriptors[key] for key in sorted(self._descriptors)]


class DomainStrategyRegistry(_VersionedRegistry[DomainStrategyDescriptor]):
    descriptor_kind = "domain strategy"
    id_field = "strategy_id"


class LayoutAdapterRegistry(_VersionedRegistry[LayoutAdapterDescriptor]):
    descriptor_kind = "layout adapter"
    id_field = "adapter_id"


class RendererCapabilityRegistry(_VersionedRegistry[RendererCapabilityDescriptor]):
    descriptor_kind = "renderer capability"
    id_field = "renderer_id"


@dataclass
class CapabilityRegistries:
    domain_strategies: DomainStrategyRegistry
    layout_adapters: LayoutAdapterRegistry
    renderers: RendererCapabilityRegistry


def build_builtin_registries() -> CapabilityRegistries:
    """Build descriptors for behavior already supported by the repository.

    The template-free entries are deliberately present but unavailable.  This
    keeps an unsupported proposal visible to the planner rather than silently
    choosing a different layout or renderer.
    """

    domain_strategies = DomainStrategyRegistry()
    domain_strategies.register(DomainStrategyDescriptor(
        strategy_id="legacy_document_schema",
        version="1",
        supported_document_types=["generic", "icd", "fpt", "requirements"],
    ))
    for strategy_id, document_types in (
        ("generic_report", ["generic", "generic_report", "report"]),
        ("icd", ["icd"]),
        ("fpt", ["fpt"]),
        ("requirements", ["requirements"]),
    ):
        domain_strategies.register(DomainStrategyDescriptor(
            strategy_id=strategy_id,
            version="1",
            supported_document_types=document_types,
        ))

    layout_adapters = LayoutAdapterRegistry()
    layout_adapters.register(LayoutAdapterDescriptor(
        adapter_id="provided_template",
        version="1",
        supported_formats=["xlsx", "xlsm", "docx", "markdown"],
        supported_modes=["provided_template"],
    ))
    for adapter_id, formats in (
        ("system_recipe", ["docx", "pdf", "xlsx"]),
        ("generated_structure", ["docx", "pdf", "xlsx"]),
    ):
        layout_adapters.register(LayoutAdapterDescriptor(
            adapter_id=adapter_id,
            version="1",
            supported_formats=formats,
            supported_modes=[adapter_id],
        ))

    renderers = RendererCapabilityRegistry()
    for renderer_id in ("xlsx", "xlsm", "docx", "pdf", "markdown"):
        renderers.register(RendererCapabilityDescriptor(
            renderer_id=renderer_id,
            version="1",
            formats=[renderer_id],
        ))
    return CapabilityRegistries(
        domain_strategies=domain_strategies,
        layout_adapters=layout_adapters,
        renderers=renderers,
    )


__all__ = [
    "CapabilityRegistries",
    "DomainStrategy",
    "DomainStrategyDescriptor",
    "DomainStrategyRegistry",
    "LayoutAdapter",
    "LayoutAdapterDescriptor",
    "LayoutAdapterRegistry",
    "RendererCapability",
    "RendererCapabilityDescriptor",
    "RendererCapabilityRegistry",
    "build_builtin_registries",
]
