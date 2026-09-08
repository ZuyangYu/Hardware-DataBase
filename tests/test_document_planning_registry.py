from __future__ import annotations

import pytest

from src.document_authoring.planning.registry import (
    DomainStrategyDescriptor,
    DomainStrategyRegistry,
    LayoutAdapterDescriptor,
    PlanIssue,
    RendererCapabilityDescriptor,
    build_builtin_registries,
)


def test_registry_requires_exact_version_and_rejects_duplicates():
    registry = DomainStrategyRegistry()
    descriptor = DomainStrategyDescriptor(
        strategy_id="icd",
        version="1",
        supported_document_types=["icd"],
    )
    registry.register(descriptor)
    assert registry.lookup("icd", "1") == descriptor
    assert registry.lookup("icd", "2") is None
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(descriptor)


def test_disabled_or_missing_capability_returns_structured_issue_without_fallback():
    registry = DomainStrategyRegistry()
    registry.register(DomainStrategyDescriptor(
        strategy_id="future-domain",
        version="2",
        supported_document_types=["future"],
        status="unavailable",
        unavailable_reason="not rolled out",
    ))

    issue = registry.resolve("future-domain", "2")
    assert isinstance(issue, PlanIssue)
    assert issue.code == "capability_unavailable"
    assert issue.blocking is True
    assert registry.resolve("future-domain", "1").code == "capability_missing"


def test_builtin_registries_expose_phase3_capabilities_without_version_fallback():
    registries = build_builtin_registries()
    assert registries.domain_strategies.lookup("legacy_document_schema", "1") is not None
    assert registries.layout_adapters.lookup("provided_template", "1") is not None
    assert registries.renderers.lookup("xlsx", "1") is not None
    assert registries.renderers.lookup("docx", "1") is not None
    assert registries.renderers.lookup("pdf", "1") is not None
    assert registries.layout_adapters.resolve("generated_structure", "1").status == "available"
    assert registries.layout_adapters.resolve("system_recipe", "1").status == "available"
    assert registries.domain_strategies.lookup("generic_report", "1") is not None


def test_descriptors_are_strict_and_do_not_accept_runtime_payloads():
    with pytest.raises(ValueError):
        LayoutAdapterDescriptor(
            adapter_id="provided_template",
            version="1",
            supported_formats=["xlsx"],
            raw_content="must-not-be-persisted",
        )
    with pytest.raises(ValueError):
        RendererCapabilityDescriptor(
            renderer_id="xlsx",
            version="1",
            formats=["xlsx"],
            path="/tmp/renderer",
        )
