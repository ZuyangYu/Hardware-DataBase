"""Independent, bounded authoring graph for semantic document units.

This is intentionally separate from the Q&A LangGraph state.  Its only
external operations are a frozen-source retrieval callback and a constrained
Managed Writer; both are checked against HarnessToolPolicy first.
"""

from __future__ import annotations

import hashlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from src.agents.claim_evidence import InformationRequirement, RetrievalOutcome
from src.document_authoring.circuit_capabilities import enrich_circuit_capabilities
from src.document_authoring.harness.policy import HarnessBudgetExceeded, HarnessToolPolicy
from src.document_authoring.harness.checkpointer import FencedCheckpointer, build_checkpointer
from src.document_authoring.harness.langgraph_state import (
    DocumentAuthoringState as PersistedAuthoringState,
    initial_authoring_state,
)
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentSchema,
    DocumentUnitDraft,
    DocumentWorkOrder,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
    LegacyTemplateClaim,
)
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.writers.managed import DeterministicEvidenceWriter, ManagedWriter
from src.document_authoring.writers.provider import WriterRequest
from src.projects.models import SourceSetSnapshot

if TYPE_CHECKING:
    from src.document_authoring.writers.evidence_reranker import EvidenceReranker
    from src.document_authoring.writers.query_rewriter import QueryRewriter
    from src.document_authoring.writers.requirement_fit_checker import RequirementFitChecker


class DocumentAuthoringState(TypedDict, total=False):
    work_order: DocumentWorkOrder
    harness_run: HarnessRun
    run_manifest: AuthoringRunManifest
    document_schema: DocumentSchema
    source_set_snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot
    information_requirements: dict[str, InformationRequirement]
    evidence_matrix: list[dict[str, Any]]
    retrieval_ledger: list[dict[str, Any]]
    section_drafts: dict[str, str]
    current_node: str
    step_count: int
    retrieval_round_count: int
    completed_units: int
    total_units: int
    last_error: dict[str, Any] | None


RetrievalProvider = Callable[[InformationRequirement, int, "str | None"], RetrievalOutcome]
ProgressCallback = Callable[[DocumentAuthoringState], None]
DraftProvider = Callable[[WriterRequest], DocumentUnitDraft]


@dataclass
class HarnessExecutionResult:
    requirements: dict[str, InformationRequirement] = field(default_factory=dict)
    outcomes: dict[str, RetrievalOutcome] = field(default_factory=dict)
    matrix_rows: list[dict[str, Any]] = field(default_factory=list)
    retrieval_ledger: list[dict[str, Any]] = field(default_factory=list)
    drafts: list[DocumentUnitDraft] = field(default_factory=list)
    unit_statuses: dict[str, str] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    step_count: int = 0
    retrieval_round_count: int = 0
    agent_token_usage: dict[str, Any] = field(default_factory=dict)


def _json_safe_graph_value(value: Any) -> Any:
    """Keep only bounded JSON values in the persisted LangGraph channel."""
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_graph_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_graph_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe_graph_value(value.model_dump(mode="json"))
    return str(value)


class AuthoringGraph:
    def __init__(
        self,
        policy: HarnessToolPolicy,
        writer: ManagedWriter,
        validator: DocumentValidator | None = None,
        on_progress: ProgressCallback | None = None,
        draft_provider: DraftProvider | None = None,
        rewriter: "QueryRewriter | None" = None,
        reranker: "EvidenceReranker | None" = None,
        fit_checker: "RequirementFitChecker | None" = None,
    ):
        self.policy = policy
        self.writer = writer
        self.validator = validator or DocumentValidator()
        self.on_progress = on_progress
        self.draft_provider = draft_provider or writer.generate
        self.rewriter = rewriter
        self.reranker = reranker
        self.fit_checker = fit_checker
        self._active_execution: dict[str, Any] | None = None
        self._last_results: dict[str, HarnessExecutionResult] = {}
        self._graph_results: dict[str, HarnessExecutionResult] = {}
        self._graph_units: dict[str, dict[str, dict[str, Any]]] = {}
        self._graph_lock = threading.RLock()
        self._compiled_graph = None
        self._last_graph_state: PersistedAuthoringState | None = None

    def run(
        self,
        *,
        work_order: DocumentWorkOrder,
        harness_run: HarnessRun,
        run_manifest: AuthoringRunManifest,
        schema: DocumentSchema,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        legacy_claims: list[LegacyTemplateClaim],
        retrieve: RetrievalProvider,
        checkpointer: FencedCheckpointer | None = None,
    ) -> HarnessExecutionResult:
        """Execute the compiled LangGraph for one authoring run.

        The domain objects/callbacks are request-scoped execution context, not
        graph state.  The compiled graph stores only the ID/count/status
        control plane; each Send branch reloads its requirement from the
        request-scoped context and keeps raw evidence only in the coordinator's
        bounded execution memory.  Business facts are persisted by the runtime
        callback before the graph checkpoint advances.
        """
        unit_ids = [unit["unit_id"] for unit in _semantic_units(schema)]
        persisted = initial_authoring_state(
            work_order_id=work_order.work_order_id,
            harness_run_id=harness_run.harness_run_id,
            run_manifest_id=run_manifest.run_manifest_id,
            source_set_snapshot_id=work_order.source_set_snapshot_id,
            input_fingerprint=work_order.input_fingerprint,
            schema_version=schema.version,
            unit_ids=unit_ids,
        )
        self._active_execution = {
            "work_order": work_order,
            "harness_run": harness_run,
            "run_manifest": run_manifest,
            "schema": schema,
            "snapshot": snapshot,
            "legacy_claims": legacy_claims,
            "retrieve": retrieve,
        }
        with self._graph_lock:
            self._graph_results.pop(harness_run.harness_run_id, None)
            self._graph_units.pop(harness_run.harness_run_id, None)
        saver = checkpointer or build_checkpointer("memory")
        self._compiled_graph = self.build_compiled_graph(saver)
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": harness_run.harness_run_id,
                "fencing_token": harness_run.fencing_token,
            }
        }
        try:
            final_state = self._compiled_graph.invoke(persisted, config)
            self._last_graph_state = final_state
            result = self._graph_results.pop(harness_run.harness_run_id, None)
            if result is None:
                # Private compatibility nodes may still populate this map when
                # an older integration supplies a custom compiled graph.
                result = self._last_results.pop(harness_run.harness_run_id, None)
            if result is None:
                raise RuntimeError("compiled authoring graph completed without an execution result")
            result.step_count = int(final_state.get("step_count", result.step_count) or 0)
            result.retrieval_round_count = int(
                final_state.get("retrieval_round_count", result.retrieval_round_count) or 0
            )
            return result
        finally:
            with self._graph_lock:
                self._graph_units.pop(harness_run.harness_run_id, None)
            self._active_execution = None

    def run_field(
        self,
        field_id: str,
        *,
        work_order: DocumentWorkOrder,
        harness_run: HarnessRun,
        run_manifest: AuthoringRunManifest,
        schema: DocumentSchema,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        legacy_claims: list[LegacyTemplateClaim],
        retrieve: RetrievalProvider,
        checkpointer: FencedCheckpointer | None = None,
    ) -> HarnessExecutionResult:
        """Run one semantic field through the same compiled graph.

        This is intentionally a filtering entry point, not a second Writer or
        retrieval implementation; AgentFieldHarness uses it for safe
        field-level fallback.
        """
        normalized = str(field_id)
        if not normalized.startswith("field:"):
            normalized = f"field:{normalized}"
        field = next((item for item in schema.fields if f"field:{item.field_id}" == normalized), None)
        if field is None:
            raise KeyError(f"document schema field not found: {field_id}")
        filtered = schema.model_copy(update={"fields": [field], "review_items": []})
        return self.run(
            work_order=work_order,
            harness_run=harness_run,
            run_manifest=run_manifest,
            schema=filtered,
            snapshot=snapshot,
            legacy_claims=legacy_claims,
            retrieve=retrieve,
            checkpointer=checkpointer,
        )

    def build_compiled_graph(self, checkpointer: FencedCheckpointer | None = None):
        """Build the named, checkpointed authoring StateGraph.

        ``plan_units`` emits bounded ``Send`` batches.  Every branch traverses
        the same retrieve/generate/validate/persist nodes and joins through
        reducers in ``langgraph_state.py``.  The old Python implementation is
        retained only as a private compatibility helper for older test and
        integration adapters; ``run`` never calls it.
        """
        graph = StateGraph(PersistedAuthoringState)
        graph.add_node("load_context", self._node_load_context)
        graph.add_node("plan_units", self._node_plan_units)
        graph.add_node("retrieve_evidence", self._node_retrieve_evidence)
        graph.add_node("generate_draft", self._node_generate_draft)
        graph.add_node("validate_draft", self._node_validate_draft)
        graph.add_node("persist_draft", self._node_persist_draft)
        graph.add_node("route_next_unit", self._node_route_next_unit)
        graph.add_node("await_human", self._node_await_human)
        graph.add_node("finalize", self._node_finalize)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "plan_units")
        graph.add_conditional_edges(
            "plan_units",
            self._dispatch_from_plan,
            {"route_next_unit": "route_next_unit"},
        )
        graph.add_conditional_edges(
            "retrieve_evidence",
            self._forward_to_generate,
            {"generate_draft": "generate_draft"},
        )
        graph.add_conditional_edges(
            "generate_draft",
            self._forward_to_validate,
            {"validate_draft": "validate_draft"},
        )
        graph.add_conditional_edges(
            "validate_draft",
            self._forward_to_persist,
            {"persist_draft": "persist_draft"},
        )
        graph.add_edge("persist_draft", "route_next_unit")
        graph.add_conditional_edges(
            "route_next_unit",
            self._dispatch_or_finalize,
            {"await_human": "await_human", "finalize": "finalize"},
        )
        graph.add_edge("await_human", END)
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=checkpointer or build_checkpointer("memory"))

    def _node_load_context(self, state: PersistedAuthoringState) -> dict[str, Any]:
        if self._active_execution is None:
            raise RuntimeError("authoring graph context is unavailable; reload by IDs before resume")
        return {"current_node": "load_context"}

    def _node_plan_units(self, state: PersistedAuthoringState) -> dict[str, Any]:
        context = self._active_execution
        if context is None:
            raise RuntimeError("authoring graph context is unavailable; reload by IDs before resume")
        run_id = context["harness_run"].harness_run_id
        semantic_units = _semantic_units(context["schema"])
        local_state: DocumentAuthoringState = dict(state)
        before_steps = int(state.get("step_count", 0) or 0)
        result = HarnessExecutionResult()
        budget_issue: dict[str, Any] | None = None
        try:
            # Keep the legacy budget semantics: planning itself consumes the
            # information-requirements step, so a one-step policy must route
            # to review before any retrieval callback is invoked.
            self._step(local_state, "create_information_requirements")
        except HarnessBudgetExceeded as exc:
            budget_issue = {"kind": "harness_budget_exceeded", "message": str(exc)}
        for unit in semantic_units:
            result.requirements[unit["unit_id"]] = _requirement_for_unit(
                unit, context["work_order"], context["snapshot"],
            )
            result.unit_statuses[unit["unit_id"]] = "planned"
        if budget_issue is not None:
            result.issues.append(budget_issue)
            result.unit_statuses = {
                unit["unit_id"]: "requires_human" for unit in semantic_units
            }
        if len(semantic_units) > self.policy.policy.max_units_per_run:
            issue = {
                "kind": "harness_budget_exceeded",
                "message": "schema semantic unit count exceeds harness policy",
            }
            result.issues.append(issue)
            result.unit_statuses = {
                unit["unit_id"]: "requires_human" for unit in semantic_units
            }
        with self._graph_lock:
            self._graph_results[run_id] = result
            self._graph_units[run_id] = {}
        return {
            "current_node": "plan_units",
            "dispatch_cursor": int(state.get("dispatch_cursor", 0)),
            "unit_statuses": dict(result.unit_statuses),
            "total_units": len(semantic_units),
            "issues": _json_safe_graph_value(result.issues),
            "step_count": max(0, int(local_state.get("step_count", 0)) - before_steps),
        }

    def _dispatch_from_plan(self, state: PersistedAuthoringState) -> str | list[Send]:
        unit_ids = list(state.get("unit_ids", []))
        statuses = state.get("unit_statuses", {}) or {}
        if not unit_ids or all(
            str(status).casefold() in {"requires_human", "blocked", "failed"}
            for status in statuses.values()
        ):
            return "route_next_unit"
        return self._send_unit_batch(state, int(state.get("dispatch_cursor", 0)))

    def _send_unit_batch(
        self,
        state: PersistedAuthoringState,
        cursor: int,
    ) -> list[Send]:
        unit_ids = list(state.get("unit_ids", []))
        start = max(0, min(int(cursor), len(unit_ids)))
        capacity = max(1, int(self.policy.policy.max_parallel_units))
        end = min(start + capacity, len(unit_ids))
        if start >= end:
            return []
        return [
            Send(
                "retrieve_evidence",
                {
                    **dict(state),
                    "current_unit_id": unit_id,
                    "dispatch_cursor": end,
                    "in_flight_unit_ids": [unit_id],
                },
            )
            for unit_id in unit_ids[start:end]
        ]

    def _graph_context_for_unit(
        self,
        state: PersistedAuthoringState,
    ) -> tuple[dict[str, Any], HarnessExecutionResult, str, dict[str, Any]]:
        context = self._active_execution
        if context is None:
            raise RuntimeError("authoring graph context is unavailable; reload by IDs before resume")
        unit_id = str(state.get("current_unit_id") or "")
        if not unit_id:
            raise RuntimeError("authoring graph branch has no current semantic unit")
        run_id = context["harness_run"].harness_run_id
        with self._graph_lock:
            result = self._graph_results.get(run_id)
            units = self._graph_units.setdefault(run_id, {})
        if result is None:
            raise RuntimeError("authoring graph result context is unavailable")
        unit = units.setdefault(unit_id, {})
        return context, result, unit_id, unit

    def _node_retrieve_evidence(self, state: PersistedAuthoringState) -> dict[str, Any]:
        context, result, unit_id, unit = self._graph_context_for_unit(state)
        requirement = result.requirements[unit_id]
        local_state: DocumentAuthoringState = dict(state)
        before_steps = int(state.get("step_count", 0) or 0)
        before_rounds = int(state.get("retrieval_round_count", 0) or 0)
        try:
            outcome = self._retrieve_with_budget(
                local_state, requirement, context["retrieve"],
            )
        except HarnessBudgetExceeded as exc:
            with self._graph_lock:
                result.issues.append({"kind": "harness_budget_exceeded", "message": str(exc)})
                result.unit_statuses[unit_id] = "requires_human"
            return {
                "current_node": "retrieve_evidence",
                "current_unit_id": unit_id,
                "unit_statuses": {unit_id: "requires_human"},
                "issues": [{"kind": "harness_budget_exceeded", "message": str(exc)}],
                "step_count": max(0, int(local_state.get("step_count", 0)) - before_steps),
                "retrieval_round_count": max(
                    0, int(local_state.get("retrieval_round_count", 0)) - before_rounds,
                ),
                "dispatch_cursor": int(state.get("dispatch_cursor", 0)),
            }

        recovery_triggered = any(
            _evidence_low_confidence(evidence_obj) for evidence_obj in outcome.evidences
        )
        evidence = _validated_evidence(context["work_order"], context["snapshot"], outcome)
        preselected_evidence, preselected_discarded_evidence_ids = _select_field_evidence(
            evidence,
            _max_evidence_items(unit_id, context["schema"]),
            preserve_rerank_order=False,
            retrieval_query_terms=requirement.retrieval_query_terms,
        )
        fast_path = (
            not recovery_triggered
            and getattr(
                getattr(self.writer, "provider", self.writer), "provider_id", None,
            ) == DeterministicEvidenceWriter.provider_id
            and _use_deterministic_evidence_writer(
                unit_id, context["schema"], requirement, preselected_evidence,
            )
        )
        if fast_path:
            evidence = preselected_evidence
            discarded_evidence_ids = preselected_discarded_evidence_ids
        elif evidence and self.reranker is not None:
            self._step(local_state, "rerank_evidence")
            self.policy.require_tool("rerank_evidence")
            evidence = self.reranker.rerank(requirement, evidence)
            evidence, discarded_evidence_ids = _select_field_evidence(
                evidence,
                _max_evidence_items(unit_id, context["schema"]),
                preserve_rerank_order=True,
                retrieval_query_terms=requirement.retrieval_query_terms,
            )
        else:
            evidence, discarded_evidence_ids = _select_field_evidence(
                evidence,
                _max_evidence_items(unit_id, context["schema"]),
                preserve_rerank_order=False,
                retrieval_query_terms=requirement.retrieval_query_terms,
            )

        ledger_row = _retrieval_ledger_row(
            unit_id, requirement, outcome, evidence, local_state,
            recovery_triggered, discarded_evidence_ids,
        )
        with self._graph_lock:
            result.outcomes[unit_id] = outcome
            result.retrieval_ledger.append(ledger_row)
            result.matrix_rows.append({
                "field_id": unit_id.removeprefix("field:") if unit_id.startswith("field:") else None,
                "review_item_id": unit_id.removeprefix("review:") if unit_id.startswith("review:") else None,
                "requirement_id": requirement.requirement_id,
                "coverage_status": _coverage_status(outcome),
                "evidence_ids": [entry["id"] for entry in evidence],
                "display_value": None,
                "diagnostics": [source.model_dump(mode="json") for source in outcome.source_outcomes],
                "retrieval_ledger": ledger_row,
            })
            unit.update({
                "requirement": requirement,
                "outcome": outcome,
                "evidence": evidence,
                "recovery_triggered": recovery_triggered,
                "fast_path": fast_path,
                "discarded_evidence_ids": discarded_evidence_ids,
            })
            if not evidence:
                result.unit_statuses[unit_id] = _missing_status(
                    unit_id, context["schema"], outcome,
                )
            retrieval_completed = len(result.outcomes)
        # A LangGraph fan-in is a synchronization point for the next node, but
        # retrieval itself can finish out of order. Publish that bounded
        # progress signal immediately so the runtime heartbeat/UI can observe a
        # completed fast branch while another source call is still running.
        if self.on_progress is not None:
            progress = dict(local_state)
            progress.update({
                "current_node": "parallel_units",
                "completed_units": retrieval_completed,
                "total_units": len(state.get("unit_ids", [])),
            })
            self.on_progress(progress)
        updates: dict[str, Any] = {
            "current_node": "retrieve_evidence",
            "current_unit_id": unit_id,
            "dispatch_cursor": int(state.get("dispatch_cursor", 0)),
            "step_count": max(0, int(local_state.get("step_count", 0)) - before_steps),
            "retrieval_round_count": max(
                0, int(local_state.get("retrieval_round_count", 0)) - before_rounds,
            ),
        }
        if not evidence:
            updates["unit_statuses"] = {
                unit_id: result.unit_statuses[unit_id],
            }
        return updates

    def _node_execute_fixed_pipeline(self, state: PersistedAuthoringState) -> dict[str, Any]:
        context = self._active_execution
        if context is None:
            raise RuntimeError("authoring graph context is unavailable; reload by IDs before resume")
        result = self._run_legacy(**context)
        self._last_results[context["harness_run"].harness_run_id] = result
        evidence_ids = [
            evidence.id
            for outcome in result.outcomes.values()
            for evidence in outcome.evidences
            if getattr(evidence, "id", None)
        ]
        status_updates = dict(result.unit_statuses)
        return {
            "current_node": "retrieve_evidence",
            "unit_statuses": status_updates,
            "unit_attempts": {unit_id: 1 for unit_id in status_updates},
            "dispatch_cursor": len(state.get("unit_ids", [])),
            "in_flight_unit_ids": [],
            "evidence_registry_ids": evidence_ids,
            "draft_ids": [draft.unit_id for draft in result.drafts],
            "issues": _json_safe_graph_value(result.issues),
            "step_count": result.step_count,
            "retrieval_round_count": result.retrieval_round_count,
        }

    @staticmethod
    def _forward_to_generate(state: PersistedAuthoringState) -> list[Send]:
        return [Send("generate_draft", dict(state))]

    @staticmethod
    def _forward_to_validate(state: PersistedAuthoringState) -> list[Send]:
        return [Send("validate_draft", dict(state))]

    @staticmethod
    def _forward_to_persist(state: PersistedAuthoringState) -> list[Send]:
        return [Send("persist_draft", dict(state))]

    def _node_generate_draft(self, state: PersistedAuthoringState) -> dict[str, Any]:
        context, _result, unit_id, unit = self._graph_context_for_unit(state)
        evidence = list(unit.get("evidence") or [])
        if not evidence:
            return {
                "current_node": "generate_draft",
                "current_unit_id": unit_id,
                "dispatch_cursor": int(state.get("dispatch_cursor", 0)),
            }
        local_state: DocumentAuthoringState = dict(state)
        before_steps = int(state.get("step_count", 0) or 0)
        self._step(local_state, "draft_ready_unit")
        self.policy.require_tool("draft_ready_unit")
        requirement = unit["requirement"]
        request = build_writer_request(
            work_order=context["work_order"],
            harness_run=context["harness_run"],
            unit_id=unit_id,
            schema=context["schema"],
            requirement=requirement,
            evidence=evidence,
            prompt_version=self.policy.policy.prompt_version,
        )
        draft = (
            DeterministicEvidenceWriter().generate(request)
            if unit.get("fast_path")
            else self.draft_provider(request)
        )
        with self._graph_lock:
            unit["request"] = request
            unit["draft"] = draft
        return {
            "current_node": "generate_draft",
            "current_unit_id": unit_id,
            "dispatch_cursor": int(state.get("dispatch_cursor", 0)),
            "step_count": max(0, int(local_state.get("step_count", 0)) - before_steps),
        }

    def _node_validate_draft(self, state: PersistedAuthoringState) -> dict[str, Any]:
        context, result, unit_id, unit = self._graph_context_for_unit(state)
        draft = unit.get("draft")
        if draft is None:
            return {
                "current_node": "validate_draft",
                "current_unit_id": unit_id,
                "dispatch_cursor": int(state.get("dispatch_cursor", 0)),
            }
        local_state: DocumentAuthoringState = dict(state)
        before_steps = int(state.get("step_count", 0) or 0)
        self._step(local_state, "validate_unit_draft")
        self.policy.require_tool("validate_unit_draft")
        evidence = list(unit.get("evidence") or [])
        evidence_by_id = {entry["id"]: entry for entry in evidence}
        validated = self.validator.validate_unit_draft(draft, evidence_by_id)
        if unit_id.startswith("field:"):
            validated = self.validator.validate_typed_field_draft(
                validated,
                evidence_by_id,
                expected_value_type=_unit_value_type(unit_id, context["schema"]),
            )
        self._step(local_state, "detect_template_contamination")
        self.policy.require_tool("detect_template_contamination")
        contamination = self.validator.detect_template_contamination(
            validated, context["legacy_claims"],
        )
        issues: list[dict[str, Any]] = []
        status = "ready_to_render"
        if contamination:
            validated = validated.model_copy(update={
                "validation_status": "requires_human",
                "validation_notes": [
                    *validated.validation_notes, "template contamination detected",
                ],
            })
            issues.extend(contamination)
            status = "requires_human"
        elif validated.validation_status != "supported":
            status = "requires_human"
        elif self.fit_checker is not None:
            self._step(local_state, "requirement_fit_check")
            self.policy.require_tool("requirement_fit_check")
            verdict = self.fit_checker.check(validated, unit["requirement"])
            if not verdict["fit"]:
                validated = validated.model_copy(update={
                    "validation_status": "requires_human",
                    "validation_notes": [
                        *validated.validation_notes,
                        f"requirement fit check: {verdict['reason']}",
                    ],
                })
                issues.append({
                    "kind": "requirement_fit_failed",
                    "unit_id": unit_id,
                    "reason": verdict["reason"],
                })
                status = "requires_human"
        if unit.get("recovery_triggered") and evidence:
            if validated.validation_status == "supported":
                validated = validated.model_copy(update={
                    "validation_status": "requires_human",
                    "validation_notes": [
                        *validated.validation_notes, "low-confidence recovery evidence",
                    ],
                })
            status = "requires_human"
            issues.append({
                "kind": "low_confidence_recovery",
                "unit_id": unit_id,
                "reason": "evidence recovered via balanced-route retry",
            })
        with self._graph_lock:
            unit["validated"] = validated
            unit["status"] = status
            result.issues.extend(issues)
        updates: dict[str, Any] = {
            "current_node": "validate_draft",
            "current_unit_id": unit_id,
            "dispatch_cursor": int(state.get("dispatch_cursor", 0)),
            "unit_statuses": {unit_id: status},
            "step_count": max(0, int(local_state.get("step_count", 0)) - before_steps),
        }
        if issues:
            updates["issues"] = _json_safe_graph_value(issues)
        return updates

    def _node_persist_draft(self, state: PersistedAuthoringState) -> dict[str, Any]:
        context, result, unit_id, unit = self._graph_context_for_unit(state)
        del context
        validated = unit.get("validated")
        with self._graph_lock:
            if validated is not None and not any(
                existing.unit_id == validated.unit_id for existing in result.drafts
            ):
                result.drafts.append(validated)
            if validated is not None:
                result.unit_statuses[unit_id] = str(
                    unit.get("status") or (
                        "ready_to_render"
                        if validated.validation_status == "supported"
                        else "requires_human"
                    )
                )
            result_status = result.unit_statuses.get(unit_id)
            completed = len(
                [
                    value for value in result.unit_statuses.values()
                    if value not in {"planned", "retrieving", "drafting", "validating"}
                ]
            )
        if self.on_progress is not None:
            progress = dict(state)
            progress.update({
                "current_node": "parallel_units",
                "completed_units": completed,
                "total_units": len(state.get("unit_ids", [])),
            })
            self.on_progress(progress)
        updates: dict[str, Any] = {
            "current_node": "persist_draft",
            "current_unit_id": unit_id,
            "dispatch_cursor": int(state.get("dispatch_cursor", 0)),
            "completed_units": 1,
        }
        if result_status is not None:
            updates["unit_statuses"] = {unit_id: result_status}
        return updates

    @staticmethod
    def _node_route_next_unit(state: PersistedAuthoringState) -> dict[str, Any]:
        return {"current_node": "route_next_unit"}

    @staticmethod
    def _node_await_human(state: PersistedAuthoringState) -> dict[str, Any]:
        return {
            "current_node": "await_human",
            "pending_human_action": state.get("pending_human_action"),
            "paused": True,
            "completed": False,
        }

    @staticmethod
    def _node_finalize(state: PersistedAuthoringState) -> dict[str, Any]:
        return {"current_node": "finalize", "completed": True}

    def _dispatch_or_finalize(self, state: PersistedAuthoringState) -> str | list[Send]:
        cursor = int(state.get("dispatch_cursor", 0) or 0)
        unit_ids = list(state.get("unit_ids", []))
        if cursor < len(unit_ids):
            return self._send_unit_batch(state, cursor)
        run_id = str(state.get("harness_run_id", ""))
        with self._graph_lock:
            result = self._graph_results.get(run_id)
        if result is not None:
            self._finalize_graph_result(result, state)
            statuses = set(result.unit_statuses.values())
        else:
            statuses = set((state.get("unit_statuses") or {}).values())
        if statuses & {"requires_human", "blocked", "conflicting", "retrieval_failed"}:
            return "await_human"
        return "finalize"

    def _finalize_graph_result(
        self,
        result: HarnessExecutionResult,
        state: PersistedAuthoringState,
    ) -> None:
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
        # Send branches may complete in any order.  Normalize all externally
        # visible collections at the join so artifacts, manifests and tests do
        # not inherit scheduler timing.
        unit_order = {unit_id: index for index, unit_id in enumerate(state.get("unit_ids", []))}

        def unit_sort_key(unit_id: str | None) -> tuple[int, str]:
            normalized = str(unit_id or "")
            return (unit_order.get(normalized, len(unit_order)), normalized)

        result.requirements = {
            unit_id: result.requirements[unit_id]
            for unit_id in unit_order
            if unit_id in result.requirements
        }
        result.outcomes = {
            unit_id: result.outcomes[unit_id]
            for unit_id in unit_order
            if unit_id in result.outcomes
        }
        result.unit_statuses = {
            unit_id: result.unit_statuses[unit_id]
            for unit_id in unit_order
            if unit_id in result.unit_statuses
        }
        result.drafts.sort(key=lambda draft: unit_sort_key(draft.unit_id))
        result.retrieval_ledger.sort(key=lambda row: unit_sort_key(row.get("unit_id")))
        result.matrix_rows.sort(
            key=lambda row: unit_sort_key(
                (
                    f"field:{row['field_id']}" if row.get("field_id") is not None
                    else f"review:{row['review_item_id']}"
                    if row.get("review_item_id") is not None else None
                )
            )
        )
        result.step_count = int(state.get("step_count", result.step_count) or 0)
        result.retrieval_round_count = int(
            state.get("retrieval_round_count", result.retrieval_round_count) or 0
        )

    @staticmethod
    def _route_after_pipeline(state: PersistedAuthoringState) -> str:
        statuses = set((state.get("unit_statuses") or {}).values())
        if statuses & {"requires_human", "blocked", "conflicting", "retrieval_failed"}:
            return "await_human"
        return "finalize"

    # Legacy body retained as a private implementation detail of the compiled
    # nodes.  New callers use run()/run_field(), never this method directly.
    def _run_legacy(
        self,
        *,
        work_order: DocumentWorkOrder,
        harness_run: HarnessRun,
        run_manifest: AuthoringRunManifest,
        schema: DocumentSchema,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        legacy_claims: list[LegacyTemplateClaim],
        retrieve: RetrievalProvider,
    ) -> HarnessExecutionResult:
        if schema.execution_mode not in {"internal_harness", "external_agent"} or work_order.execution_mode not in {
            "internal_harness", "external_agent"
        }:
            raise ValueError("authoring graph requires a Harness-backed work order and schema")
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
            "completed_units": 0,
            "total_units": len(semantic_units),
        }

        try:
            self._step(state, "create_information_requirements")
            for unit in semantic_units:
                requirement = _requirement_for_unit(unit, work_order, snapshot)
                result.requirements[unit["unit_id"]] = requirement

            if len(semantic_units) > 1 and self.policy.policy.max_parallel_units > 1:
                def publish_completed_unit(
                    unit_id: str,
                    unit_result: HarnessExecutionResult,
                ) -> None:
                    state["step_count"] += unit_result.step_count
                    state["retrieval_round_count"] += unit_result.retrieval_round_count
                    state["completed_units"] += 1
                    self.policy.require_step(state["step_count"])
                    self.policy.require_retrieval_round(state["retrieval_round_count"])
                    state["current_node"] = "parallel_units"
                    if self.on_progress is not None:
                        self.on_progress(state)

                unit_results = self._run_parallel_units(
                    semantic_units, work_order, harness_run, run_manifest, schema,
                    snapshot, legacy_claims, retrieve, on_completed=publish_completed_unit,
                )
                for unit in semantic_units:
                    unit_result = unit_results[unit["unit_id"]]
                    result.requirements.update(unit_result.requirements)
                    result.outcomes.update(unit_result.outcomes)
                    result.matrix_rows.extend(unit_result.matrix_rows)
                    result.retrieval_ledger.extend(unit_result.retrieval_ledger)
                    result.drafts.extend(unit_result.drafts)
                    result.unit_statuses.update(unit_result.unit_statuses)
                    result.issues.extend(unit_result.issues)
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

            for unit_id, requirement in result.requirements.items():
                outcome = self._retrieve_with_budget(state, requirement, retrieve)
                result.outcomes[unit_id] = outcome
                # Read the low_confidence signal from the raw outcome evidences
                # (which retain metadata) before _validated_evidence strips it
                # on the project path.
                recovery_triggered = any(
                    _evidence_low_confidence(evidence_obj)
                    for evidence_obj in outcome.evidences
                )
                evidence = _validated_evidence(work_order, snapshot, outcome)
                preselected_evidence, preselected_discarded_evidence_ids = _select_field_evidence(
                    evidence,
                    _max_evidence_items(unit_id, schema),
                    preserve_rerank_order=False,
                    retrieval_query_terms=requirement.retrieval_query_terms,
                )
                fast_path = (
                    not recovery_triggered
                    and getattr(
                        getattr(self.writer, "provider", self.writer),
                        "provider_id",
                        None,
                    ) == DeterministicEvidenceWriter.provider_id
                    and _use_deterministic_evidence_writer(
                        unit_id, schema, requirement, preselected_evidence,
                    )
                )
                if fast_path:
                    evidence = preselected_evidence
                    discarded_evidence_ids = preselected_discarded_evidence_ids
                elif evidence and self.reranker is not None:
                    # Rerank (P6): reorder validated evidence by requirement
                    # relevance before the writer. Gated by the allowlist; an
                    # old policy without rerank_evidence never injects a
                    # reranker, and require_tool defends a mismatched injection.
                    self._step(state, "rerank_evidence")
                    self.policy.require_tool("rerank_evidence")
                    evidence = self.reranker.rerank(requirement, evidence)
                evidence, discarded_evidence_ids = _select_field_evidence(
                    evidence,
                    _max_evidence_items(unit_id, schema),
                    preserve_rerank_order=self.reranker is not None,
                    retrieval_query_terms=requirement.retrieval_query_terms,
                ) if not fast_path else (evidence, discarded_evidence_ids)
                # Retrieval ledger (P9): per-unit observability row surfaced in
                # the matrix (for human review) and on the result, so it is no
                # longer dropped when run() returns.
                ledger_row = _retrieval_ledger_row(
                    unit_id, requirement, outcome, evidence, state, recovery_triggered,
                    discarded_evidence_ids,
                )
                result.retrieval_ledger.append(ledger_row)
                result.matrix_rows.append({
                    "field_id": unit_id.removeprefix("field:") if unit_id.startswith("field:") else None,
                    "review_item_id": unit_id.removeprefix("review:") if unit_id.startswith("review:") else None,
                    "requirement_id": requirement.requirement_id,
                    "coverage_status": _coverage_status(outcome),
                    "evidence_ids": [entry["id"] for entry in evidence],
                    "display_value": None,
                    "diagnostics": [source.model_dump(mode="json") for source in outcome.source_outcomes],
                    "retrieval_ledger": ledger_row,
                })
                if not evidence:
                    result.unit_statuses[unit_id] = _missing_status(unit_id, schema, outcome)
                    continue
                self._step(state, "draft_ready_unit")
                self.policy.require_tool("draft_ready_unit")
                request = build_writer_request(
                    work_order=work_order,
                    harness_run=harness_run,
                    unit_id=unit_id,
                    schema=schema,
                    requirement=requirement,
                    evidence=evidence,
                    prompt_version=self.policy.policy.prompt_version,
                )
                draft = (
                    DeterministicEvidenceWriter().generate(request)
                    if fast_path
                    else self.draft_provider(request)
                )
                self._step(state, "validate_unit_draft")
                self.policy.require_tool("validate_unit_draft")
                evidence_by_id = {entry["id"]: entry for entry in evidence}
                validated = self.validator.validate_unit_draft(draft, evidence_by_id)
                if unit_id.startswith("field:"):
                    validated = self.validator.validate_typed_field_draft(
                        validated,
                        evidence_by_id,
                        expected_value_type=_unit_value_type(unit_id, schema),
                    )
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
                elif self.fit_checker is not None:
                    # Requirement fit check (P10): LLM judges whether the draft
                    # actually answers the requirement. Gated by the allowlist;
                    # an old policy without requirement_fit_check never injects
                    # a fit_checker, and require_tool defends a mismatched
                    # injection. Degrades to a pass verdict on LLM failure.
                    self._step(state, "requirement_fit_check")
                    self.policy.require_tool("requirement_fit_check")
                    verdict = self.fit_checker.check(validated, requirement)
                    if not verdict["fit"]:
                        validated = validated.model_copy(update={
                            "validation_status": "requires_human",
                            "validation_notes": [*validated.validation_notes, f"requirement fit check: {verdict['reason']}"],
                        })
                        result.issues.append({
                            "kind": "requirement_fit_failed", "unit_id": unit_id,
                            "reason": verdict["reason"],
                        })
                        result.unit_statuses[unit_id] = "requires_human"
                    else:
                        result.unit_statuses[unit_id] = "ready_to_render"
                else:
                    result.unit_statuses[unit_id] = "ready_to_render"
                if recovery_triggered and evidence:
                    # Stage 5: evidence recovered via a relaxed balanced-route
                    # retry is low-confidence. Route it to human review even if
                    # the draft otherwise validated as supported, rather than
                    # auto-rendering a draft built on a relaxed-scope retrieve.
                    if validated.validation_status == "supported":
                        validated = validated.model_copy(update={
                            "validation_status": "requires_human",
                            "validation_notes": [*validated.validation_notes, "low-confidence recovery evidence"],
                        })
                    result.unit_statuses[unit_id] = "requires_human"
                    result.issues.append({
                        "kind": "low_confidence_recovery", "unit_id": unit_id,
                        "reason": "evidence recovered via balanced-route retry",
                    })
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

    def _run_parallel_units(
        self,
        semantic_units: list[dict[str, Any]],
        work_order: DocumentWorkOrder,
        harness_run: HarnessRun,
        run_manifest: AuthoringRunManifest,
        schema: DocumentSchema,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        legacy_claims: list[LegacyTemplateClaim],
        retrieve: RetrievalProvider,
        on_completed: Callable[[str, HarnessExecutionResult], None] | None = None,
    ) -> dict[str, HarnessExecutionResult]:
        def run_one(unit: dict[str, Any]) -> tuple[str, HarnessExecutionResult]:
            unit_schema = schema.model_copy(update={
                "fields": [unit["schema"]] if unit["kind"] == "field" else [],
                "review_items": [unit["schema"]] if unit["kind"] == "review" else [],
            })
            graph = AuthoringGraph(
                self.policy, self.writer, self.validator,
                draft_provider=self.draft_provider, rewriter=self.rewriter,
                reranker=self.reranker, fit_checker=self.fit_checker,
            )
            return unit["unit_id"], graph._run_legacy(
                work_order=work_order, harness_run=harness_run, run_manifest=run_manifest,
                schema=unit_schema, snapshot=snapshot, legacy_claims=legacy_claims,
                retrieve=retrieve,
            )

        workers = min(self.policy.policy.max_parallel_units, len(semantic_units))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="authoring-unit") as executor:
            futures = [executor.submit(run_one, unit) for unit in semantic_units]
            results: dict[str, HarnessExecutionResult] = {}
            for future in as_completed(futures):
                unit_id, unit_result = future.result()
                results[unit_id] = unit_result
                if on_completed is not None:
                    on_completed(unit_id, unit_result)
            return results

    def _retrieve_with_budget(
        self,
        state: DocumentAuthoringState,
        requirement: InformationRequirement,
        retrieve: RetrievalProvider,
    ) -> RetrievalOutcome:
        self.policy.require_tool("retrieve_evidence")
        original_query = _query_string(requirement)
        last: RetrievalOutcome | None = None
        for attempt in range(1, self.policy.policy.max_retrieval_attempts_per_unit + 1):
            self._step(state, "retrieve_requirement_evidence")
            state["retrieval_round_count"] += 1
            self.policy.require_retrieval_round(state["retrieval_round_count"])
            if attempt == 1:
                outcome = retrieve(requirement, attempt, None)
            elif last is not None and last.status == "success_empty":
                # An empty success means the query missed, not that the source
                # failed; rewrite and retry once before giving up.
                override = self._rewrite_for_retry(state, requirement, original_query)
                outcome = retrieve(requirement, attempt, override)
            else:
                # Hard-failure statuses retry with the original query.
                outcome = retrieve(requirement, attempt, None)
            last = outcome
            if outcome.status not in {
                "retrieval_failed", "source_unavailable", "access_denied",
                "partial_failure", "success_empty",
            }:
                return outcome
        assert last is not None
        # Adaptive recovery (stage 5, P3 extreme): attempts exhausted on an
        # empty success. If the policy allows it, make one balanced-route
        # retrieve (relaxed=True) that drops the source_group hard filter
        # while keeping the frozen source_names scope, so a mis-routed query
        # can still reach frozen sources. Recovered evidence is tagged
        # low_confidence and routed to human review rather than leaving the
        # field blocked. This uses its own budget (max_adaptive_recovery_rounds)
        # and does NOT call require_retrieval_round, so it never collides with
        # max_retrieval_rounds. Only success_empty triggers it: hard failures
        # mean the source is unavailable and a balanced retry cannot help.
        if (
            "adaptive_recovery" in self.policy.policy.allowed_tools
            and last.status == "success_empty"
            and self.policy.policy.max_adaptive_recovery_rounds > 0
        ):
            self._step(state, "adaptive_recovery")
            self.policy.require_tool("adaptive_recovery")
            recovery = retrieve(requirement, attempt + 1, None, relaxed=True)
            if recovery.status == "success_with_hits" and recovery.evidences:
                _tag_low_confidence(recovery)
                last = recovery
        return last

    def _rewrite_for_retry(
        self,
        state: DocumentAuthoringState,
        requirement: InformationRequirement,
        original_query: str,
    ) -> str | None:
        if self.rewriter is None:
            return None
        # Gate the LLM rewrite on the policy allowlist. A frozen old policy
        # without rewrite_query never reaches here (rewriter is None), but the
        # guard defends against mismatched injection.
        self.policy.require_tool("rewrite_query")
        ledger = state.setdefault("retrieval_ledger", [])
        already_rewritten = sum(
            1
            for row in ledger
            if row.get("unit_id") == requirement.semantic_unit_id
            and row.get("rewrite") is not None
        )
        if already_rewritten >= self.policy.policy.max_query_rewrite_rounds:
            # Budget exhausted; do not rewrite again, fall back to original.
            return None
        self._step(state, "rewrite_query")
        try:
            rewritten = self.rewriter.rewrite(requirement)
        except Exception:
            rewritten = None
        ledger.append({
            "unit_id": requirement.semantic_unit_id,
            "original_query": original_query,
            "rewrite": rewritten,
            "attempt": 2,
        })
        return rewritten

    def _step(self, state: DocumentAuthoringState, node: str) -> None:
        state["current_node"] = node
        state["step_count"] += 1
        self.policy.require_step(state["step_count"])
        if self.on_progress is not None:
            self.on_progress(state)


def _semantic_units(schema: DocumentSchema) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for field_schema in schema.fields:
        if field_schema.authoring_policy in {"managed_writer", "external_agent_draft"}:
            units.append({"unit_id": f"field:{field_schema.field_id}", "kind": "field", "schema": field_schema})
    for item in schema.review_items:
        if item.evaluation_mode == "semantic_assisted":
            units.append({"unit_id": f"review:{item.review_item_id}", "kind": "review", "schema": item})
    return units


_ELECTRICAL_FACT_TERMS = (
    "edf", "电路", "原理图", "位号", "管脚", "引脚", "pin", "net", "网络", "连接器",
    "connector", "器件", "型号", "model", "part number", "datasheet", "数据手册", "mcu", "can",
)
_CIRCUIT_METADATA_KEYS = {
    "pin_mappings", "net_mappings", "device_mappings", "edf_parse_result", "edf_relation",
}


def _use_deterministic_evidence_writer(
    unit_id: str,
    schema: DocumentSchema,
    requirement: InformationRequirement,
    evidence: list[dict[str, Any]],
) -> bool:
    """Return true only for one directly grounded electrical fact.

    This guard is intentionally narrow: evidence must be uniquely selected and
    either originate from the EDF/circuit structured route or contain a direct
    assignment tied to a field query anchor.  All ambiguous prose retains the
    normal rerank-and-managed-writer path.
    """
    if not unit_id.startswith("field:") or len(evidence) != 1:
        return False
    field_text = " ".join((
        _unit_label(unit_id, schema),
        _unit_description(unit_id, schema),
        *requirement.retrieval_query_terms,
    )).casefold()
    if not any(term in field_text for term in _ELECTRICAL_FACT_TERMS):
        return False
    item = evidence[0]
    metadata = item.get("metadata") or {}
    if (
        str(metadata.get("source_group") or "").casefold() == "circuit_design"
        or any(metadata.get(key) for key in _CIRCUIT_METADATA_KEYS)
    ):
        return True
    content = str(item.get("content") or "")
    anchors = _field_specific_query_terms(list(requirement.retrieval_query_terms))
    return any(
        re.search(rf"{re.escape(anchor)}\s*(?:[:：=]|为|\bis\b)", content, re.IGNORECASE)
        for anchor in anchors
    )


def _max_evidence_items(unit_id: str, schema: DocumentSchema) -> int:
    if unit_id.startswith("field:"):
        field_id = unit_id.removeprefix("field:")
        for field in schema.fields:
            if field.field_id == field_id:
                return field.max_evidence_items
    return 5


def _select_field_evidence(
    evidence: list[dict[str, Any]],
    max_items: int,
    *,
    preserve_rerank_order: bool,
    retrieval_query_terms: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rank, deduplicate, and bound evidence without widening frozen scope.

    A broad retrieval route can return a whole page or table for several
    fields.  Such a chunk is not a safe scalar value unless it contains a
    field-specific query anchor.  Filtering only long, unanchored chunks here
    keeps short direct facts available while preventing a generic page from
    being copied into many worksheet cells.
    """
    ranked = list(evidence) if preserve_rerank_order else sorted(
        evidence,
        key=lambda item: (
            0 if (item.get("metadata") or {}).get("preferred_source_role_match") else 1,
            -float(item.get("score") or 0),
            str(item.get("id") or ""),
        ),
    )
    unique: list[dict[str, Any]] = []
    discarded: list[str] = []
    seen_content: set[str] = set()
    specific_terms = _field_specific_query_terms(retrieval_query_terms or [])
    for item in ranked:
        content = str(item.get("content") or "").strip()
        evidence_id = str(item.get("id") or "")
        if (
            len(content) >= 80
            and specific_terms
            and not _has_query_anchor(content, specific_terms)
        ):
            if evidence_id:
                discarded.append(evidence_id)
            continue
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_hash in seen_content:
            if evidence_id:
                discarded.append(evidence_id)
            continue
        seen_content.add(content_hash)
        unique.append(item)
    selected = unique[:max_items]
    discarded.extend(str(item.get("id") or "") for item in unique[max_items:] if item.get("id"))
    return selected, discarded


_GENERIC_RETRIEVAL_TERMS = {
    "data", "detail", "information", "net", "pin", "power", "signal",
    "text", "value", "ground", "连接", "信号", "信息", "数据", "引脚",
    "电源", "网络", "内容", "字段", "详情",
}


def _field_specific_query_terms(query_terms: list[str]) -> list[str]:
    """Keep discriminating lexical anchors and drop broad routing words."""
    result: list[str] = []
    seen: set[str] = set()
    for term in query_terms:
        normalized = str(term or "").strip()
        folded = normalized.casefold()
        if not normalized or folded in _GENERIC_RETRIEVAL_TERMS or folded in seen:
            continue
        # Single ASCII letters and digits are too broad to establish that a
        # long result belongs to this field.  Identifiers (for example
        # X1903-22) and meaningful Chinese/word labels are retained.
        if len(normalized) < 2:
            continue
        result.append(normalized)
        seen.add(folded)
    return result


def _has_query_anchor(content: str, query_terms: list[str]) -> bool:
    folded_content = content.casefold()
    return any(term.casefold() in folded_content for term in query_terms)


def _requirement_for_unit(
    unit: dict[str, Any],
    work_order: DocumentWorkOrder,
    snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
) -> InformationRequirement:
    schema = unit["schema"]
    if unit["kind"] == "field":
        capability = _capabilities(
            enrich_circuit_capabilities(
                schema.required_capabilities,
                label=schema.label,
                description=schema.description,
                query_terms=schema.query_terms,
            )
        )
        source_roles = schema.preferred_source_roles
        subject = schema.label
        predicate = schema.description or None
        missing_policy = schema.missing_policy
        claim_type = "attribute"
        query_terms = _unique_query_terms(
            [
                *getattr(schema, "subject_aliases", []),
                *schema.query_terms,
                schema.description,
                schema.label,
            ]
        )
    else:
        capability = _capabilities(schema.required_capabilities)
        source_roles = schema.required_source_roles
        subject = schema.label
        predicate = "review"
        missing_policy = "mark_tbd"
        claim_type = "requirement"
        query_terms = _unique_query_terms([schema.label, "review"])
    requirement_id = hashlib.sha256(
        f"{work_order.work_order_id}|{unit['unit_id']}|{work_order.input_fingerprint}".encode("utf-8")
    ).hexdigest()[:24]
    return InformationRequirement(
        requirement_id=f"req-{requirement_id}", semantic_unit_id=unit["unit_id"],
        claim_type=claim_type, subject=subject, predicate=predicate,
        retrieval_query_terms=query_terms,
        required_capabilities=capability, preferred_source_roles=source_roles,
        project_id=work_order.project_id, baseline_id=work_order.baseline_id,
        source_version_scope=list(
            snapshot.source_names
            if work_order.scope_type == "knowledge_base"
            else snapshot.source_version_ids
        ),
        missing_policy=missing_policy,
    )


def _capabilities(values: list[str]) -> list[str]:
    from src.document_authoring.contract_registry import supported_capabilities

    supported, unsupported = supported_capabilities(list(values))
    if unsupported:
        import logging

        logging.getLogger(__name__).warning(
            "unsupported capabilities in requirement: %s (dropped after diagnostics)",
            unsupported,
        )
    return supported


def _query_string(requirement: InformationRequirement) -> str:
    if requirement.retrieval_query_terms:
        return " ".join(requirement.retrieval_query_terms)
    return " ".join(
        value
        for value in (requirement.subject, requirement.predicate, requirement.object_hint)
        if value
    )


def _unique_query_terms(values: list[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized.casefold() not in seen:
            result.append(normalized)
            seen.add(normalized.casefold())
    return result


def _retrieval_ledger_row(
    unit_id: str,
    requirement: InformationRequirement,
    outcome: RetrievalOutcome,
    evidence: list[dict[str, Any]],
    state: DocumentAuthoringState,
    recovery_triggered: bool = False,
    discarded_evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a per-unit retrieval observability row (P9).

    Surfaces the query, any rewrites, per-source hit counts, whether a RAGFlow
    fallback was triggered, and the final (post-rerank) evidence ids so a human
    reviewer can see why a field is empty. ``fallback_triggered`` is read from
    ``outcome.evidences`` (which retain metadata), not the validated dicts --
    the project path strips metadata during validation. ``recovery_triggered``
    records whether stage 5 adaptive recovery supplied the evidence.
    """
    rewrites = [
        entry["rewrite"]
        for entry in state.get("retrieval_ledger", [])
        if entry.get("unit_id") == unit_id and entry.get("rewrite")
    ]
    per_source = [
        {
            "source": source.source_version_id,
            "status": source.status,
            "hit_count": len(source.evidence_ids),
        }
        for source in outcome.source_outcomes
    ]
    return {
        "unit_id": unit_id,
        "original_query": _query_string(requirement),
        "rewrites": rewrites,
        "per_source": per_source,
        "fallback_triggered": any(
            _evidence_fallback(evidence_obj) for evidence_obj in outcome.evidences
        ),
        "final_evidence_ids": [entry["id"] for entry in evidence],
        "discarded_evidence_ids": list(discarded_evidence_ids or []),
        "recovery_triggered": recovery_triggered,
        "recovery_reason": "balanced_route_retry" if recovery_triggered else None,
    }


def _evidence_fallback(evidence: Any) -> bool:
    """True if a RAGFlow source-group fallback flag is set on the evidence."""
    metadata = getattr(evidence, "metadata", None) or {}
    return bool(
        metadata.get("ragflow_source_name_fallback")
        or metadata.get("ragflow_metadata_condition_fallback")
    )


def _evidence_low_confidence(evidence: Any) -> bool:
    """True if stage 5 adaptive recovery tagged the evidence low-confidence."""
    metadata = getattr(evidence, "metadata", None) or {}
    return bool(metadata.get("low_confidence"))


def _tag_low_confidence(outcome: RetrievalOutcome) -> None:
    """Tag every evidence of a recovery outcome as low-confidence (stage 5)."""
    for evidence in outcome.evidences:
        metadata = dict(getattr(evidence, "metadata", {}) or {})
        metadata["low_confidence"] = True
        try:
            evidence.metadata = metadata
        except Exception:  # pragma: no cover - defensive for non-model objects
            pass


def _validated_evidence(
    work_order: DocumentWorkOrder,
    snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
    outcome: RetrievalOutcome,
) -> list[dict[str, Any]]:
    if outcome.applied_source_set_snapshot_id != snapshot.source_set_snapshot_id:
        raise PermissionError("harness retrieval outcome source-set mismatch")
    if work_order.scope_type == "knowledge_base":
        if outcome.applied_region_policy_versions:
            raise PermissionError(
                "knowledge base harness outcome used unexpected region policies"
            )
        if outcome.status not in {"success_with_hits", "success_empty"}:
            return []
        result: list[dict[str, Any]] = []
        for evidence in outcome.evidences:
            if (
                evidence.metadata.get("knowledge_base_name")
                != work_order.knowledge_base_name
                or evidence.source_name not in snapshot.source_names
            ):
                raise PermissionError(
                    "harness received evidence outside the frozen source set"
                )
            result.append(
                {
                    "id": evidence.id,
                    "content": evidence.content,
                    "source_name": evidence.source_name,
                    "score": getattr(evidence, "score", 0),
                    "metadata": dict(evidence.metadata),
                    "locator": dict(getattr(evidence, "locator", {})),
                    "fact_type": getattr(evidence, "fact_type", None),
                }
            )
        return result
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
            "score": getattr(evidence, "score", 0),
            "source_version_id": getattr(evidence, "source_version_id", None),
            "processing_artifact_id": getattr(evidence, "processing_artifact_id", None),
            "metadata": dict(getattr(evidence, "metadata", {}) or {}),
            "locator": dict(getattr(evidence, "locator", {})),
            "fact_type": getattr(evidence, "fact_type", None),
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


def _unit_value_type(unit_id: str, schema: DocumentSchema) -> str:
    if unit_id.startswith("field:"):
        return next(
            item.value_type
            for item in schema.fields
            if item.field_id == unit_id.removeprefix("field:")
        )
    return "text"


# ── Task 3: GenerationBrief -> WriterRequest passthrough ────────────────────


def build_writer_request(
    *,
    work_order,
    harness_run,
    unit_id: str,
    schema,
    requirement: InformationRequirement,
    evidence: list[dict[str, Any]],
    prompt_version: str,
) -> WriterRequest:
    """Assemble the WriterRequest, translating the confirmed brief into
    canonical writer constraints. Work orders without a brief keep the legacy
    empty-constraint behaviour; raw user text never enters the request.
    """
    from src.document_authoring.harness.agent_contracts import (
        effective_missing_policy,
        normalize_clarification_policy,
    )

    brief = dict(getattr(work_order, "generation_brief", {}) or {})
    confirmed = bool(brief.get("confirmed"))
    missing_or_conflicts: list[dict[str, Any]] = []
    allowed_derivations: list[dict[str, Any]] = []
    if confirmed:
        brief_missing = normalize_clarification_policy(
            "missing_data_policy", brief.get("missing_data_policy")
        )
        inference_policy = normalize_clarification_policy(
            "inference_policy", brief.get("inference_policy")
        )
        if brief_missing:
            missing_or_conflicts.append({
                "kind": "missing_data_policy", "policy": brief_missing,
            })
        field = _field_for_unit(unit_id, schema)
        if field is not None:
            effective = effective_missing_policy(brief_missing, field.missing_policy)
            if effective:
                missing_or_conflicts.append({
                    "kind": "effective_missing_policy", "policy": effective,
                })
            if (
                inference_policy
                and inference_policy != "forbid"
                and field.allow_derivation
            ):
                allowed_derivations.append({
                    "kind": "labeled_inference", "policy": inference_policy,
                })
    return WriterRequest(
        work_order_id=work_order.work_order_id,
        run_id=harness_run.harness_run_id,
        unit_id=unit_id,
        unit_label=_unit_label(unit_id, schema),
        unit_description=_unit_description(unit_id, schema),
        field_value_type=_unit_value_type(unit_id, schema),
        retrieval_query_terms=list(requirement.retrieval_query_terms),
        evidence=evidence,
        allowed_derivations=allowed_derivations,
        missing_or_conflicts=missing_or_conflicts,
        prompt_version=prompt_version,
    )


def _field_for_unit(unit_id: str, schema):
    if unit_id.startswith("field:"):
        field_id = unit_id.removeprefix("field:")
        return next((item for item in schema.fields if item.field_id == field_id), None)
    return None


def build_writer_system_prompt(request: WriterRequest) -> str:
    """Fixed-boundary instruction block generated only from canonical enums."""
    missing_policies = [
        str(item.get("policy"))
        for item in request.missing_or_conflicts
        if item.get("kind") in {"missing_data_policy", "effective_missing_policy"}
    ]
    derivations = [
        str(item.get("policy"))
        for item in request.allowed_derivations
        if item.get("kind") == "labeled_inference"
    ]
    lines: list[str] = []
    if missing_policies:
        lines.append("Missing data policy (canonical): " + ", ".join(missing_policies))
    if derivations:
        lines.append("Inference policy (canonical): " + ", ".join(derivations))
    if not lines:
        return ""
    return (
        "<<USER_CONSTRAINTS>>\n"
        + "\n".join(lines)
        + "\n<<END_USER_CONSTRAINTS>>"
    )
