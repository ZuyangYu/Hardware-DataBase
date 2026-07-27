"""Persistence-aware internal Harness runtime for P2b."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from hashlib import sha256
from typing import TYPE_CHECKING

from src.document_authoring.harness.graph import AuthoringGraph, HarnessExecutionResult, RetrievalProvider
from src.document_authoring.harness.policy import HarnessLeaseLost, HarnessToolPolicy
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentUnitDraft,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessCheckpoint,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
    LegacyTemplateClaim,
    NodeExecutionReceipt,
    TemplateVersion,
    content_hash,
)
from src.document_authoring.validator import DocumentValidator
from src.document_authoring.work_order_store import DocumentAuthoringStore
from src.document_authoring.writers.managed import ManagedWriter
from src.projects.models import SourceSetSnapshot

if TYPE_CHECKING:
    from src.document_authoring.writers.query_rewriter import QueryRewriter


class InternalDocumentHarnessRuntime:
    def __init__(self, store: DocumentAuthoringStore, validator: DocumentValidator | None = None):
        self.store = store
        self.validator = validator or DocumentValidator()

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
            tool_policy_hash=content_hash(policy),
            max_steps=policy.max_steps,
            max_retrieval_rounds=policy.max_retrieval_rounds,
            max_retrieval_attempts_per_unit=policy.max_retrieval_attempts_per_unit,
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
    ) -> HarnessExecutionResult:
        lease_owner = f"harness-worker-{uuid.uuid4().hex}"
        running = self.store.claim_harness_run(run.harness_run_id, lease_owner, policy.lease_seconds)
        checkpoint = HarnessCheckpoint(
            checkpoint_id=f"checkpoint-{uuid.uuid4().hex}", harness_run_id=running.harness_run_id,
            work_order_id=work_order.work_order_id, input_fingerprint=work_order.input_fingerprint,
            source_set_snapshot_id=work_order.source_set_snapshot_id, fencing_token=running.fencing_token,
            current_node="authoring_graph",
        )
        self.store.save_harness_checkpoint_owned(checkpoint, lease_owner, running.fencing_token)
        self.store.update_harness_run_owned(
            running.harness_run_id,
            lease_owner,
            running.fencing_token,
            checkpoint_id=checkpoint.checkpoint_id,
            current_node="authoring_graph",
        )

        def save_progress(state) -> None:
            nonlocal checkpoint
            checkpoint = checkpoint.model_copy(update={
                "current_node": state["current_node"],
                "step_count": state["step_count"],
                "retrieval_round_count": state["retrieval_round_count"],
                "updated_at": datetime.now(timezone.utc),
            })
            self.store.heartbeat_harness_run(
                running.harness_run_id, lease_owner, running.fencing_token, policy.lease_seconds,
            )
            self.store.save_harness_checkpoint_owned(checkpoint, lease_owner, running.fencing_token)

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
            receipt = self.store.begin_node_execution_owned(
                receipt, lease_owner, running.fencing_token,
            )
            if receipt.status == "committed":
                if receipt.output_payload is None:
                    raise RuntimeError("committed draft receipt has no output payload")
                return DocumentUnitDraft.model_validate(receipt.output_payload)
            # Refresh the lease right before the (potentially long) writer call
            # so a slow LLM does not silently expire the lease and lose the run.
            self.store.heartbeat_harness_run(
                running.harness_run_id, lease_owner, running.fencing_token, policy.lease_seconds,
            )
            try:
                draft = writer.generate(request)
            except Exception as exc:
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
            return draft

        try:
            graph = AuthoringGraph(
                HarnessToolPolicy(policy),
                writer,
                self.validator,
                on_progress=save_progress,
                draft_provider=draft_with_receipt,
                rewriter=rewriter,
            )
            result = graph.run(
                work_order=work_order, harness_run=running, run_manifest=manifest, schema=schema, snapshot=snapshot,
                legacy_claims=legacy_claims, retrieve=retrieve,
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
            self.store.finalize_harness_checkpoint(checkpoint.checkpoint_id, "failed")
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
        finished = self.store.update_harness_run_owned(
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
        self.store.finalize_harness_checkpoint(
            checkpoint.checkpoint_id,
            "completed" if finished.status == "completed" else "waiting_human",
        )
        return result
