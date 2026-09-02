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

from src.document_authoring.harness.graph import AuthoringGraph, HarnessExecutionResult, RetrievalProvider
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
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
    LegacyTemplateClaim,
    NodeExecutionReceipt,
    TemplateVersion,
    content_hash,
)
from src.document_authoring.harness.idempotency import execution_event_key, receipt_action_key
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import ManagedWriter
from src.projects.models import SourceSetSnapshot
from src.observability import observe
from src.observability.metrics import record_authoring_unit
import src.settings

if TYPE_CHECKING:
    from src.document_authoring.writers.evidence_reranker import EvidenceReranker
    from src.document_authoring.writers.query_rewriter import QueryRewriter
    from src.document_authoring.writers.requirement_fit_checker import RequirementFitChecker


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

    def create_run(
        self,
        work_order: DocumentWorkOrder,
        policy: HarnessPolicy,
        snapshot: SourceSetSnapshot | KnowledgeBaseSourceSnapshot,
        template: TemplateVersion,
        schema: DocumentSchema,
    ) -> tuple[HarnessRun, AuthoringRunManifest]:
        manifest = self.build_manifest(work_order, policy, snapshot, template, schema)
        run = HarnessRun(
            harness_run_id=f"harness-{uuid.uuid4().hex}", work_order_id=work_order.work_order_id,
            run_manifest_id=manifest.run_manifest_id, status="queued", max_retries=policy.max_retries,
            tenant_id=work_order.tenant_id,
            knowledge_base_id=getattr(work_order, "knowledge_base_id", None),
            input_fingerprint=work_order.input_fingerprint,
            input_fingerprint_version=getattr(work_order, "input_fingerprint_version", 1),
            source_set_snapshot_id=work_order.source_set_snapshot_id,
            total_units=len(schema.fields) + len(schema.review_items),
            unit_statuses=dict(getattr(work_order, "unit_statuses", {}) or {}),
            requested_executor=(
                getattr(work_order, "requested_executor", None)
                or getattr(work_order, "execution_mode", None)
            ),
            migration_state="native",
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
    ) -> HarnessExecutionResult:
        lease_owner = f"harness-worker-{uuid.uuid4().hex}"
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
            current_node="authoring_graph",
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
                sanitized_payload=dict(payload or {}),
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
            # A few external integrations still call Runtime directly with
            # the pre-selector test DTO.  Preserve that narrow compatibility
            # shape; all production DocumentWorkOrder/DocumentSchema objects
            # go through the four-gate selector below.
            legacy_runtime_shape = not hasattr(work_order, "execution_mode") or not hasattr(schema, "execution_mode")
            if legacy_runtime_shape:
                selection = None
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
