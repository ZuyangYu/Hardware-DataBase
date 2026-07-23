"""Independent, bounded authoring graph for semantic document units.

This is intentionally separate from the Q&A LangGraph state.  Its only
external operations are a frozen-source retrieval callback and a constrained
Managed Writer; both are checked against HarnessToolPolicy first.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, TypedDict

from src.agents.claim_evidence import InformationRequirement, RetrievalOutcome
from src.document_authoring.harness.policy import HarnessBudgetExceeded, HarnessToolPolicy
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentSchema,
    DocumentUnitDraft,
    DocumentWorkOrder,
    HarnessRun,
    LegacyTemplateClaim,
)
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.writers.managed import ManagedWriter
from src.document_authoring.writers.provider import WriterRequest
from src.projects.models import SourceSetSnapshot


class DocumentAuthoringState(TypedDict, total=False):
    work_order: DocumentWorkOrder
    harness_run: HarnessRun
    run_manifest: AuthoringRunManifest
    document_schema: DocumentSchema
    source_set_snapshot: SourceSetSnapshot
    information_requirements: dict[str, InformationRequirement]
    evidence_matrix: list[dict[str, Any]]
    retrieval_ledger: list[dict[str, Any]]
    section_drafts: dict[str, str]
    current_node: str
    step_count: int
    retrieval_round_count: int
    last_error: dict[str, Any] | None


RetrievalProvider = Callable[[InformationRequirement, int], RetrievalOutcome]
ProgressCallback = Callable[[DocumentAuthoringState], None]
DraftProvider = Callable[[WriterRequest], DocumentUnitDraft]


@dataclass
class HarnessExecutionResult:
    requirements: dict[str, InformationRequirement] = field(default_factory=dict)
    outcomes: dict[str, RetrievalOutcome] = field(default_factory=dict)
    matrix_rows: list[dict[str, Any]] = field(default_factory=list)
    drafts: list[DocumentUnitDraft] = field(default_factory=list)
    unit_statuses: dict[str, str] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    step_count: int = 0
    retrieval_round_count: int = 0


class AuthoringGraph:
    def __init__(
        self,
        policy: HarnessToolPolicy,
        writer: ManagedWriter,
        validator: DocumentValidator | None = None,
        on_progress: ProgressCallback | None = None,
        draft_provider: DraftProvider | None = None,
    ):
        self.policy = policy
        self.writer = writer
        self.validator = validator or DocumentValidator()
        self.on_progress = on_progress
        self.draft_provider = draft_provider or writer.generate

    def run(
        self,
        *,
        work_order: DocumentWorkOrder,
        harness_run: HarnessRun,
        run_manifest: AuthoringRunManifest,
        schema: DocumentSchema,
        snapshot: SourceSetSnapshot,
        legacy_claims: list[LegacyTemplateClaim],
        retrieve: RetrievalProvider,
    ) -> HarnessExecutionResult:
        if schema.execution_mode != "internal_harness" or work_order.execution_mode != "internal_harness":
            raise ValueError("authoring graph requires internal_harness work order and schema")
        semantic_units = _semantic_units(schema)
        if len(semantic_units) > self.policy.policy.max_units_per_run:
            raise HarnessBudgetExceeded("schema semantic unit count exceeds harness policy")
        result = HarnessExecutionResult()
        state: DocumentAuthoringState = {
            "work_order": work_order,
            "harness_run": harness_run,
            "run_manifest": run_manifest,
            "document_schema": schema,
            "source_set_snapshot": snapshot,
            "current_node": "initialize",
            "step_count": 0,
            "retrieval_round_count": 0,
        }

        try:
            self._step(state, "create_information_requirements")
            for unit in semantic_units:
                requirement = _requirement_for_unit(unit, work_order, snapshot)
                result.requirements[unit["unit_id"]] = requirement

            for unit_id, requirement in result.requirements.items():
                outcome = self._retrieve_with_budget(state, requirement, retrieve)
                result.outcomes[unit_id] = outcome
                evidence = _validated_evidence(work_order, snapshot, outcome)
                result.matrix_rows.append({
                    "field_id": unit_id.removeprefix("field:") if unit_id.startswith("field:") else None,
                    "review_item_id": unit_id.removeprefix("review:") if unit_id.startswith("review:") else None,
                    "requirement_id": requirement.requirement_id,
                    "coverage_status": _coverage_status(outcome),
                    "evidence_ids": [entry["id"] for entry in evidence],
                    "display_value": None,
                    "diagnostics": [source.model_dump(mode="json") for source in outcome.source_outcomes],
                })
                if not evidence:
                    result.unit_statuses[unit_id] = _missing_status(unit_id, schema, outcome)
                    continue
                self._step(state, "draft_ready_unit")
                self.policy.require_tool("draft_ready_unit")
                draft = self.draft_provider(WriterRequest(
                    work_order_id=work_order.work_order_id,
                    run_id=harness_run.harness_run_id,
                    unit_id=unit_id,
                    unit_label=_unit_label(unit_id, schema),
                    unit_description=_unit_description(unit_id, schema),
                    evidence=evidence,
                    missing_or_conflicts=[],
                    prompt_version=self.policy.policy.prompt_version,
                ))
                self._step(state, "validate_unit_draft")
                self.policy.require_tool("validate_unit_draft")
                evidence_by_id = {entry["id"]: entry for entry in evidence}
                validated = self.validator.validate_unit_draft(draft, evidence_by_id)
                contamination = []
                self._step(state, "detect_template_contamination")
                self.policy.require_tool("detect_template_contamination")
                contamination = self.validator.detect_template_contamination(validated, legacy_claims)
                if contamination:
                    validated = validated.model_copy(update={
                        "validation_status": "requires_human",
                        "validation_notes": [*validated.validation_notes, "template contamination detected"],
                    })
                    result.issues.extend(contamination)
                    result.unit_statuses[unit_id] = "requires_human"
                elif validated.validation_status != "supported":
                    result.unit_statuses[unit_id] = "requires_human"
                else:
                    result.unit_statuses[unit_id] = "ready_to_render"
                result.drafts.append(validated)

            self._step(state, "validate_cross_unit")
            self.policy.require_tool("validate_cross_unit")
            consistency_issues = self.validator.validate_cross_unit_consistency(result.drafts)
            if consistency_issues:
                result.issues.extend(consistency_issues)
                conflicted_units = {
                    unit_id
                    for issue in consistency_issues
                    for units in issue["values"].values()
                    for unit_id in units
                }
                for unit_id in conflicted_units:
                    result.unit_statuses[unit_id] = "conflicting"
            result.step_count = state["step_count"]
            result.retrieval_round_count = state["retrieval_round_count"]
            return result
        except HarnessBudgetExceeded as exc:
            result.issues.append({"kind": "harness_budget_exceeded", "message": str(exc)})
            result.step_count = state["step_count"]
            result.retrieval_round_count = state["retrieval_round_count"]
            for unit in semantic_units:
                result.unit_statuses.setdefault(unit["unit_id"], "requires_human")
            return result

    def _retrieve_with_budget(
        self,
        state: DocumentAuthoringState,
        requirement: InformationRequirement,
        retrieve: RetrievalProvider,
    ) -> RetrievalOutcome:
        self.policy.require_tool("retrieve_evidence")
        last: RetrievalOutcome | None = None
        for attempt in range(1, self.policy.policy.max_retrieval_rounds + 1):
            self._step(state, "retrieve_requirement_evidence")
            state["retrieval_round_count"] += 1
            self.policy.require_retrieval_round(state["retrieval_round_count"])
            outcome = retrieve(requirement, attempt)
            last = outcome
            if outcome.status not in {"retrieval_failed", "source_unavailable", "access_denied", "partial_failure"}:
                return outcome
        assert last is not None
        return last

    def _step(self, state: DocumentAuthoringState, node: str) -> None:
        state["current_node"] = node
        state["step_count"] += 1
        self.policy.require_step(state["step_count"])
        if self.on_progress is not None:
            self.on_progress(state)


def _semantic_units(schema: DocumentSchema) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for field_schema in schema.fields:
        if field_schema.authoring_policy == "managed_writer":
            units.append({"unit_id": f"field:{field_schema.field_id}", "kind": "field", "schema": field_schema})
    for item in schema.review_items:
        if item.evaluation_mode == "semantic_assisted":
            units.append({"unit_id": f"review:{item.review_item_id}", "kind": "review", "schema": item})
    return units


def _requirement_for_unit(unit: dict[str, Any], work_order: DocumentWorkOrder, snapshot: SourceSetSnapshot) -> InformationRequirement:
    schema = unit["schema"]
    if unit["kind"] == "field":
        capability = _capabilities(schema.required_capabilities)
        source_roles = schema.preferred_source_roles
        subject = schema.label
        predicate = schema.description or None
        missing_policy = schema.missing_policy
        claim_type = "attribute"
    else:
        capability = _capabilities(schema.required_capabilities)
        source_roles = schema.required_source_roles
        subject = schema.label
        predicate = "review"
        missing_policy = "mark_tbd"
        claim_type = "requirement"
    requirement_id = hashlib.sha256(
        f"{work_order.work_order_id}|{unit['unit_id']}|{work_order.input_fingerprint}".encode("utf-8")
    ).hexdigest()[:24]
    return InformationRequirement(
        requirement_id=f"req-{requirement_id}", semantic_unit_id=unit["unit_id"],
        claim_type=claim_type, subject=subject, predicate=predicate,
        required_capabilities=capability, preferred_source_roles=source_roles,
        project_id=work_order.project_id, baseline_id=work_order.baseline_id,
        source_version_scope=list(snapshot.source_version_ids), missing_policy=missing_policy,
    )


def _capabilities(values: list[str]) -> list[str]:
    allowed = {"entity_lookup", "relationship_lookup", "tabular_lookup", "document_claim_lookup", "revision_lookup"}
    return [value for value in values if value in allowed]


def _validated_evidence(work_order: DocumentWorkOrder, snapshot: SourceSetSnapshot, outcome: RetrievalOutcome) -> list[dict[str, Any]]:
    if outcome.applied_source_set_snapshot_id != snapshot.source_set_snapshot_id:
        raise PermissionError("harness retrieval outcome source-set mismatch")
    if outcome.applied_region_policy_versions != snapshot.region_policy_versions:
        raise PermissionError("harness retrieval outcome region-policy mismatch")
    if outcome.status not in {"success_with_hits", "success_empty"}:
        return []
    versions = set(snapshot.source_version_ids) | set(snapshot.shared_reference_version_ids)
    artifacts = set(snapshot.processing_artifact_ids)
    result: list[dict[str, Any]] = []
    for evidence in outcome.evidences:
        if (
            getattr(evidence, "project_id", None) != work_order.project_id
            or getattr(evidence, "source_version_id", None) not in versions
            or getattr(evidence, "processing_artifact_id", None) not in artifacts
        ):
            raise PermissionError("harness received evidence outside the frozen source set")
        result.append({
            "id": evidence.id, "content": evidence.content,
            "source_version_id": evidence.source_version_id,
            "processing_artifact_id": evidence.processing_artifact_id,
            "locator": dict(evidence.locator), "fact_type": evidence.fact_type,
        })
    return result


def _coverage_status(outcome: RetrievalOutcome) -> str:
    return "supported" if outcome.status == "success_with_hits" else (
        "missing" if outcome.status == "success_empty" else outcome.status
    )


def _missing_status(unit_id: str, schema: DocumentSchema, outcome: RetrievalOutcome) -> str:
    if outcome.status in {"retrieval_failed", "source_unavailable", "access_denied", "partial_failure"}:
        return "retrieval_failed"
    if unit_id.startswith("field:"):
        field_id = unit_id.removeprefix("field:")
        field = next(item for item in schema.fields if item.field_id == field_id)
        return "blocked" if field.missing_policy == "block_section" else "tbd"
    return "insufficient_evidence"


def _unit_label(unit_id: str, schema: DocumentSchema) -> str:
    if unit_id.startswith("field:"):
        return next(item.label for item in schema.fields if item.field_id == unit_id.removeprefix("field:"))
    return next(item.label for item in schema.review_items if item.review_item_id == unit_id.removeprefix("review:"))


def _unit_description(unit_id: str, schema: DocumentSchema) -> str:
    if unit_id.startswith("field:"):
        return next(item.description for item in schema.fields if item.field_id == unit_id.removeprefix("field:"))
    return ""
