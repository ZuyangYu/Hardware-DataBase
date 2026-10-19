"""Offline R3 conflict gate over raw fixtures and a disposable authoring store.

This evaluates extraction, grouping, persisted review state and writer inputs.
It does not evaluate retrieval relevance, API authorization or formal approval.
Exit codes: 0 local pass, 2 failure/invalid input, 3 human review incomplete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import src.settings as settings  # noqa: E402
from src.document_authoring.conflicts.extractor import GroundedClaimExtractor  # noqa: E402
from src.document_authoring.conflicts.policy import ConflictPolicy  # noqa: E402
from src.document_authoring.service import DocumentGenerationService  # noqa: E402
from src.document_authoring.work_order_store import DocumentAuthoringStore  # noqa: E402
from src.document_authoring.writers.provider import WriterRequest  # noqa: E402
from src.projects.service import ProjectService  # noqa: E402
from src.projects.store import ProjectStore  # noqa: E402

DEFAULT_DATASET = REPO_ROOT / "evaluation/datasets/multi_kb_conflicts_v1.jsonl"


class FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(FixtureModel):
    kb_id: int = Field(gt=0)
    role: Literal["primary", "reference", "standard", "peer"]


class Evidence(FixtureModel):
    id: str = Field(min_length=1)
    kb_id: int = Field(gt=0)
    content: str = Field(min_length=1)
    approval_status: Literal["draft", "approved", "effective", "released"] = "draft"
    effective_scope: str = "project:fixture"
    revision: str = ""
    source_version_id: str = "v1"


class ExpectedGroup(FixtureModel):
    field_key: str = Field(min_length=1)
    kind: Literal["technical_fact_conflict", "critical_source_gap"]
    severity: Literal["S0", "S1", "S2", "S3"]
    status: Literal["open", "resolved", "dismissed", "blocked", "deferred", "approval_pending"]
    blocking: bool
    action: str | None
    evidence_ids: list[str] = Field(min_length=1)
    selected_evidence_ids: list[str]


class Expected(FixtureModel):
    claim_count: int = Field(ge=0)
    retained_evidence_ids: list[str]
    groups: list[ExpectedGroup]


class Review(FixtureModel):
    status: Literal["pending", "approved"]
    reviewer: str | None

    @model_validator(mode="after")
    def attributed_review(self):
        if self.status == "approved" and not (self.reviewer or "").strip():
            raise ValueError("approved labels require an identified independent reviewer")
        return self


class Case(FixtureModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    origin: Literal["synthetic", "source_excerpt"]
    review: Review
    purpose: Literal["compose", "compare"]
    unit_label: str = Field(min_length=1)
    sources: list[Source] = Field(min_length=1, max_length=4)
    evidence: list[Evidence] = Field(min_length=1)
    expected: Expected
    expected_auto_resolve: Expected | None = None

    @model_validator(mode="after")
    def source_bindings(self):
        source_ids = [source.kb_id for source in self.sources]
        evidence_ids = [item.id for item in self.evidence]
        if len(set(source_ids)) != len(source_ids) or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("source and evidence identities must be unique within a case")
        if any(item.kb_id not in source_ids for item in self.evidence):
            raise ValueError("evidence references an unselected source")
        roles = [source.role for source in self.sources]
        if self.purpose == "compose" and (roles.count("primary") != 1 or "peer" in roles):
            raise ValueError("compose requires one primary and reference/standard sources")
        if self.purpose == "compare" and set(roles) != {"peer"}:
            raise ValueError("compare requires peer sources")
        for expected in (self.expected, self.expected_auto_resolve):
            if expected is None:
                continue
            labels = [expected.retained_evidence_ids]
            for group in expected.groups:
                labels.extend([group.evidence_ids, group.selected_evidence_ids])
                if not set(group.selected_evidence_ids) <= set(group.evidence_ids):
                    raise ValueError("selected labels must belong to their conflict group")
            if any(len(ids) != len(set(ids)) or not set(ids) <= set(evidence_ids) for ids in labels):
                raise ValueError("expected labels reference duplicate or unknown evidence")
        return self


def validate_cases(cases: list[dict]) -> list[dict]:
    validated = [Case.model_validate(case).model_dump(mode="json", exclude_none=False) for case in cases]
    if not validated or len({case["id"] for case in validated}) != len(validated):
        raise ValueError("dataset must be nonempty with unique case IDs")
    return validated


def _parse_cases(text: str) -> list[dict]:
    cases = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"invalid JSON at line {number}") from exc
    return validate_cases(cases)


def load_cases(path: Path) -> list[dict]:
    return _parse_cases(path.read_text(encoding="utf-8"))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _expected(case: dict, auto_resolve: bool) -> dict:
    return (case.get("expected_auto_resolve") if auto_resolve else None) or case["expected"]


def _canonical_groups(groups: list[dict]) -> list[str]:
    return sorted(json.dumps({**group, "evidence_ids": sorted(group["evidence_ids"]),
                              "selected_evidence_ids": sorted(group["selected_evidence_ids"])},
                             sort_keys=True) for group in groups)


def score_case(case: dict, observed: dict, *, auto_resolve: bool) -> list[str]:
    expected = _expected(case, auto_resolve)
    failures = []
    if observed["claim_count"] != expected["claim_count"]:
        failures.append(f"claim_count: expected {expected['claim_count']}, got {observed['claim_count']}")
    if not observed["provenance_valid"]:
        failures.append("provenance: source identity, version, hash or exact quote span missing/changed")
    if sorted(observed["retained_evidence_ids"]) != sorted(expected["retained_evidence_ids"]):
        failures.append("retained_evidence_ids differ from the labeled writer input")
    if _canonical_groups(observed["groups"]) != _canonical_groups(expected["groups"]):
        failures.append("groups differ in membership, severity, review state, action or selected source")
    return failures


def observe_case(case: dict, service: DocumentGenerationService, *, index: int) -> dict:
    sources = {source["kb_id"]: source for source in case["sources"]}
    evidence = [{
        "id": item["id"], "content": item["content"], "source_name": f"fixture-{item['id']}",
        "metadata": {
            "knowledge_source": sources[item["kb_id"]],
            "approval_status": item["approval_status"], "effective_scope": item["effective_scope"],
            "revision": item["revision"], "source_version_id": item["source_version_id"],
            "source_content_hash": hashlib.sha256(item["content"].encode()).hexdigest(),
        },
    } for item in case["evidence"]]
    claims = GroundedClaimExtractor().extract(evidence)
    by_id = {item["id"]: item for item in evidence}
    provenance_valid = True
    for claim in claims:
        source = by_id.get(claim.evidence_ids[0]) if len(claim.evidence_ids) == 1 else None
        span = claim.locator.get("quote_span", {})
        provenance_valid &= bool(source and span and span.get("text")
                                 and source["content"][span["start"]:span["end"]] == span["text"]
                                 and claim.source_kb_id == source["metadata"]["knowledge_source"]["kb_id"]
                                 and claim.source_role == source["metadata"]["knowledge_source"]["role"]
                                 and claim.source_content_hash == source["metadata"]["source_content_hash"]
                                 and claim.source_version_id == source["metadata"]["source_version_id"]
                                 and claim.approval_status == source["metadata"]["approval_status"])
    order = SimpleNamespace(work_order_id=f"eval-{index}", tenant_id="offline-evaluation",
                            kb_scope_snapshot={"purpose": case["purpose"], "sources": case["sources"]})
    request = WriterRequest(work_order_id=order.work_order_id, run_id="offline", unit_id="field:fixture",
                            unit_label=case["unit_label"], evidence=evidence, prompt_version="conflict-eval-v1")
    prepared = service.prepare_conflict_writer_request(order, SimpleNamespace(content_hash=_digest(evidence)), request)
    persisted = service.conflict_store.list_cases(
        tenant_id=order.tenant_id, work_order_id=order.work_order_id,
        statuses=("open", "resolved", "dismissed", "blocked", "deferred", "approval_pending"),
    )
    contexts = {item["conflict_id"]: item for item in prepared.conflict_context}
    groups = []
    for conflict in persisted:
        context = contexts[conflict.conflict_id]
        selected = context["selected_claim"] or {}
        groups.append({
            "field_key": conflict.field_key, "kind": conflict.conflict_kind,
            "severity": conflict.severity, "status": conflict.status,
            "blocking": context["blocking"], "action": context["action"],
            "evidence_ids": sorted({eid for claim in conflict.claims for eid in claim.evidence_ids}),
            "selected_evidence_ids": sorted(selected.get("evidence_ids", [])),
        })
    return {"claim_count": len(claims), "claims": [claim.model_dump(mode="json") for claim in claims],
            "provenance_valid": provenance_valid, "groups": groups,
            "retained_evidence_ids": [item["id"] for item in prepared.evidence]}


def evaluate(cases: list[dict], *, require_human_review: bool = False) -> dict:
    cases = validate_cases(cases)
    auto_resolve = bool(settings.MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED)
    results = []
    missed = false_blocks = execution_errors = 0
    with tempfile.TemporaryDirectory(prefix="hdb-conflict-eval-") as directory:
        root = Path(directory)
        service = DocumentGenerationService(
            project_service=ProjectService(ProjectStore(str(root / "projects.db"))),
            store=DocumentAuthoringStore(str(root / "authoring.db"), str(root / "artifacts")),
        )
        for index, case in enumerate(cases):
            expected = _expected(case, auto_resolve)
            try:
                observed = observe_case(case, service, index=index)
                failures = score_case(case, observed, auto_resolve=auto_resolve)
                def blocking_keys(groups):
                    return {(group["field_key"], group["kind"], tuple(sorted(group["evidence_ids"])))
                            for group in groups if group["blocking"]}

                wanted = blocking_keys(expected["groups"])
                actual = blocking_keys(observed["groups"])
                missed += len(wanted - actual)
                false_blocks += int(bool(actual - wanted))
            except Exception as exc:
                execution_errors += 1
                observed = None
                failures = [f"execution_error: {type(exc).__name__}: {exc}"]
            results.append({"id": case["id"], "category": case["category"], "expected": expected,
                            "observed": observed, "failures": failures, "passed": not failures})
    pending = [case["id"] for case in cases if case["review"]["status"] != "approved"]
    passed = all(result["passed"] for result in results)
    verdict = "fail" if not passed else "insufficient_evaluation" if require_human_review and pending else "pass"
    return {
        "schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_kind": "offline_conflict_regression", "model_used_for_scoring": False,
        "case_set_sha256": _digest(cases), "extractor_version": GroundedClaimExtractor.version,
        "policy_version": ConflictPolicy().version,
        "implementation_sha256": {
            path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() for path in (
                "scripts/production_hardening/multi_kb_conflict_eval.py",
                "src/document_authoring/service.py",
                *(f"src/document_authoring/conflicts/{name}.py"
                  for name in ("models", "extractor", "normalizer", "detector", "policy", "store")),
            )
        },
        "configuration": {"auto_resolve_enabled": auto_resolve, "require_human_review": require_human_review},
        "verdict": verdict, "release_ready": False,
        "not_evaluated": ["retrieval_relevance", "live_source_authorization", "formal_approval",
                          "long_running_load", "migration_rollback"],
        "pending_human_review": pending,
        "summary": {"case_count": len(cases), "passed_cases": sum(item["passed"] for item in results),
                    "critical_block_misses": missed, "false_block_cases": false_blocks,
                    "execution_error_cases": execution_errors},
        "cases": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-human-review", action="store_true")
    args = parser.parse_args(argv)
    try:
        dataset_bytes = args.dataset.read_bytes()
        cases = _parse_cases(dataset_bytes.decode("utf-8"))
        report = evaluate(cases, require_human_review=args.require_human_review)
    except (OSError, ValueError, TypeError) as exc:
        print(f"invalid dataset: {exc}", file=sys.stderr)
        return 2
    report["dataset_sha256"] = hashlib.sha256(dataset_bytes).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], **report["summary"]}))
    return {"pass": 0, "fail": 2, "insufficient_evaluation": 3}[report["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
