"""Persistence-aware internal Harness runtime for P2b."""

from __future__ import annotations

import uuid
import time
import threading
import os
from datetime import datetime, timezone
from hashlib import sha256
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from src.document_authoring.harness.graph import (
    AuthoringGraph,
    HarnessExecutionResult,
    RetrievalProvider,
    PlanExecutionRouteError,
    select_execution_route,
    _coverage_status,
    _missing_status,
    _retrieval_ledger_row,
    _select_field_evidence,
    _validated_evidence,
    _requirement_for_unit,
    _unit_value_type,
    build_writer_request,
)
from src.document_authoring.harness.agent_loop import (
    AgentFieldHarness,
    HarnessExecutionContext,
    InternalGraphExecutor,
    select_harness_executor,
)
from src.document_authoring.harness.checkpointer import FencedCheckpointer, build_checkpointer
from src.document_authoring.harness.policy import HarnessLeaseLost, HarnessToolPolicy
from src.document_authoring.models import (
    AuthoringRunManifest,
    AuthoringExecutionEvent,
    DocumentUnitDraft,
    DocumentSchema,
    DocumentFieldSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
    LegacyTemplateClaim,
    NodeExecutionReceipt,
    EvidenceRegistryEntry,
    TemplateVersion,
    content_hash,
)
from src.document_authoring.harness.idempotency import execution_event_key, receipt_action_key
from src.document_authoring.harness.plan_graph import PlanDAGExecutor
from src.document_authoring.harness.plan_graph import PlanDAGExecutionResult
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.planning.models import DocumentPlan
from src.document_authoring.planning.task_graph import (
    CompiledTaskGraph,
    CompiledTaskNode,
    TaskGraphCompiler,
)
from src.document_authoring.planning.task_graph_store import TaskGraphStore
from src.document_authoring.writers.managed import ManagedWriter
from src.projects.models import SourceSetSnapshot
from src.observability import observe
from src.observability.metrics import record_authoring_unit
import src.settings

if TYPE_CHECKING:
    from src.document_authoring.writers.evidence_reranker import EvidenceReranker
    from src.document_authoring.writers.query_rewriter import QueryRewriter
    from src.document_authoring.writers.requirement_fit_checker import RequirementFitChecker


def _allowlist_values(value: Any) -> set[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = []
    return {
        str(item).strip().casefold()
        for item in values
        if str(item).strip()
    }


def _allowlisted(value: Any, configured: Any) -> bool:
    allowed = _allowlist_values(configured)
    normalized = str(value or "").strip().casefold()
    return bool(allowed) and ("*" in allowed or normalized in allowed)


@dataclass
class AuthoringRunContext:
    """Public capability boundary exposed to graph/executor nodes.

    The runtime remains the only owner of lease, receipt and business-store
    side effects.  Nodes receive these narrow operations instead of closures
    hidden inside ``execute``; the methods are intentionally callback-backed so
    tests can use an in-memory context without constructing a database.
    """

    save_progress_callback: Callable[[Any], None]
    heartbeat_callback: Callable[[], Any]
    draft_with_receipt_callback: Callable[[Any], DocumentUnitDraft]
    observed_retrieve_callback: Callable[..., Any]
    record_telemetry_callback: Callable[..., None] | None = None
    finalize_callback: Callable[..., Any] | None = None

    def save_progress(self, state: Any) -> None:
        self.save_progress_callback(state)

    def heartbeat(self) -> Any:
        return self.heartbeat_callback()

    def draft_with_receipt(self, request: Any) -> DocumentUnitDraft:
        return self.draft_with_receipt_callback(request)

    def observed_retrieve(self, *args: Any, **kwargs: Any) -> Any:
        return self.observed_retrieve_callback(*args, **kwargs)

    def record_telemetry(self, *args: Any, **kwargs: Any) -> None:
        if self.record_telemetry_callback is not None:
            self.record_telemetry_callback(*args, **kwargs)

    def finalize(self, *args: Any, **kwargs: Any) -> Any:
        if self.finalize_callback is None:
            return None
        return self.finalize_callback(*args, **kwargs)


class InternalDocumentHarnessRuntime:
    def __init__(
        self,
        store: DocumentAuthoringStore,
        validator: DocumentValidator | None = None,
        *,
        checkpointer: FencedCheckpointer | None = None,
        checkpointer_factory: Callable[..., FencedCheckpointer] | None = None,
    ):
        self.store = store
        self.validator = validator or DocumentValidator()
        self.checkpointer = checkpointer
        self.checkpointer_factory = checkpointer_factory
        self.task_graphs = TaskGraphStore(store.db_path)

    def _bind_plan_graph(
        self,
        work_order: DocumentWorkOrder,
        *,
        document_plan: DocumentPlan | None = None,
        compiled_graph: CompiledTaskGraph | None = None,
    ) -> tuple[str, DocumentPlan | None, CompiledTaskGraph | None]:
        route = select_execution_route(
            work_order,
            enabled=bool(getattr(src.settings, "DOCUMENT_PLAN_DAG_EXECUTION_ENABLED", False)),
        )
        if route != "plan_dag":
            return route, None, None

        plan_id = str(getattr(work_order, "document_plan_id", "") or "")
        plan_version = getattr(work_order, "document_plan_version", None)
        plan_hash = str(getattr(work_order, "document_plan_hash", "") or "")
        output_spec_id = str(getattr(work_order, "output_spec_id", "") or "")
        output_spec_version = getattr(work_order, "output_spec_version", None)
        output_spec_hash = str(getattr(work_order, "output_spec_hash", "") or "")
        planning_store = getattr(self.store, "planning", None)
        if not output_spec_id or output_spec_version is None or not output_spec_hash:
            raise PlanExecutionRouteError(
                "plan execution requires a complete OutputSpec binding"
            )
        plan = document_plan
        if plan is None:
            if planning_store is not None:
                plan = planning_store.get_plan(
                    plan_id,
                    int(plan_version),
                    tenant_id=getattr(work_order, "tenant_id", None),
                    user_id=getattr(work_order, "created_by", None),
                )
        if plan is None:
            raise PlanExecutionRouteError("accepted DocumentPlan is unavailable for plan execution")
        if (
            plan.document_plan_id != plan_id
            or plan.version != int(plan_version)
            or plan.plan_hash != plan_hash
            or plan.status != "accepted"
        ):
            raise PlanExecutionRouteError("accepted DocumentPlan identity or status does not match Work Order")
        if planning_store is None:
            raise PlanExecutionRouteError("accepted OutputSpec is unavailable for plan execution")
        output_spec = planning_store.get_output_spec(
            output_spec_id,
            int(output_spec_version),
            tenant_id=getattr(work_order, "tenant_id", None),
            user_id=getattr(work_order, "created_by", None),
        )
        if output_spec is None or output_spec.status != "accepted":
            raise PlanExecutionRouteError(
                "accepted OutputSpec is unavailable for plan execution"
            )
        if (
            output_spec.output_spec_id != output_spec_id
            or output_spec.version != int(output_spec_version)
            or output_spec.content_hash != output_spec_hash
            or plan.output_spec_id != output_spec.output_spec_id
            or plan.output_spec_version != output_spec.version
            or plan.output_spec_hash != output_spec.content_hash
        ):
            raise PlanExecutionRouteError(
                "accepted OutputSpec identity or hash does not match the Work Order and plan"
            )
        primary_deliverables = [
            item for item in output_spec.artifact.deliverables
            if item.role == "primary"
        ]
        if len(primary_deliverables) != 1:
            raise PlanExecutionRouteError(
                "accepted OutputSpec must declare exactly one primary deliverable"
            )
        primary_format = primary_deliverables[0].format
        if primary_format != getattr(work_order, "target_format", None):
            raise PlanExecutionRouteError(
                "accepted OutputSpec primary format does not match the Work Order"
            )
        if not (
            _allowlisted(
                getattr(work_order, "tenant_id", None),
                getattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS", ()),
            )
            and _allowlisted(
                output_spec.document_type,
                getattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES", ()),
            )
            and _allowlisted(
                primary_format,
                getattr(src.settings, "DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS", ()),
            )
        ):
            raise PlanExecutionRouteError(
                "plan execution scope is not present in the configured allowlist"
            )

        graph = compiled_graph or TaskGraphCompiler().compile(plan)
        expected_graph_id = f"document-plan:{plan.document_plan_id}:v{plan.version}"
        if graph.graph_id != expected_graph_id or graph.plan_hash != plan.plan_hash:
            raise PlanExecutionRouteError("compiled task graph is not bound to the accepted DocumentPlan")
        persisted = self.task_graphs.put(
            graph,
            tenant_id=getattr(work_order, "tenant_id", "default"),
            user_id=getattr(work_order, "created_by", "system"),
            task_id=getattr(work_order, "task_id", None),
        )
        return route, plan, persisted

    def _load_bound_plan_graph(
        self,
        work_order: DocumentWorkOrder,
        run: HarnessRun,
        manifest: AuthoringRunManifest,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
    ) -> tuple[DocumentPlan, CompiledTaskGraph]:
        """Reload the exact accepted plan and graph recorded by the run.

        Compilation is a create-time operation.  An executing worker never
        recompiles from mutable planning input: it reloads the immutable rows
        by all recorded identities and fails closed on any mismatch.
        """
        if manifest.execution_route != "plan_dag" or run.execution_route != "plan_dag":
            raise PlanExecutionRouteError("plan graph execution requires a plan_dag run manifest")
        expected = {
            "plan_id": getattr(work_order, "document_plan_id", None),
            "plan_version": getattr(work_order, "document_plan_version", None),
            "plan_hash": getattr(work_order, "document_plan_hash", None),
            "graph_id": getattr(manifest, "task_graph_id", None),
            "graph_version": getattr(manifest, "task_graph_version", None),
            "graph_hash": getattr(manifest, "task_graph_hash", None),
        }
        if any(value in (None, "") for value in expected.values()):
            raise PlanExecutionRouteError("plan run is missing an immutable graph binding")
        if any(
            getattr(run, field, None) != value
            for field, value in (
                ("task_graph_id", expected["graph_id"]),
                ("task_graph_version", expected["graph_version"]),
                ("task_graph_hash", expected["graph_hash"]),
                ("document_plan_id", expected["plan_id"]),
                ("document_plan_version", expected["plan_version"]),
                ("document_plan_hash", expected["plan_hash"]),
            )
        ):
            raise PlanExecutionRouteError("HarnessRun graph identity differs from its manifest")
        if manifest.source_set_snapshot_id != snapshot.source_set_snapshot_id:
            raise PlanExecutionRouteError("run manifest source snapshot differs from execution snapshot")
        if work_order.source_set_snapshot_id != snapshot.source_set_snapshot_id:
            raise PlanExecutionRouteError("work order source snapshot differs from execution snapshot")

        planning_store = getattr(self.store, "planning", None)
        plan = (
            planning_store.get_plan(
                str(expected["plan_id"]),
                int(expected["plan_version"]),
                tenant_id=getattr(work_order, "tenant_id", None),
                user_id=getattr(work_order, "created_by", None),
            )
            if planning_store is not None
            else None
        )
        if plan is None or plan.status != "accepted":
            raise PlanExecutionRouteError("accepted DocumentPlan is unavailable during execution")
        if (
            plan.document_plan_id != expected["plan_id"]
            or plan.version != int(expected["plan_version"])
            or plan.plan_hash != expected["plan_hash"]
            or plan.source_snapshot_id != snapshot.source_set_snapshot_id
            or plan.source_snapshot_hash != snapshot.content_hash
        ):
            raise PlanExecutionRouteError("accepted plan or source snapshot no longer matches the run")

        graph = self.task_graphs.get(
            str(expected["graph_id"]),
            int(expected["graph_version"]),
            str(expected["graph_hash"]),
            tenant_id=getattr(work_order, "tenant_id", None),
            user_id=getattr(work_order, "created_by", None),
            task_id=getattr(work_order, "task_id", None),
        )
        if graph is None:
            raise PlanExecutionRouteError("compiled task graph is unavailable during execution")
        if (
            graph.plan_id != plan.document_plan_id
            or graph.plan_hash != plan.plan_hash
            or graph.source_snapshot_id != snapshot.source_set_snapshot_id
            or graph.source_snapshot_hash != snapshot.content_hash
        ):
            raise PlanExecutionRouteError("compiled graph is not bound to the frozen plan and source")
        return plan, graph

    @staticmethod
    def _plan_execution_schema(
        plan: DocumentPlan,
        original_schema: DocumentSchema,
    ) -> DocumentSchema:
        """Adapt plan semantic units to the existing typed writer boundary.

        The adapter is request-local and never persisted as a replacement
        ``DocumentSchema``.  It gives the established retrieval/writer/
        validator contracts a plan-shaped view while the accepted plan stays
        the source of truth for unit identity and table coverage.
        """
        coverage_by_unit = {
            requirement.unit_id: requirement
            for requirement in plan.coverage_contract.requirements
        }
        fields: list[DocumentFieldSchema] = []
        for unit in plan.semantic_units:
            output = dict(unit.output_schema or {})
            raw_type = str(output.get("type") or output.get("value_type") or "text").strip().casefold()
            value_type = {
                "object": "text",
                "paragraph": "text",
                "section": "text",
                "list": "enumeration",
                "array": "enumeration",
                "repeating_table": "table",
            }.get(raw_type, raw_type or "text")
            requirement = coverage_by_unit.get(unit.unit_id)
            columns = list(
                output.get("columns")
                or (requirement.required_columns if requirement is not None else [])
                or []
            )
            table_columns = {str(column): str(column) for column in columns} or None
            fields.append(DocumentFieldSchema(
                field_id=unit.unit_id,
                label=str(output.get("label") or unit.unit_id),
                description=str(output.get("description") or ""),
                required=unit.required,
                value_type=value_type,
                required_capabilities=[],
                preferred_source_roles=[],
                retrieval_policy_id=f"plan:{plan.document_plan_id}:{unit.unit_id}:retrieval",
                query_terms=[str(term) for term in output.get("query_terms", []) if str(term).strip()],
                subject_aliases=[],
                max_evidence_items=5,
                verification_policy_id=f"plan:{plan.document_plan_id}:{unit.unit_id}:verification",
                allow_derivation=False,
                missing_policy="mark_tbd",
                authoring_policy="managed_writer",
                table_columns=table_columns,
                table_row_scope=(
                    str(output.get("row_scope") or "") or None
                ),
                table_row_keys=list(requirement.row_keys) if requirement is not None else [],
                table_row_order=(
                    requirement.row_order if requirement is not None else "declared"
                ),
                table_duplicate_policy=(
                    requirement.duplicate_policy if requirement is not None else "reject"
                ),
            ))
        return DocumentSchema(
            document_schema_id=original_schema.document_schema_id,
            version=original_schema.version,
            document_type=original_schema.document_type,
            fields=fields,
            review_items=[],
            status="approved",
            execution_mode="internal_harness",
        )

    @staticmethod
    def _plan_receipt_payload(node: CompiledTaskNode, output: Any) -> dict[str, Any]:
        """Project a node result to bounded, non-evidence receipt facts."""
        if isinstance(output, dict):
            payload = dict(output.get("receipt_payload") or output)
        elif hasattr(output, "model_dump"):
            payload = output.model_dump(mode="json")
        else:
            payload = {"value": str(output)}
        payload.pop("draft", None)
        payload.pop("evidence", None)
        payload.pop("outcome", None)
        payload["node_id"] = node.node_id
        payload["kind"] = node.kind
        return {
            str(key): value
            for key, value in payload.items()
            if key not in {"content", "raw_content", "prompt", "evidence_content"}
        }

    def _persist_plan_node_receipt(
        self,
        *,
        node: CompiledTaskNode,
        graph: CompiledTaskGraph,
        output: Any,
        attempt: int,
        running: HarnessRun,
        lease_owner: str,
        persistence_lock: threading.RLock,
    ) -> Any:
        receipt = NodeExecutionReceipt(
            receipt_id=f"receipt-{uuid.uuid4().hex}",
            harness_run_id=running.harness_run_id,
            node_name="plan_node",
            unit_id=node.node_id,
            input_fingerprint=content_hash({
                "graph_hash": graph.graph_hash,
                "node_id": node.node_id,
                "action_key": node.action_key,
            }),
            action_key=node.action_key,
            attempt=attempt,
            fencing_token=running.fencing_token,
        )
        payload = self._plan_receipt_payload(node, output)
        with persistence_lock:
            started = self.store.begin_node_execution_owned(
                receipt, lease_owner, running.fencing_token,
            )
            if started.status == "committed":
                return output
            self.store.commit_node_execution_owned(
                started.receipt_id,
                running.harness_run_id,
                lease_owner,
                running.fencing_token,
                payload,
            )
        return output

    def _register_plan_evidence(
        self,
        *,
        work_order: DocumentWorkOrder,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        running: HarnessRun,
        evidence: list[dict[str, Any]],
    ) -> None:
        """Register opaque evidence handles without persisting source text."""
        for item in evidence:
            evidence_id = str(item.get("id") or "").strip()
            if not evidence_id:
                continue
            source_identity = str(
                item.get("source_version_id")
                or item.get("source_name")
                or "unknown-source"
            ).strip()
            if work_order.scope_type == "knowledge_base":
                knowledge_base_id = str(
                    getattr(work_order, "knowledge_base_id", None)
                    or getattr(work_order, "knowledge_base_name", None)
                    or "knowledge-base"
                )
                project_id = None
            else:
                knowledge_base_id = None
                project_id = str(getattr(work_order, "project_id", None) or "project")
            entry = EvidenceRegistryEntry(
                evidence_id=evidence_id,
                tenant_id=str(getattr(work_order, "tenant_id", "default") or "default"),
                harness_run_id=running.harness_run_id,
                work_order_id=work_order.work_order_id,
                knowledge_base_id=knowledge_base_id,
                project_id=project_id,
                source_set_snapshot_id=snapshot.source_set_snapshot_id,
                snapshot_content_hash=snapshot.content_hash,
                content_hash=sha256(str(item.get("content") or "").encode("utf-8")).hexdigest(),
                source_identity=source_identity,
                reload_handle=f"evidence-{uuid.uuid4().hex}",
            )
            self.store.register_evidence(entry)

    def _execute_plan_route(
        self,
        *,
        work_order: DocumentWorkOrder,
        run: HarnessRun,
        manifest: AuthoringRunManifest,
        policy: HarnessPolicy,
        schema: DocumentSchema,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        legacy_claims: list[LegacyTemplateClaim],
        writer: ManagedWriter,
        retrieve: RetrievalProvider,
        rewriter: "QueryRewriter | None",
        reranker: "EvidenceReranker | None",
        fit_checker: "RequirementFitChecker | None",
        should_cancel: Callable[[], bool] | None,
        run_context: AuthoringRunContext,
        check_lease: Callable[[], bool],
        persistence_lock: threading.RLock,
        lease_owner: str,
        plan_node_callback: Callable[[CompiledTaskNode, HarnessExecutionResult, PlanDAGExecutionResult], Any] | None = None,
    ) -> HarnessExecutionResult:
        del rewriter, reranker, fit_checker
        plan, graph = self._load_bound_plan_graph(work_order, run, manifest, snapshot)
        plan_schema = self._plan_execution_schema(plan, schema)
        tool_policy = HarnessToolPolicy(policy)
        result = HarnessExecutionResult(
            execution_route="plan_dag",
            document_plan_id=plan.document_plan_id,
            task_graph_id=graph.graph_id,
        )
        result_lock = threading.RLock()
        requirements_by_unit: dict[str, Any] = {}
        table_requirements = {
            requirement.unit_id: requirement
            for requirement in plan.coverage_contract.requirements
            if requirement.kind == "table"
        }
        field_by_unit = {field.field_id: field for field in plan_schema.fields}
        for unit in plan.semantic_units:
            unit_key = f"field:{unit.unit_id}"
            requirement = _requirement_for_unit(
                {"unit_id": unit_key, "kind": "field", "schema": field_by_unit[unit.unit_id]},
                work_order,
                snapshot,
            )
            requirements_by_unit[unit_key] = requirement
            result.requirements[unit_key] = requirement
            result.unit_statuses[unit_key] = "planned"

        committed = {
            receipt.unit_id: receipt.output_payload or {}
            for receipt in self.store.list_node_execution_receipts(
                run.harness_run_id, node_name="plan_node", status="committed",
            )
        }
        for payload in committed.values():
            if not isinstance(payload, dict) or payload.get("kind") != "unit":
                continue
            unit_id = str(payload.get("unit_id") or "").strip()
            if not unit_id:
                continue
            result.unit_statuses[unit_id] = str(payload.get("status") or "ready_to_render")
            row = payload.get("matrix_row")
            if isinstance(row, dict):
                result.matrix_rows.append(row)
            ledger = payload.get("retrieval_ledger")
            if isinstance(ledger, dict):
                result.retrieval_ledger.append(ledger)

        def step(node_name: str) -> None:
            del node_name
            check_lease()
            with result_lock:
                result.step_count += 1
                step_count = result.step_count
            tool_policy.require_step(step_count)

        def execute_unit(node: CompiledTaskNode, attempt: int) -> dict[str, Any]:
            if node.unit_id is None:
                raise PlanExecutionRouteError(f"plan unit node has no unit_id: {node.node_id}")
            unit_key = f"field:{node.unit_id}"
            field = field_by_unit.get(node.unit_id)
            if field is None:
                raise PlanExecutionRouteError(f"plan graph references unknown semantic unit: {node.unit_id}")
            requirement = requirements_by_unit[unit_key]
            step("retrieve_evidence")
            tool_policy.require_tool("retrieve_evidence")
            last = None
            for retrieval_attempt in range(1, policy.max_retrieval_attempts_per_unit + 1):
                if retrieval_attempt > 1:
                    step("retrieve_evidence_retry")
                with result_lock:
                    result.retrieval_round_count += 1
                    retrieval_round_count = result.retrieval_round_count
                tool_policy.require_retrieval_round(retrieval_round_count)
                outcome = run_context.observed_retrieve(requirement, retrieval_attempt, None)
                last = outcome
                if outcome.status not in {
                    "retrieval_failed", "source_unavailable", "access_denied",
                    "partial_failure", "success_empty",
                }:
                    break
            if last is None:
                raise PlanExecutionRouteError("plan unit retrieval produced no outcome")
            outcome = last
            evidence = _validated_evidence(work_order, snapshot, outcome)
            evidence, discarded = _select_field_evidence(
                evidence,
                getattr(field, "max_evidence_items", 5),
                preserve_rerank_order=False,
                retrieval_query_terms=requirement.retrieval_query_terms,
            )
            self._register_plan_evidence(
                work_order=work_order,
                snapshot=snapshot,
                running=run,
                evidence=evidence,
            )
            ledger = _retrieval_ledger_row(
                unit_key, requirement, outcome, evidence,
                {"retrieval_ledger": []}, False, discarded,
            )
            matrix_row = {
                "field_id": node.unit_id,
                "review_item_id": None,
                "requirement_id": requirement.requirement_id,
                "coverage_status": _coverage_status(outcome),
                "evidence_ids": [entry["id"] for entry in evidence],
                "display_value": None,
                "diagnostics": [source.model_dump(mode="json") for source in outcome.source_outcomes],
                "retrieval_ledger": ledger,
            }
            with result_lock:
                result.outcomes[unit_key] = outcome
                result.retrieval_ledger.append(ledger)
                result.matrix_rows.append(matrix_row)
            if not evidence:
                with result_lock:
                    result.unit_statuses[unit_key] = _missing_status(unit_key, plan_schema, outcome)
                return {
                    "receipt_payload": {
                        "unit_id": unit_key,
                        "status": result.unit_statuses[unit_key],
                        "evidence_ids": [],
                        "matrix_row": matrix_row,
                        "retrieval_ledger": ledger,
                    }
                }

            step("draft_ready_unit")
            tool_policy.require_tool("draft_ready_unit")
            request = build_writer_request(
                work_order=work_order,
                harness_run=run,
                unit_id=unit_key,
                schema=plan_schema,
                requirement=requirement,
                evidence=evidence,
                prompt_version=policy.prompt_version,
                table_requirement=table_requirements.get(node.unit_id),
            )
            draft = run_context.draft_with_receipt(request)
            step("validate_unit_draft")
            tool_policy.require_tool("validate_unit_draft")
            evidence_by_id = {entry["id"]: entry for entry in evidence}
            validated = self.validator.validate_unit_draft(draft, evidence_by_id)
            validated = self.validator.validate_typed_field_draft(
                validated,
                evidence_by_id,
                expected_value_type=_unit_value_type(unit_key, plan_schema),
                table_requirement=table_requirements.get(node.unit_id),
            )
            step("detect_template_contamination")
            tool_policy.require_tool("detect_template_contamination")
            contamination = self.validator.detect_template_contamination(validated, legacy_claims)
            status = "ready_to_render"
            if contamination:
                validated = validated.model_copy(update={
                    "validation_status": "requires_human",
                    "validation_notes": [*validated.validation_notes, "template contamination detected"],
                })
                with result_lock:
                    result.issues.extend(contamination)
                status = "requires_human"
            elif validated.validation_status != "supported":
                status = "requires_human"
            with result_lock:
                result.drafts.append(validated)
            # Persist each validated draft before its plan-node receipt is
            # committed.  A worker may be replaced after this unit completes
            # but before the graph reaches its terminal node; on recovery the
            # receipt must therefore have a durable draft to accompany it.
            with persistence_lock:
                self.store.save_unit_drafts(
                    work_order.work_order_id,
                    run.harness_run_id,
                    [validated],
                    lease_owner=lease_owner,
                    fencing_token=run.fencing_token,
                )
            with result_lock:
                result.unit_statuses[unit_key] = status
            matrix_row["display_value"] = (
                validated.typed_value.display_value
                if validated.typed_value is not None else validated.content
            )
            return {
                "draft": validated,
                "receipt_payload": {
                    "unit_id": unit_key,
                    "draft_id": validated.unit_id,
                    "status": status,
                    "evidence_ids": list(validated.evidence_ids),
                    "matrix_row": matrix_row,
                    "retrieval_ledger": ledger,
                },
            }

        def execute_node(
            node: CompiledTaskNode,
            attempt: int,
            dag_result: PlanDAGExecutionResult,
        ) -> dict[str, Any]:
            check_lease()
            if plan_node_callback is not None:
                callback_output = plan_node_callback(node, result, dag_result)
                output = callback_output if callback_output is not None else {
                    "node_id": node.node_id, "kind": node.kind,
                }
            else:
                output = {
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "status": "committed",
                }
            self._persist_plan_node_receipt(
                node=node, graph=graph, output=output, attempt=attempt,
                running=run, lease_owner=lease_owner,
                persistence_lock=persistence_lock,
            )
            if node.kind == "release" and hasattr(output, "artifact_id"):
                result.finalization_result = output
            return output if isinstance(output, dict) else {"value": output}

        def execute_unit_with_receipt(node: CompiledTaskNode, attempt: int) -> dict[str, Any]:
            output = execute_unit(node, attempt)
            self._persist_plan_node_receipt(
                node=node, graph=graph, output=output, attempt=attempt,
                running=run, lease_owner=lease_owner,
                persistence_lock=persistence_lock,
            )
            return output

        dag = PlanDAGExecutor(max_workers=policy.max_parallel_units).run(
            graph,
            execute_unit=execute_unit_with_receipt,
            execute_node=execute_node,
            committed_receipts=committed,
        )
        persisted_drafts = self.store.list_unit_drafts(run.harness_run_id)
        by_unit = {draft.unit_id: draft for draft in persisted_drafts}
        for draft in result.drafts:
            by_unit[draft.unit_id] = draft
        ordered_unit_ids = [f"field:{unit.unit_id}" for unit in plan.semantic_units]
        result.drafts = [
            by_unit[key] for key in ordered_unit_ids if key in by_unit
        ] + [
            by_unit[key] for key in sorted(set(by_unit) - set(ordered_unit_ids))
        ]
        for payload in committed.values():
            if isinstance(payload, dict) and payload.get("kind") == "unit":
                unit_id = str(payload.get("unit_id") or "")
                if unit_id and payload.get("draft_id") and unit_id not in by_unit:
                    raise PlanExecutionRouteError(
                        f"committed plan unit receipt has no durable draft: {unit_id}"
                    )
        unit_order = {
            f"field:{unit.unit_id}": index
            for index, unit in enumerate(plan.semantic_units)
        }
        result.matrix_rows.sort(
            key=lambda row: (
                unit_order.get(f"field:{row.get('field_id')}", len(unit_order)),
                str(row.get("field_id") or row.get("review_item_id") or ""),
            )
        )
        result.retrieval_ledger.sort(
            key=lambda row: (
                unit_order.get(str(row.get("unit_id") or ""), len(unit_order)),
                str(row.get("unit_id") or ""),
            )
        )
        result.outcomes = {
            key: result.outcomes[key]
            for key in sorted(
                result.outcomes,
                key=lambda value: (unit_order.get(value, len(unit_order)), value),
            )
        }
        result.plan_node_outputs = {
            key: dag.outputs[key]
            for key in graph.topological_order
            if key in dag.outputs
        }
        result.step_count = max(result.step_count, len(dag.completed_nodes))
        return result

    def create_run(
        self,
        work_order: DocumentWorkOrder,
        policy: HarnessPolicy,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        template: TemplateVersion,
        schema: DocumentSchema,
        *,
        document_plan: DocumentPlan | None = None,
        compiled_graph: CompiledTaskGraph | None = None,
    ) -> tuple[HarnessRun, AuthoringRunManifest]:
        route, plan, graph = self._bind_plan_graph(
            work_order,
            document_plan=document_plan,
            compiled_graph=compiled_graph,
        )
        manifest = self.build_manifest(
            work_order,
            policy,
            snapshot,
            template,
            schema,
            document_plan=plan,
            compiled_graph=graph,
            execution_route=route,
        )
        run = HarnessRun(
            harness_run_id=f"harness-{uuid.uuid4().hex}", work_order_id=work_order.work_order_id,
            run_manifest_id=manifest.run_manifest_id, status="queued", max_retries=policy.max_retries,
            tenant_id=work_order.tenant_id,
            knowledge_base_id=getattr(work_order, "knowledge_base_id", None),
            input_fingerprint=work_order.input_fingerprint,
            input_fingerprint_version=getattr(work_order, "input_fingerprint_version", 1),
            source_set_snapshot_id=work_order.source_set_snapshot_id,
            total_units=len(plan.unit_tasks) if plan is not None else len(schema.fields) + len(schema.review_items),
            unit_statuses=dict(getattr(work_order, "unit_statuses", {}) or {}),
            requested_executor=(
                getattr(work_order, "requested_executor", None)
                or getattr(work_order, "execution_mode", None)
            ),
            migration_state="native",
            task_graph_id=graph.graph_id if graph is not None else None,
            task_graph_version=graph.version if graph is not None else None,
            task_graph_hash=graph.graph_hash if graph is not None else None,
            document_plan_id=plan.document_plan_id if plan is not None else None,
            document_plan_version=plan.version if plan is not None else None,
            document_plan_hash=plan.plan_hash if plan is not None else None,
            execution_route=route,
        )
        self.store.save_run_manifest(manifest)
        self.store.create_harness_run(run)
        return run, manifest

    @staticmethod
    def build_manifest(
        work_order: DocumentWorkOrder,
        policy: HarnessPolicy,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        template: TemplateVersion,
        schema: DocumentSchema,
        *,
        document_plan: DocumentPlan | None = None,
        compiled_graph: CompiledTaskGraph | None = None,
        execution_route: str = "legacy_schema",
    ) -> AuthoringRunManifest:
        source_names = (
            list(snapshot.source_names)
            if work_order.scope_type == "knowledge_base"
            else list(snapshot.source_version_ids)
        )
        return AuthoringRunManifest(
            run_manifest_id=f"manifest-{uuid.uuid4().hex}", work_order_id=work_order.work_order_id,
            harness_policy_id=policy.harness_policy_id, harness_policy_version=policy.version,
            writer_provider_id=policy.writer_provider_id, prompt_version=policy.prompt_version,
            source_set_snapshot_id=work_order.source_set_snapshot_id, input_fingerprint=work_order.input_fingerprint,
            source_set_snapshot_hash=snapshot.content_hash,
            baseline_content_hash=work_order.baseline_content_hash,
            source_version_ids=source_names,
            processing_artifact_ids=list(getattr(snapshot, "processing_artifact_ids", [])),
            region_policy_versions=dict(getattr(snapshot, "region_policy_versions", {})),
            template_content_hash=template.content_hash,
            document_schema_hash=content_hash(schema),
            template_schema_hash=content_hash({
                "template_schema_id": work_order.template_schema_id,
                "template_schema_version": work_order.template_schema_version,
            }),
            retrieval_policy_hash=content_hash({"version": work_order.retrieval_policy_version}),
            execution_mode=work_order.execution_mode,
            input_fingerprint_version=getattr(work_order, "input_fingerprint_version", 1),
            requested_executor=(
                getattr(work_order, "requested_executor", None)
                or getattr(work_order, "execution_mode", None)
            ),
            tool_policy_hash=content_hash(policy),
            max_steps=policy.max_steps,
            max_retrieval_rounds=policy.max_retrieval_rounds,
            max_retrieval_attempts_per_unit=policy.max_retrieval_attempts_per_unit,
            max_parallel_units=policy.max_parallel_units,
            task_graph_id=compiled_graph.graph_id if compiled_graph is not None else None,
            task_graph_version=compiled_graph.version if compiled_graph is not None else None,
            task_graph_hash=compiled_graph.graph_hash if compiled_graph is not None else None,
            document_plan_id=document_plan.document_plan_id if document_plan is not None else None,
            document_plan_version=document_plan.version if document_plan is not None else None,
            document_plan_hash=document_plan.plan_hash if document_plan is not None else None,
            execution_route=execution_route if execution_route in {"legacy_schema", "plan_dag"} else "legacy_schema",
        )

    def execute(
        self,
        *,
        work_order: DocumentWorkOrder,
        run: HarnessRun,
        manifest: AuthoringRunManifest,
        policy: HarnessPolicy,
        schema: DocumentSchema,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        legacy_claims: list[LegacyTemplateClaim],
        writer: ManagedWriter,
        retrieve: RetrievalProvider,
        rewriter: "QueryRewriter | None" = None,
        reranker: "EvidenceReranker | None" = None,
        fit_checker: "RequirementFitChecker | None" = None,
        should_cancel: Callable[[], bool] | None = None,
        plan_node_callback: Callable[[CompiledTaskNode, HarnessExecutionResult, PlanDAGExecutionResult], Any] | None = None,
    ) -> HarnessExecutionResult:
        lease_owner = f"harness-worker-{uuid.uuid4().hex}"
        # Older direct Runtime integrations may provide a minimal manifest
        # stub without the additive route field.  Treat those objects as the
        # unchanged legacy route instead of making the new field mandatory.
        execution_route = getattr(manifest, "execution_route", "legacy_schema")
        # Parallel unit workers persist idempotency receipts while the graph
        # coordinator persists progress.  SQLite permits one writer at a time;
        # serialize only these short store transactions, never the external
        # retrieval/model calls, so progress cannot be starved by receipt I/O.
        persistence_lock = threading.RLock()
        running = self.store.claim_harness_run(run.harness_run_id, lease_owner, policy.lease_seconds)
        if should_cancel is not None and should_cancel():
            self.store.request_harness_run_state(running.harness_run_id, "cancelled")
            raise HarnessLeaseLost("document authoring job was cancelled")
        def current_fencing_token(thread_id: str) -> int | None:
            try:
                current = self.store.get_harness_run(thread_id)
                token = getattr(current, "fencing_token", None)
                return int(token) if isinstance(token, int) else None
            except Exception:
                return None

        if self.checkpointer is not None:
            graph_checkpointer = self.checkpointer
        elif self.checkpointer_factory is not None:
            graph_checkpointer = self.checkpointer_factory(
                fencing_token_provider=current_fencing_token,
            )
        else:
            import src.settings as settings

            backend = getattr(settings, "DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND", "sqlite")
            path = getattr(
                settings,
                "DOCUMENT_AUTHORING_CHECKPOINTER_PATH",
                os.path.join(settings.STORAGE_DIR, "document_authoring_checkpoints.sqlite"),
            )
            graph_checkpointer = build_checkpointer(
                backend,
                sqlite_path=path if str(backend).casefold() == "sqlite" else None,
                fencing_token_provider=current_fencing_token,
            )
        self.store.update_harness_run_owned(
            running.harness_run_id,
            lease_owner,
            running.fencing_token,
            current_node=(
                "plan_dag:preflight"
                if execution_route == "plan_dag"
                else "authoring_graph"
            ),
            effective_executor=(
                "deterministic_rule"
                if getattr(work_order, "execution_mode", None) == "deterministic_only"
                else None
            ),
        )

        def append_execution_event(event: AuthoringExecutionEvent) -> AuthoringExecutionEvent:
            with persistence_lock:
                return self.store.append_execution_event(event)

        def runtime_event(event_type: str, *, action: str, status: str = "succeeded", payload: dict[str, Any] | None = None, error_code: str | None = None) -> AuthoringExecutionEvent:
            action_key = receipt_action_key(
                harness_run_id=running.harness_run_id,
                node_name="authoring_runtime",
                unit_id="run",
                attempt=max(1, int(getattr(running, "retry_count", 0) or 0) + 1),
                input_fingerprint=getattr(work_order, "input_fingerprint", ""),
                action={"version": "v1", "operation": action},
            )
            return append_execution_event(AuthoringExecutionEvent(
                event_id=f"authoring-event-{uuid.uuid4().hex}",
                event_type=event_type,
                tenant_id=getattr(running, "tenant_id", getattr(work_order, "tenant_id", "default")),
                work_order_id=work_order.work_order_id,
                harness_run_id=running.harness_run_id,
                idempotency_key=execution_event_key(action_key, event_type),
                attempt=max(1, int(getattr(running, "retry_count", 0) or 0) + 1),
                executor="authoring_graph",
                node_name="authoring_runtime",
                status=status,
                error_code=error_code,
                sanitized_payload={
                    "execution_route": execution_route,
                    **dict(payload or {}),
                },
            ))

        runtime_event(
            "run_started",
            action="run_started",
            payload={
                "requested_executor": getattr(work_order, "requested_executor", None) or getattr(work_order, "execution_mode", None),
                "input_fingerprint_version": getattr(work_order, "input_fingerprint_version", 1),
            },
        )

        def save_progress(state) -> None:
            with persistence_lock:
                self.store.heartbeat_harness_run(
                    running.harness_run_id, lease_owner, running.fencing_token, policy.lease_seconds,
                )
                updates = {
                    "current_node": state.get("current_node", "authoring_graph"),
                    "step_count": state.get("step_count", 0),
                    "retrieval_round_count": state.get("retrieval_round_count", 0),
                    "completed_units": state.get("completed_units", 0),
                    "total_units": state.get("total_units", 0),
                }
                for key in (
                    "unit_statuses", "unit_attempts", "dispatch_cursor", "evidence_matrix_hash",
                    "draft_ids", "pending_human_event", "trace_id", "agent_thread_id",
                ):
                    if key in state:
                        updates[key] = state[key]
                # HarnessRun is now the business progress source.  The legacy
                # HarnessCheckpoint table is deliberately read-only during the
                # observation window and is never touched on this path.
                self.store.update_harness_run_owned(
                    running.harness_run_id,
                    lease_owner,
                    running.fencing_token,
                    **updates,
                )

        def draft_with_receipt(request) -> DocumentUnitDraft:
            receipt = NodeExecutionReceipt(
                receipt_id=f"receipt-{uuid.uuid4().hex}", harness_run_id=running.harness_run_id,
                node_name="draft_ready_unit", unit_id=request.unit_id,
                input_fingerprint=content_hash({
                    "writer_provider_id": writer.provider.provider_id,
                    "request": request.model_dump(mode="json"),
                }),
                fencing_token=running.fencing_token,
            )
            with persistence_lock:
                receipt = self.store.begin_node_execution_owned(
                    receipt, lease_owner, running.fencing_token,
                )
            if receipt.status == "committed":
                if receipt.output_payload is None:
                    raise RuntimeError("committed draft receipt has no output payload")
                return DocumentUnitDraft.model_validate(receipt.output_payload)
            # Refresh the lease right before the (potentially long) writer call
            # so a slow LLM does not silently expire the lease and lose the run.
            with persistence_lock:
                self.store.heartbeat_harness_run(
                    running.harness_run_id, lease_owner, running.fencing_token, policy.lease_seconds,
                )
            unit_started = time.monotonic()
            unit_status = "completed"
            try:
                try:
                    with observe.chain(
                        "hdb.authoring.draft",
                        operation="draft_ready_unit",
                        unit_id=request.unit_id,
                    ):
                        draft = writer.generate(request)
                except Exception as exc:
                    unit_status = "failed"
                    with persistence_lock:
                        self.store.fail_node_execution_owned(
                            receipt.receipt_id,
                            running.harness_run_id,
                            lease_owner,
                            running.fencing_token,
                            {"type": type(exc).__name__, "message": str(exc)},
                        )
                    raise
                # Refresh the lease again after the writer call so the commit is
                # safe even if the writer took most of the lease window.
                try:
                    with persistence_lock:
                        self.store.heartbeat_harness_run(
                            running.harness_run_id, lease_owner, running.fencing_token, policy.lease_seconds,
                        )
                        self.store.commit_node_execution_owned(
                            receipt.receipt_id,
                            running.harness_run_id,
                            lease_owner,
                            running.fencing_token,
                            draft.model_dump(mode="json"),
                        )
                except Exception:
                    unit_status = "failed"
                    raise
                return draft
            finally:
                record_authoring_unit(
                    operation="draft_ready_unit",
                    status=unit_status,
                    duration_s=time.monotonic() - unit_started,
                )

        def observed_retrieve(*args, **kwargs):
            started = time.monotonic()
            status = "success"
            with observe.retriever("hdb.authoring.retrieve", operation="retrieve"):
                try:
                    return retrieve(*args, **kwargs)
                except Exception:
                    status = "failed"
                    raise
                finally:
                    record_authoring_unit(
                        operation="retrieve",
                        status=status,
                        duration_s=time.monotonic() - started,
                    )

        run_context = AuthoringRunContext(
            save_progress_callback=save_progress,
            heartbeat_callback=lambda: self.store.heartbeat_harness_run(
                running.harness_run_id, lease_owner, running.fencing_token, policy.lease_seconds,
            ),
            draft_with_receipt_callback=draft_with_receipt,
            observed_retrieve_callback=observed_retrieve,
            record_telemetry_callback=lambda **payload: record_authoring_unit(**payload),
        )

        try:
            def check_lease() -> bool:
                if should_cancel is not None and should_cancel():
                    self.store.request_harness_run_state(running.harness_run_id, "cancelled")
                    raise HarnessLeaseLost("document authoring job was cancelled")
                with persistence_lock:
                    self.store.heartbeat_harness_run(
                        running.harness_run_id,
                        lease_owner,
                        running.fencing_token,
                        policy.lease_seconds,
                    )
                return True

            def persist_executor_run(harness_run: Any, **updates: Any) -> Any:
                # The executor may update only the fields that are part of the
                # HarnessRun progress contract. Runtime remains the owner of
                # the fenced write and never accepts a replacement run object.
                allowed = {
                    "status", "current_node", "unit_statuses", "pending_human_event",
                    "effective_executor", "requested_executor", "degraded_reasons",
                    "agent_thread_id", "last_agent_checkpoint_at", "error", "last_error_code",
                    "step_count", "retrieval_round_count", "completed_units", "total_units",
                    "agent_token_usage",
                }
                clean = {key: value for key, value in updates.items() if key in allowed}
                if not clean:
                    clean = {
                        key: getattr(harness_run, key)
                        for key in ("effective_executor", "degraded_reasons", "agent_thread_id")
                        if hasattr(harness_run, key) and getattr(harness_run, key) is not None
                    }
                with persistence_lock:
                    persisted = self.store.update_harness_run_owned(
                        running.harness_run_id,
                        lease_owner,
                        running.fencing_token,
                        **clean,
                    )
                return persisted

            def on_degraded(reason: str, _harness_run: Any, pending: tuple[str, ...] = ()) -> None:
                runtime_event(
                    "fallback_started",
                    action=f"fallback_started:{reason}",
                    payload={"reason": reason, "field_count": len(pending)},
                    error_code=reason,
                )

            execution_context = HarnessExecutionContext(
                work_order=work_order,
                harness_run=running,
                schema=schema,
                policy=policy,
                run_manifest=manifest,
                snapshot=snapshot,
                legacy_claims=tuple(legacy_claims),
                writer=writer,
                retrieve=run_context.observed_retrieve,
                checkpointer=graph_checkpointer,
                extra={
                    "store": self.store,
                    "evidence_store": self.store,
                    "append_execution_event": append_execution_event,
                    "persist_run": persist_executor_run,
                    "check_lease": check_lease,
                    "execution_events": self.store.list_execution_events,
                    "run_context": run_context,
                },
            )
            selection = None
            if execution_route == "plan_dag":
                # Plan-backed execution has its own dependency-aware graph and
                # never constructs the schema-driven graph or its executor.
                result = self._execute_plan_route(
                    work_order=work_order,
                    run=running,
                    manifest=manifest,
                    policy=policy,
                    schema=schema,
                    snapshot=snapshot,
                    legacy_claims=legacy_claims,
                    writer=writer,
                    retrieve=retrieve,
                    rewriter=rewriter,
                    reranker=reranker,
                    fit_checker=fit_checker,
                    should_cancel=should_cancel,
                    run_context=run_context,
                    check_lease=check_lease,
                    persistence_lock=persistence_lock,
                    lease_owner=lease_owner,
                    plan_node_callback=plan_node_callback,
                )
            else:
                graph = AuthoringGraph(
                    HarnessToolPolicy(policy),
                    writer,
                    self.validator,
                    on_progress=run_context.save_progress,
                    draft_provider=run_context.draft_with_receipt,
                    rewriter=rewriter,
                    reranker=reranker,
                    fit_checker=fit_checker,
                )
                graph_executor = InternalGraphExecutor(graph)
                # A few external integrations still call Runtime directly
                # with the pre-selector test DTO. Preserve that narrow
                # compatibility shape; all production legacy objects go
                # through the four-gate selector below.
                legacy_runtime_shape = not hasattr(work_order, "execution_mode") or not hasattr(schema, "execution_mode")
                if legacy_runtime_shape:
                    result = graph_executor.execute(execution_context)
                else:
                    selection = select_harness_executor(
                        schema=schema,
                        work_order=work_order,
                        policy=policy,
                        fallback_executor=graph_executor,
                        requested_executor=getattr(work_order, "requested_executor", None) or work_order.execution_mode,
                        agent_mode_enabled=src.settings.DOCUMENT_AUTHORING_AGENT_MODE_ENABLED,
                        agent_tools_implemented=True,
                        run_manifest=manifest,
                        harness_run=running,
                    )
                    selection.apply_to_run(running)
                    with persistence_lock:
                        self.store.update_harness_run_owned(
                            running.harness_run_id,
                            lease_owner,
                            running.fencing_token,
                            requested_executor=selection.requested_executor,
                            effective_executor=selection.effective_executor,
                            degraded_reasons=selection.degraded_reasons,
                        )
                    if isinstance(selection.executor, AgentFieldHarness):
                        selection.executor.on_run_update = persist_executor_run
                        selection.executor.on_degraded = on_degraded
                    result = selection.executor.execute(execution_context)
            # A human approval resumes the existing HarnessRun.  The agent
            # checkpoint intentionally stores only bounded references, while
            # the governed draft table is the durable source for proposals
            # already accepted before the interrupt.  Carry those drafts into
            # the resumed result so the final FillPlan cannot silently omit a
            # previously committed field.
            persisted_drafts = self.store.list_unit_drafts(running.harness_run_id)
            if not isinstance(persisted_drafts, (list, tuple)):
                persisted_drafts = []
            if persisted_drafts and hasattr(result, "drafts"):
                drafts_by_unit = {draft.unit_id: draft for draft in persisted_drafts}
                drafts_by_unit.update({draft.unit_id: draft for draft in (result.drafts or [])})
                result.drafts = list(drafts_by_unit.values())
                persisted_statuses = {
                    draft.unit_id: "ready_to_render"
                    for draft in persisted_drafts
                    if getattr(draft, "validation_status", None) == "supported"
                }
                result.unit_statuses = {
                    **persisted_statuses,
                    **dict(getattr(result, "unit_statuses", {}) or {}),
                }
            if selection is not None and selection.is_degraded:
                runtime_event(
                    "fallback_completed",
                    action="fallback_completed",
                    payload={"reasons": list(selection.degraded_reasons)},
                )
        except HarnessLeaseLost:
            # Pause/cancel or a new worker advanced the fencing token. The
            # stale worker must not overwrite the controller's decision.
            raise
        except Exception as exc:
            self.store.update_harness_run_owned(
                running.harness_run_id,
                lease_owner,
                running.fencing_token,
                status="failed",
                current_node="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
                last_error_code=type(exc).__name__,
                lease_owner=None,
                lease_expires_at=None,
            )
            raise
        final_status = "waiting_human" if any(status in {"requires_human", "blocked", "conflicting", "retrieval_failed"} for status in result.unit_statuses.values()) else "completed"
        # Refresh the lease before expensive finalization steps so the commit
        # is safe even if the graph took most of the lease window.
        self.store.heartbeat_harness_run(
            running.harness_run_id, lease_owner, running.fencing_token, policy.lease_seconds,
        )
        self.store.save_unit_drafts(
            work_order.work_order_id,
            run.harness_run_id,
            result.drafts,
            lease_owner=lease_owner,
            fencing_token=running.fencing_token,
        )
        self.store.update_harness_run_owned(
            running.harness_run_id,
            lease_owner,
            running.fencing_token,
            status=final_status,
            current_node="complete",
            step_count=result.step_count,
            retrieval_round_count=result.retrieval_round_count,
            lease_owner=None,
            lease_expires_at=None,
        )
        evidence_hashes = {
            evidence.id: sha256(evidence.content.encode("utf-8")).hexdigest()
            for outcome in result.outcomes.values()
            for evidence in outcome.evidences
        }
        self.store.replace_run_manifest(manifest.model_copy(update={
            "evidence_content_hashes": evidence_hashes,
            "completed_at": datetime.now(timezone.utc),
        }))
        runtime_event(
            "run_finalized",
            action="run_finalized",
            payload={"status": final_status, "draft_count": len(result.drafts)},
        )
        return result
