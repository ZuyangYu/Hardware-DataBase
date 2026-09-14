"""Compatibility conversion from the legacy GenerationBrief to OutputSpec."""

from __future__ import annotations

from typing import Any, Mapping

from src.document_authoring.generation_sessions import GenerationBrief
from src.document_authoring.models import DocumentSchema

from .models import (
    ArtifactSpec,
    DeliverableSpec,
    OutlineUnitSpec,
    OutputSpec,
    ProvidedTemplateSource,
    SourceScopeSpec,
    TableRequirement,
)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _target_identity(brief: GenerationBrief) -> dict[str, str]:
    identity: dict[str, str] = {}
    scope = brief.scope if isinstance(brief.scope, Mapping) else {}
    explicit = scope.get("target_identity")
    if isinstance(explicit, Mapping):
        identity.update({str(key): str(value) for key, value in explicit.items() if str(value).strip()})
    for key in ("project", "product", "release", "version", "module", "connector"):
        value = scope.get(key)
        if value is not None and str(value).strip() and key not in identity:
            identity[key] = str(value).strip()
    return identity


def legacy_brief_to_output_spec(
    brief: GenerationBrief | Mapping[str, Any],
    *,
    template_version_id: str,
    template_schema_id: str,
    template_schema_version: str,
    document_type: str,
    target_format: str,
    output_spec_id: str,
    output_spec_version: int = 1,
    document_schema: DocumentSchema | None = None,
) -> OutputSpec:
    """Project legacy request data into a new, one-way OutputSpec version.

    The optional schema enriches the outline and table requirements.  If it is
    absent, the legacy scope still remains represented without pretending that
    a schema was inspected.
    """

    normalized_brief = brief if isinstance(brief, GenerationBrief) else GenerationBrief.model_validate(brief)
    fields = list(document_schema.fields) if document_schema is not None else []
    review_items = list(document_schema.review_items) if document_schema is not None else []
    outline = [
        OutlineUnitSpec(unit_id=field.field_id, kind="table" if field.value_type == "table" else "field", title=field.label, required=field.required)
        for field in fields
    ]
    outline.extend(
        OutlineUnitSpec(unit_id=item.review_item_id, kind="review_item", title=item.label, required=True)
        for item in review_items
    )
    if not outline:
        outline = [OutlineUnitSpec(unit_id="document", kind="section", title=document_type, required=True)]
    table_requirements = []
    scope = normalized_brief.scope if isinstance(normalized_brief.scope, Mapping) else {}
    requested_rows = scope.get("row_keys")
    for field in fields:
        if field.value_type != "table":
            continue
        columns = list((field.table_columns or {}).values())
        if not columns:
            columns = ["value"]
        table_requirements.append(TableRequirement(
            unit_id=field.field_id,
            row_scope=str(scope.get("row_scope") or "unresolved"),
            required_columns=columns,
            row_keys=_string_list(requested_rows),
            row_identity_fields=_string_list(scope.get("row_identity_fields")),
            row_key_schema=(
                dict(scope.get("row_key_schema"))
                if isinstance(scope.get("row_key_schema"), Mapping) else {}
            ),
            row_order=str(scope.get("row_order") or "declared"),
            duplicate_policy=str(scope.get("duplicate_policy") or "reject"),
        ))
    source_policy = normalized_brief.source_policy if isinstance(normalized_brief.source_policy, Mapping) else {}
    source_scope = SourceScopeSpec(
        knowledge_bases=_string_list(source_policy.get("knowledge_bases") or source_policy.get("knowledge_base_name")),
        attachments=_string_list(source_policy.get("attachments") or scope.get("attachments")),
        projects=_string_list(scope.get("projects") or scope.get("project")),
        version_policy="current_published",
    )
    missing_policy = normalized_brief.missing_data_policy or "mark_tbd"
    inference_policy = normalized_brief.inference_policy or "forbid"
    layout = ProvidedTemplateSource(
        template_version_id=template_version_id,
        template_schema_id=template_schema_id,
        template_schema_version=template_schema_version,
    )
    return OutputSpec(
        output_spec_id=output_spec_id,
        version=output_spec_version,
        status="proposed",
        purpose=normalized_brief.purpose or f"Generate {document_type}",
        audience=_string_list(scope.get("audience")),
        document_type=document_type,
        target_identity=_target_identity(normalized_brief),
        artifact=ArtifactSpec(deliverables=[DeliverableSpec(
            format=target_format,
            role="primary",
            required=True,
            requested_by="user",
        )]),
        layout_source=layout,
        outline=outline,
        table_requirements=table_requirements,
        source_scope=source_scope,
        language=str(scope.get("language") or "en-US"),
        style={str(key): str(value) for key, value in (scope.get("style") or {}).items()} if isinstance(scope.get("style"), Mapping) else {},
        missing_data_policy=missing_policy,
        inference_policy=inference_policy,
        approval_policy_id=str(scope.get("approval_policy_id") or "default-document-v1"),
        accepted_recommendations=[],
    )


__all__ = ["legacy_brief_to_output_spec"]
