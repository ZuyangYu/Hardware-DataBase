"""Fail-closed retrieval over an immutable Project SourceSetSnapshot."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from src.agents.claim_evidence import InformationRequirement, RetrievalOutcome, RetrievalSourceOutcome
from src.pipelines.document_rag.schemas import EvidenceEnvelope, RequestContext
from src.projects.service import ProjectService


class SourceUnavailableError(RuntimeError):
    pass


class RetrievalFailedError(RuntimeError):
    pass


class ProjectEvidenceRetrievalService:
    """Domain boundary for document retrieval.

    ``retrieve_one`` is an adapter supplied by the RAGFlow, Spreadsheet or
    Circuit service.  It receives one immutable source version at a time, so a
    backend cannot broaden a failed selected-source query into a global search.
    """

    def __init__(self, project_service: ProjectService):
        self.projects = project_service

    def retrieve(
        self,
        ctx: RequestContext,
        requirement: InformationRequirement,
        snapshot_id: str,
        retrieve_one: Callable[[str, list[str], dict[str, str]], list[EvidenceEnvelope]],
    ) -> RetrievalOutcome:
        tenant_id = ctx.tenant_id or "default"
        snapshot = self.projects.store.get_source_set_snapshot(snapshot_id, tenant_id)
        if snapshot is None:
            raise KeyError("source set snapshot not found")
        self.projects.access.require(ctx, snapshot.project_id, "read_evidence")
        if requirement.project_id and requirement.project_id != snapshot.project_id:
            raise PermissionError("requirement project does not match frozen source set")
        requested_versions = set(requirement.source_version_scope or snapshot.source_version_ids)
        allowed_versions = set(snapshot.source_version_ids) | set(snapshot.shared_reference_version_ids)
        if not requested_versions <= allowed_versions:
            raise PermissionError("requirement expands beyond frozen source versions")

        outcomes: list[RetrievalSourceOutcome] = []
        evidences: list[EvidenceEnvelope] = []
        by_version_artifacts = self._artifacts_by_version(snapshot.source_version_ids, snapshot.processing_artifact_ids, tenant_id)
        for version_id in sorted(requested_versions):
            artifacts = by_version_artifacts.get(version_id, [])
            if not artifacts:
                outcomes.append(RetrievalSourceOutcome(source_version_id=version_id, status="source_unavailable", error_code="no_frozen_artifact"))
                continue
            try:
                result = retrieve_one(version_id, artifacts, dict(snapshot.region_policy_versions))
            except PermissionError as exc:
                outcomes.append(RetrievalSourceOutcome(source_version_id=version_id, status="access_denied", error_code=str(exc)))
                continue
            except SourceUnavailableError as exc:
                outcomes.append(RetrievalSourceOutcome(source_version_id=version_id, status="source_unavailable", error_code=str(exc), retryable=True))
                continue
            except Exception as exc:
                outcomes.append(RetrievalSourceOutcome(source_version_id=version_id, status="retrieval_failed", error_code=type(exc).__name__, diagnostics={"message": str(exc)}, retryable=True))
                continue
            accepted = [
                evidence for evidence in result
                if evidence.project_id == snapshot.project_id
                and evidence.source_version_id == version_id
                and evidence.processing_artifact_id in artifacts
            ]
            # Any adapter returning unscoped evidence is a filter incompatibility,
            # not a zero-result condition.  Do not silently accept or retry broad.
            if len(accepted) != len(result):
                outcomes.append(RetrievalSourceOutcome(source_version_id=version_id, status="filter_unsupported", error_code="adapter_returned_out_of_scope_evidence"))
                continue
            evidences.extend(accepted)
            outcomes.append(RetrievalSourceOutcome(
                source_version_id=version_id,
                processing_artifact_id=artifacts[0] if len(artifacts) == 1 else None,
                status="success_with_hits" if accepted else "success_empty",
                evidence_ids=[evidence.id for evidence in accepted],
            ))
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status=_aggregate_status(outcomes),
            evidences=evidences,
            source_outcomes=outcomes,
            query_fingerprint=hashlib.sha256(
                f"{requirement.model_dump_json()}|{snapshot.content_hash}".encode("utf-8")
            ).hexdigest(),
            applied_source_set_snapshot_id=snapshot.source_set_snapshot_id,
            applied_region_policy_versions=dict(snapshot.region_policy_versions),
        )

    def _artifacts_by_version(self, versions: list[str], artifact_ids: list[str], tenant_id: str) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {version_id: [] for version_id in versions}
        for version_id in versions:
            version = self.projects.store.get_source_version(version_id, tenant_id)
            if version is None:
                continue
            for artifact_id in artifact_ids:
                artifact = self.projects.store.get_processing_artifact(artifact_id, tenant_id)
                if artifact is not None and artifact.asset_id == version.asset_id and artifact.status == "ready":
                    result[version_id].append(artifact.artifact_id)
        return result


def _aggregate_status(outcomes: list[RetrievalSourceOutcome]) -> str:
    statuses = {outcome.status for outcome in outcomes}
    if not outcomes:
        return "source_unavailable"
    if "access_denied" in statuses:
        return "access_denied"
    if "retrieval_failed" in statuses or "filter_unsupported" in statuses:
        return "retrieval_failed" if statuses <= {"retrieval_failed", "filter_unsupported"} else "partial_failure"
    if "source_unavailable" in statuses:
        return "source_unavailable" if statuses <= {"source_unavailable"} else "partial_failure"
    return "success_with_hits" if "success_with_hits" in statuses else "success_empty"
