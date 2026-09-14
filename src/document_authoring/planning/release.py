"""Versioned approval, release and artifact-lineage policy contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import NonEmptyId, PlanningModel, planning_content_hash


RiskLevel = Literal["low", "medium", "high"]
GateName = Literal["gate1", "gate2"]


class ApprovalPolicy(PlanningModel):
    """Immutable, versioned release policy selected by a DocumentPlan."""

    policy_id: NonEmptyId
    version: NonEmptyId
    risk_level: RiskLevel = "high"
    supported_document_types: list[NonEmptyId] = Field(default_factory=list, max_length=256)
    gate1_required: bool = True
    gate2_required: bool = True
    required_signer_roles: list[NonEmptyId] = Field(default_factory=list, max_length=32)
    auto_release_allowed: bool = False
    max_issue_count: int = Field(default=0, ge=0, le=100_000)
    status: Literal["available", "unavailable", "disabled"] = "available"
    unavailable_reason: str | None = Field(default=None, max_length=1_000)
    policy_hash: str | None = None

    @model_validator(mode="after")
    def normalize_policy(self) -> "ApprovalPolicy":
        self.supported_document_types = _unique(self.supported_document_types)
        self.required_signer_roles = _unique(self.required_signer_roles)
        if self.status != "available" and not self.unavailable_reason:
            raise ValueError("unavailable approval policies require unavailable_reason")
        if self.auto_release_allowed and self.risk_level != "low":
            raise ValueError("automatic release is only permitted for low-risk policies")
        expected = planning_content_hash(self, exclude={"policy_hash"})
        if self.policy_hash is not None and self.policy_hash != expected:
            raise ValueError("policy_hash does not match policy contents")
        object.__setattr__(self, "policy_hash", expected)
        return self


class ApprovalPolicyRegistry:
    """Exact policy version lookup; no implicit version fallback."""

    def __init__(self) -> None:
        self._policies: dict[tuple[str, str], ApprovalPolicy] = {}

    def register(self, policy: ApprovalPolicy) -> ApprovalPolicy:
        key = (policy.policy_id, policy.version)
        if key in self._policies:
            raise ValueError(f"duplicate approval policy registration: {policy.policy_id}@{policy.version}")
        self._policies[key] = policy
        return policy

    def lookup(self, policy_id: str, version: str) -> ApprovalPolicy | None:
        return self._policies.get((str(policy_id).strip(), str(version).strip()))

    get = lookup

    def resolve(self, policy_id: str, version: str) -> ApprovalPolicy:
        policy = self.lookup(policy_id, version)
        if policy is None:
            raise ValueError(f"approval policy {policy_id}@{version} is not registered")
        if policy.status != "available":
            raise ValueError(
                f"approval policy {policy_id}@{version} is unavailable: "
                f"{policy.unavailable_reason or policy.status}"
            )
        return policy

    def list(self) -> list[ApprovalPolicy]:
        return [self._policies[key] for key in sorted(self._policies)]


class GateFact(PlanningModel):
    """A hash-bound fact for one lifecycle gate."""

    fact_id: NonEmptyId
    gate: GateName
    status: Literal["passed", "failed", "pending"]
    plan_id: NonEmptyId
    plan_version: int = Field(ge=1)
    plan_hash: NonEmptyId
    artifact_hash: NonEmptyId | None = None
    actor_id: NonEmptyId | None = None
    actor_role: NonEmptyId | None = None
    issue_codes: list[NonEmptyId] = Field(default_factory=list, max_length=10_000)
    evidence: dict[str, Any] = Field(default_factory=dict, max_length=256)
    fact_hash: str | None = None

    @model_validator(mode="after")
    def normalize_fact(self) -> "GateFact":
        self.issue_codes = _unique(self.issue_codes)
        if self.status == "passed" and self.issue_codes:
            raise ValueError("a passed gate cannot contain issue codes")
        expected = planning_content_hash(self, exclude={"fact_hash"})
        if self.fact_hash is not None and self.fact_hash != expected:
            raise ValueError("fact_hash does not match gate fact contents")
        object.__setattr__(self, "fact_hash", expected)
        return self


class ReleaseSignature(PlanningModel):
    """An actor/role attestation bound to one gate subject hash."""

    signature_id: NonEmptyId
    gate: GateName
    actor_id: NonEmptyId
    actor_role: NonEmptyId
    subject_hash: NonEmptyId
    signed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    signature: NonEmptyId | None = None

    @model_validator(mode="after")
    def normalize_signature(self) -> "ReleaseSignature":
        if self.signature is None:
            object.__setattr__(self, "signature", planning_content_hash({
                "signature_id": self.signature_id,
                "gate": self.gate,
                "actor_id": self.actor_id,
                "actor_role": self.actor_role,
                "subject_hash": self.subject_hash,
                "signed_at": self.signed_at.isoformat(),
            }))
        return self


class ArtifactLineage(PlanningModel):
    """The facts needed to reconstruct an artifact's parent/run chain."""

    artifact_id: NonEmptyId
    artifact_hash: NonEmptyId
    parent_artifact_id: NonEmptyId | None = None
    revision_id: NonEmptyId | None = None
    plan_id: NonEmptyId
    plan_version: int = Field(ge=1)
    plan_hash: NonEmptyId
    source_snapshot_id: NonEmptyId
    source_snapshot_hash: NonEmptyId
    strategy_id: NonEmptyId
    strategy_version: NonEmptyId
    strategy_hash: NonEmptyId
    policy_id: NonEmptyId
    policy_version: NonEmptyId
    policy_hash: NonEmptyId
    run_manifest_hash: NonEmptyId
    lineage_hash: str | None = None

    @model_validator(mode="after")
    def normalize_lineage(self) -> "ArtifactLineage":
        expected = planning_content_hash(self, exclude={"lineage_hash"})
        if self.lineage_hash is not None and self.lineage_hash != expected:
            raise ValueError("lineage_hash does not match lineage contents")
        object.__setattr__(self, "lineage_hash", expected)
        return self


class TenantReleasePolicy(PlanningModel):
    """Explicit tenant/document-type allowlist for low-risk auto-release."""

    tenant_id: NonEmptyId
    document_type: NonEmptyId
    policy_id: NonEmptyId
    policy_version: NonEmptyId
    auto_release_enabled: bool = False
    allowed_formats: list[NonEmptyId] = Field(default_factory=list, max_length=16)
    allowlisted_recipe_ids: list[NonEmptyId] = Field(default_factory=list, max_length=128)
    threshold_evidence_hash: NonEmptyId | None = None

    @model_validator(mode="after")
    def normalize_tenant_policy(self) -> "TenantReleasePolicy":
        self.allowed_formats = _unique(self.allowed_formats)
        self.allowlisted_recipe_ids = _unique(self.allowlisted_recipe_ids)
        if self.auto_release_enabled and not self.threshold_evidence_hash:
            raise ValueError("auto-release tenant policy requires threshold evidence")
        return self


class PolicyReleaseDecision(PlanningModel):
    """Fail-closed release result with all gate/signature/lineage references."""

    status: Literal["released", "needs_review", "blocked"]
    release_allowed: bool = False
    tenant_id: NonEmptyId
    document_type: NonEmptyId
    policy_id: NonEmptyId
    policy_version: NonEmptyId
    plan_id: NonEmptyId
    plan_hash: NonEmptyId
    artifact_hash: NonEmptyId
    gate_fact_ids: list[NonEmptyId] = Field(default_factory=list, max_length=8)
    signature_ids: list[NonEmptyId] = Field(default_factory=list, max_length=32)
    lineage_hash: NonEmptyId | None = None
    reasons: list[NonEmptyId] = Field(default_factory=list, max_length=10_000)
    decision_hash: str | None = None

    @model_validator(mode="after")
    def normalize_decision(self) -> "PolicyReleaseDecision":
        self.gate_fact_ids = _unique(self.gate_fact_ids)
        self.signature_ids = _unique(self.signature_ids)
        self.reasons = _unique(self.reasons)
        expected = planning_content_hash(self, exclude={"decision_hash"})
        if self.decision_hash is not None and self.decision_hash != expected:
            raise ValueError("decision_hash does not match release decision contents")
        object.__setattr__(self, "decision_hash", expected)
        return self


class ReleasePolicyService:
    """Evaluate two gates and exact lineage against a tenant release policy."""

    def __init__(
        self,
        *,
        policy_registry: ApprovalPolicyRegistry | None = None,
        tenant_policies: Sequence[TenantReleasePolicy] | None = None,
    ) -> None:
        self.policy_registry = policy_registry or build_builtin_approval_policy_registry()
        self.tenant_policies = {
            (item.tenant_id, item.document_type, item.policy_id, item.policy_version): item
            for item in (tenant_policies or [])
        }

    def evaluate(
        self,
        *,
        tenant_id: str,
        document_type: str,
        policy_id: str,
        policy_version: str,
        gate_facts: Sequence[GateFact | Mapping[str, Any]],
        lineage: ArtifactLineage | Mapping[str, Any],
        signatures: Sequence[ReleaseSignature | Mapping[str, Any]] = (),
        issue_count: int = 0,
        output_format: str = "",
        recipe_id: str = "",
        threshold_evidence_hash: str | None = None,
    ) -> PolicyReleaseDecision:
        policy = self.policy_registry.resolve(policy_id, policy_version)
        tenant = str(tenant_id).strip()
        doc_type = str(document_type).strip()
        lineage_value = lineage if isinstance(lineage, ArtifactLineage) else ArtifactLineage.model_validate(lineage)
        facts = [fact if isinstance(fact, GateFact) else GateFact.model_validate(fact) for fact in gate_facts]
        signed = [item if isinstance(item, ReleaseSignature) else ReleaseSignature.model_validate(item) for item in signatures]
        reasons: list[str] = []
        review_reasons: list[str] = []
        if policy.supported_document_types and doc_type not in policy.supported_document_types:
            reasons.append("policy_document_type_unsupported")
        if not lineage_value.plan_id or not lineage_value.plan_hash or not lineage_value.artifact_hash:
            reasons.append("lineage_invalid")
        if lineage_value.policy_id != policy.policy_id or lineage_value.policy_version != policy.version:
            reasons.append("policy_lineage_mismatch")
        if lineage_value.policy_hash != (policy.policy_hash or ""):
            reasons.append("policy_hash_mismatch")
        if len({fact.gate for fact in facts}) != len(facts):
            reasons.append("duplicate_gate_fact")
        facts_by_gate = {fact.gate: fact for fact in facts}
        for fact in facts:
            if (
                fact.plan_id != lineage_value.plan_id
                or fact.plan_version != lineage_value.plan_version
                or fact.plan_hash != lineage_value.plan_hash
            ):
                reasons.append("gate_fact_plan_mismatch")
            if fact.artifact_hash is not None and fact.artifact_hash != lineage_value.artifact_hash:
                reasons.append("gate_fact_artifact_mismatch")
            if fact.gate == "gate2" and fact.status == "passed" and not fact.artifact_hash:
                reasons.append("gate2_artifact_missing")
        reasons = list(dict.fromkeys(reasons))
        for gate, required in (("gate1", policy.gate1_required), ("gate2", policy.gate2_required)):
            fact = facts_by_gate.get(gate)
            if required and (fact is None or fact.status != "passed"):
                reasons.append(f"{gate}_not_passed")
        if issue_count > policy.max_issue_count:
            reasons.append("issue_threshold_exceeded")
        tenant_policy = self.tenant_policies.get((tenant, doc_type, policy.policy_id, policy.version))
        auto_allowed = bool(
            policy.auto_release_allowed
            and tenant_policy is not None
            and tenant_policy.auto_release_enabled
            and (not tenant_policy.allowed_formats or output_format in tenant_policy.allowed_formats)
            and (not tenant_policy.allowlisted_recipe_ids or recipe_id in tenant_policy.allowlisted_recipe_ids)
        )
        if tenant_policy is not None and tenant_policy.auto_release_enabled:
            supplied_threshold = str(threshold_evidence_hash or "").strip()
            configured_threshold = str(tenant_policy.threshold_evidence_hash or "").strip()
            if supplied_threshold and supplied_threshold != configured_threshold:
                review_reasons.append("threshold_evidence_mismatch")
                auto_allowed = False
        gate2_subject = facts_by_gate.get("gate2")
        subject_hash = gate2_subject.fact_hash if gate2_subject else lineage_value.lineage_hash
        valid_signatures = [
            item for item in signed
            if item.subject_hash == subject_hash and item.gate == "gate2"
        ]
        if not auto_allowed:
            missing_roles = set(policy.required_signer_roles) - {item.actor_role for item in valid_signatures}
            if missing_roles:
                reasons.append("required_signer_missing")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            status: Literal["released", "needs_review", "blocked"] = "blocked"
            allowed = False
        elif auto_allowed or valid_signatures:
            status = "released"
            allowed = True
        else:
            status = "needs_review"
            allowed = False
            reasons.extend(review_reasons or ["human_release_signature_required"])
        return PolicyReleaseDecision(
            status=status,
            release_allowed=allowed,
            tenant_id=tenant,
            document_type=doc_type,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            plan_id=lineage_value.plan_id,
            plan_hash=lineage_value.plan_hash,
            artifact_hash=lineage_value.artifact_hash,
            gate_fact_ids=[fact.fact_id for fact in facts],
            signature_ids=[item.signature_id for item in valid_signatures],
            lineage_hash=lineage_value.lineage_hash,
            reasons=reasons,
        )

    def reconstruct_lineage(
        self,
        records: Sequence[ArtifactLineage | Mapping[str, Any]],
        artifact_id: str,
    ) -> list[ArtifactLineage]:
        return reconstruct_artifact_lineage(records, artifact_id)


def reconstruct_artifact_lineage(
    records: Sequence[ArtifactLineage | Mapping[str, Any]],
    artifact_id: str,
) -> list[ArtifactLineage]:
    """Follow parent references oldest-to-newest and reject cycles/gaps."""

    by_id = {
        item.artifact_id: item
        for raw in records
        for item in [raw if isinstance(raw, ArtifactLineage) else ArtifactLineage.model_validate(raw)]
    }
    current_id = str(artifact_id or "").strip()
    if current_id not in by_id:
        raise KeyError("artifact lineage record not found")
    chain: list[ArtifactLineage] = []
    seen: set[str] = set()
    while current_id:
        if current_id in seen:
            raise ValueError("artifact lineage contains a cycle")
        seen.add(current_id)
        current = by_id.get(current_id)
        if current is None:
            raise ValueError("artifact lineage parent is missing")
        chain.append(current)
        current_id = current.parent_artifact_id or ""
    chain.reverse()
    return chain


def build_builtin_approval_policy_registry() -> ApprovalPolicyRegistry:
    registry = ApprovalPolicyRegistry()
    registry.register(ApprovalPolicy(
        policy_id="default-document-v1",
        version="1",
        risk_level="high",
        supported_document_types=["generic", "generic_report", "report", "icd", "fpt", "requirements"],
        required_signer_roles=["document_owner"],
    ))
    registry.register(ApprovalPolicy(
        policy_id="low-risk-document-v1",
        version="1",
        risk_level="low",
        supported_document_types=["generic", "generic_report", "report"],
        required_signer_roles=[],
        auto_release_allowed=True,
    ))
    registry.register(ApprovalPolicy(
        policy_id="formal-engineering-v1",
        version="1",
        risk_level="high",
        supported_document_types=["icd", "fpt", "requirements"],
        required_signer_roles=["document_owner", "reviewer"],
    ))
    return registry


def _unique(values: list[str]) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError("policy list entries must be non-empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError("policy list entries must be unique")
    return normalized


__all__ = [
    "ApprovalPolicy",
    "ApprovalPolicyRegistry",
    "ArtifactLineage",
    "GateFact",
    "PolicyReleaseDecision",
    "ReleasePolicyService",
    "ReleaseSignature",
    "TenantReleasePolicy",
    "build_builtin_approval_policy_registry",
    "reconstruct_artifact_lineage",
]
