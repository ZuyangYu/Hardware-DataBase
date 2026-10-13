"""Immutable dataset provenance and evidence-based release/visit linkage.

Versions retain references and content hashes, not another copy of private
answers. Materializing samples rechecks the original sharing policy every
time, so revocation/deletion cannot leave an accessible frozen derivative.
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.auth import ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN
from src.evaluation.schemas import EvaluationSample, SampleRubric
from .store import QualityConflict, QualityStore, dumps, now_iso, sha256

DDL = (
    "CREATE TABLE IF NOT EXISTS quality_workflow_schema(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS quality_dataset_versions(
        version_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, owner_user_id BIGINT NOT NULL,
        department_id BIGINT NOT NULL, request_key TEXT NOT NULL, manifest_json TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(owner_user_id, request_key))""",
    """CREATE TABLE IF NOT EXISTS quality_change_events(
        link_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, version_id TEXT NOT NULL,
        actor_user_id BIGINT NOT NULL, event_json TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(case_id) REFERENCES quality_cases(case_id) ON DELETE CASCADE,
        FOREIGN KEY(version_id) REFERENCES quality_dataset_versions(version_id))""",
    """CREATE TABLE IF NOT EXISTS quality_analysis_reports(
        report_id TEXT PRIMARY KEY, actor_user_id BIGINT NOT NULL, department_id BIGINT,
        request_key TEXT NOT NULL, input_sha256 TEXT NOT NULL, report_json TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(actor_user_id, request_key))""",
)


class DatasetCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=64)
    review_id: str = Field(min_length=1, max_length=64)
    critical: bool = False
    split: Literal["gold", "repair", "holdout"] = "repair"
    rubric: SampleRubric = Field(default_factory=SampleRubric)


class FreezeDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    cases: list[DatasetCase] = Field(min_length=1, max_length=200)


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int = Field(ge=1)
    dataset_version_id: str = Field(min_length=1, max_length=64)
    change_ref: str = Field(min_length=1, max_length=500)
    baseline_run_id: str = Field(min_length=1, max_length=128)
    candidate_run_id: str = Field(min_length=1, max_length=128)
    release_ref: str = Field(default="", max_length=500)
    deployed_at: str | None = None
    validation_turn_id: str | None = Field(default=None, max_length=128)


class WorkflowStore(QualityStore):
    def __init__(self, db_path=None, *, initialize=False):
        super().__init__(db_path, initialize=initialize)
        with self.transaction() as conn:
            if self.backend == "sqlite" or initialize:
                for ddl in DDL:
                    conn.execute(ddl)
                conn.execute("INSERT INTO quality_workflow_schema VALUES(1, 1) ON CONFLICT(id) DO NOTHING")
            try:
                if conn.execute("SELECT version FROM quality_workflow_schema WHERE id=1").fetchone()[0] != 1:
                    raise RuntimeError("workflow schema version mismatch")
                conn.execute("SELECT version_id, dataset_id, owner_user_id, department_id, request_key, manifest_json, manifest_sha256 FROM quality_dataset_versions LIMIT 0")
                conn.execute("SELECT link_id, case_id, version_id, actor_user_id, event_json, created_at FROM quality_change_events LIMIT 0")
                conn.execute("SELECT report_id, actor_user_id, department_id, request_key, input_sha256, report_json FROM quality_analysis_reports LIMIT 0")
            except Exception as exc:
                raise RuntimeError("quality workflow schema not ready; run python -m src.storage.bootstrap") from exc

    def _material(self, conn, entry, viewer):
        case, snapshot = self.case_rows(conn, entry["case_id"], viewer)
        view = self.view(conn, case, snapshot, viewer)
        if viewer.role != ROLE_EMPLOYEE or not view["source_valid"] or not all(k in view["content"] for k in ("query", "answer")):
            raise PermissionError("dataset requires a currently authorized fixed question and answer")
        review = next((r for r in view["reviews"] if r["review_id"] == entry["review_id"]), None)
        if not review or review["judgment"] != "confirmed" or not review["content"].get("reference_answer", "").strip():
            raise QualityConflict("confirmed review with an authorized reference answer required")
        if any(r["supersedes"] == review["review_id"] for r in view["reviews"]):
            raise QualityConflict("dataset review was superseded")
        content = view["content"]
        evidence = content.get("evidence", [])
        if "evidence_hashes" in entry:
            available = {sha256(e): e for e in evidence}
            if any(key not in available for key in entry["evidence_hashes"]):
                raise PermissionError("the frozen dataset evidence is no longer authorized")
            evidence = [available[key] for key in entry["evidence_hashes"]]
        material = {"query": content["query"], "answer": content["answer"], "evidence": evidence,
                    "reference_answer": review["content"]["reference_answer"]}
        digest = sha256(material)
        if entry.get("material_sha256") and digest != entry["material_sha256"]:
            raise QualityConflict("authorized dataset material changed or was revoked")
        return snapshot, material, digest

    def freeze(self, dataset_id: str, viewer, request: FreezeDatasetRequest):
        if viewer.role != ROLE_EMPLOYEE or viewer.department_id is None:
            raise PermissionError("an employee with authorized review material must freeze datasets")
        if len({e.case_id for e in request.cases}) != len(request.cases):
            raise ValueError("dataset case IDs must be unique")
        with self.transaction() as conn:
            self.request_lock(conn, "freeze_dataset", viewer.id, request.client_request_id)
            key = sha256([dataset_id, request.model_dump(mode="json")])
            prior = conn.execute("SELECT * FROM quality_dataset_versions WHERE owner_user_id=? AND request_key=?", (viewer.id, request.client_request_id)).fetchone()
            if prior:
                if json.loads(prior["manifest_json"])["request_sha256"] != key:
                    raise QualityConflict("dataset request key was reused")
                return {"version_id": prior["version_id"], "manifest_sha256": prior["manifest_sha256"], "deduplicated": True}
            entries, groups = [], {}
            for item in request.cases:
                entry = item.model_dump(mode="json")
                snapshot, material, digest = self._material(conn, entry, viewer)
                if snapshot["department_id"] != viewer.department_id:
                    raise PermissionError("dataset cases must be in the original department")
                group = str(json.loads(snapshot["manifest_json"])["session_id"])
                if group in groups and groups[group] != item.split:
                    raise ValueError("one conversation cannot cross training/holdout splits")
                groups[group] = item.split
                entries.append({**entry, "material_sha256": digest, "snapshot_id": snapshot["snapshot_id"],
                                "evidence_hashes": [sha256(e) for e in material["evidence"]],
                                "answer_revision": snapshot["answer_revision"], "source_group": group})
            manifest = {"schema_version": 1, "dataset_id": dataset_id, "request_sha256": key, "entries": entries,
                        "source_policy": "fixed_retrieved_evidence", "evaluation_adapter": "offline_fixed_answer"}
            version = "dataset-" + uuid.uuid4().hex
            digest = sha256(manifest)
            conn.execute("INSERT INTO quality_dataset_versions VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                         (version, dataset_id, viewer.id, viewer.department_id, request.client_request_id, dumps(manifest), digest, now_iso()))
            return {"version_id": version, "manifest_sha256": digest, "deduplicated": False}

    def version(self, version_id, viewer, *, materialize=False):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM quality_dataset_versions WHERE version_id=?", (version_id,)).fetchone()
            if row is None or (viewer.role != ROLE_SYSTEM_ADMIN and viewer.id != row["owner_user_id"] and viewer.department_id != row["department_id"]):
                raise KeyError("dataset version not found")
            manifest = json.loads(row["manifest_json"])
            result = {"version_id": version_id, "dataset_id": row["dataset_id"], "manifest": manifest,
                      "manifest_sha256": row["manifest_sha256"], "created_at": row["created_at"]}
            if materialize:
                samples, snapshots = [], []
                for entry in manifest["entries"]:
                    snapshot, material, _ = self._material(conn, entry, viewer)
                    original = json.loads(snapshot["manifest_json"])
                    sample = EvaluationSample(id=entry["case_id"], question=material["query"], reference_answer=material["reference_answer"],
                        reference_contexts=[e.get("text", "") for e in material["evidence"]], kb_name=original["kb_name"],
                        tags=[entry["split"]], critical=entry["critical"], rubric=SampleRubric.model_validate(entry["rubric"]),
                        request_context={"user_id": snapshot["owner_user_id"], "department_id": snapshot["department_id"],
                                         "roles": ["employee"], "allowed_kbs": [original["kb_name"]], "kb_permissions": {original["kb_name"]: "read"}})
                    samples.append(sample.model_dump(mode="json"))
                    snapshots.append({"sample_id": sample.id, "question": sample.question, "kb_name": sample.kb_name,
                                      "response": material["answer"], "retrieved_contexts": sample.reference_contexts,
                                      "metadata": {"case_id": entry["case_id"], "snapshot_id": snapshot["snapshot_id"],
                                                   "dataset_version_id": version_id, "material_sha256": entry["material_sha256"]}})
                result.update(samples=samples, snapshots=snapshots)
            return result

    def link_change(self, case_id, viewer, request: ChangeRequest, *, evaluation_root: Path):
        # Local immutable report files are the evidence, not a client-supplied
        # "passed" flag. Read-only history rejects missing/mismatched metadata.
        from src.evaluation.history import compatibility_report, load_history_run
        from src.evaluation.reproducibility import execution_content_hash

        version = self.version(request.dataset_version_id, viewer, materialize=True)
        if request.baseline_run_id == request.candidate_run_id:
            raise ValueError("baseline and candidate must be distinct evaluation runs")
        if case_id not in {e["case_id"] for e in version["manifest"]["entries"]}:
            raise ValueError("case is outside the dataset version")
        for run_id in (request.baseline_run_id, request.candidate_run_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
                raise ValueError("invalid run ID")
        baseline = load_history_run(evaluation_root / request.baseline_run_id)
        candidate = load_history_run(evaluation_root / request.candidate_run_id)
        comparison = compatibility_report(candidate, baseline)
        if not comparison["compatible"]:
            raise QualityConflict("paired evaluation lacks compatible fixed inputs/scoring/ownership")
        gate = candidate.summary.gate
        if not gate or gate.verdict != "pass" or not gate.passed:
            raise QualityConflict("candidate regression has not passed its gate")
        # Bind the experiment's exact inputs to the materialized dataset. A
        # cohort of matching IDs alone cannot prove identical question/reference.
        expected = {s["id"]: s for s in version["samples"]}
        expected_digest = execution_content_hash([EvaluationSample.model_validate(s) for s in version["samples"]])
        for run in (baseline, candidate):
            if run.summary.execution_content_sha256 != expected_digest:
                raise QualityConflict("run input hash does not match the fixed dataset version")
            if set(run.sample_ids) != set(expected):
                raise QualityConflict("run cohort does not match the fixed dataset")
            for r in run.results:
                s = expected[r.sample_id]
                if r.question != s["question"] or r.reference_answer != s["reference_answer"] or r.critical != s["critical"]:
                    raise QualityConflict("run question/reference differs from the fixed dataset")
        payload = {**request.model_dump(mode="json"), "comparison": comparison,
                   "dataset_manifest_sha256": version["manifest_sha256"], "status": "regression_verified"}
        from .experiments import paired_statistics

        payload["paired_statistics"] = paired_statistics(baseline, candidate, viewer)
        if request.release_ref or request.deployed_at or request.validation_turn_id:
            if not request.release_ref or not request.deployed_at:
                raise ValueError("release_ref and deployed_at must be supplied together")
            from datetime import datetime, timezone

            deployed = datetime.fromisoformat(request.deployed_at.replace("Z", "+00:00"))
            if deployed.tzinfo is None or deployed > datetime.now(timezone.utc):
                raise ValueError("deployed_at must be a past timezone-aware timestamp")
            payload["status"] = "deployed"
            if request.validation_turn_id:
                with closing(self.connect()) as conn:
                    target = self.target(conn, request.validation_turn_id, viewer.id)
                    validation = conn.execute("SELECT * FROM quality_cases WHERE snapshot_id=?", (target["snapshot_id"],)).fetchone()
                    if not validation or target["created_at"] < deployed.astimezone(timezone.utc).isoformat() or not self.source_valid(conn, target):
                        raise QualityConflict("a valid post-deployment reviewed turn is required")
                    review = conn.execute("SELECT judgment, content_json FROM quality_reviews WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (validation["case_id"],)).fetchone()
                    if not review or review["judgment"] != "not_an_error" or json.loads(review["content_json"]).get("correctness") != "correct":
                        raise QualityConflict("post-deployment validation is not confirmed correct")
                    fixed = expected[case_id]
                    target_material = json.loads(target["content_json"])
                    target_manifest = json.loads(target["manifest_json"])
                    if target_material.get("query") != fixed["question"] or target_manifest.get("kb_name") != fixed["kb_name"] or target["department_id"] != viewer.department_id:
                        raise QualityConflict("post-deployment turn does not validate the linked scenario")
                    if target_manifest.get("code_revision") != request.release_ref:
                        raise QualityConflict("post-deployment turn is not bound to the declared release revision")
                    payload["status"] = "closed"
        with self.transaction() as conn:
            entries = version["manifest"]["entries"]
            lock_ids = {entry["case_id"] for entry in entries}
            if payload["status"] == "closed":
                live_target = self.target(conn, request.validation_turn_id, viewer.id)
                live_case = conn.execute("SELECT case_id FROM quality_cases WHERE snapshot_id=?", (live_target["snapshot_id"],)).fetchone()
                if live_case is None:
                    raise QualityConflict("post-deployment validation was removed")
                lock_ids.add(live_case["case_id"])
            for locked_id in sorted(lock_ids):
                self.case_rows(conn, locked_id, viewer, lock=True)
            for entry in entries:
                self._material(conn, entry, viewer)
            if payload["status"] == "closed":
                live_target = self.target(conn, request.validation_turn_id, viewer.id)
                live_review = conn.execute("SELECT judgment, content_json FROM quality_reviews WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (live_case["case_id"],)).fetchone()
                if not self.source_valid(conn, live_target) or not live_review or live_review["judgment"] != "not_an_error" or json.loads(live_review["content_json"]).get("correctness") != "correct":
                    raise QualityConflict("post-deployment validation changed before closing")
            case, _snapshot = self.case_rows(conn, case_id, viewer)
            if case["row_version"] != request.row_version:
                raise QualityConflict("case changed; refresh before linking")
            link_id = "change-" + uuid.uuid4().hex
            conn.execute("INSERT INTO quality_change_events VALUES(?, ?, ?, ?, ?, ?)", (link_id, case_id, request.dataset_version_id, viewer.id, dumps(payload), now_iso()))
            status = "closed" if payload["status"] == "closed" else "regression_verified"
            conn.execute("UPDATE quality_cases SET status=?, row_version=row_version+1, updated_at=? WHERE case_id=?", (status, now_iso(), case_id))
            return {"link_id": link_id, "status": payload["status"]}

    def changes(self, case_id, viewer):
        with closing(self.connect()) as conn:
            self.case_rows(conn, case_id, viewer)
            rows = conn.execute("SELECT link_id, event_json, created_at FROM quality_change_events WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()
            # No user-provided free-text refs in the metadata-only view.
            authorized = self.get_case(case_id, viewer)["content_access"] != "hidden"
            return [{"link_id": r["link_id"], "created_at": r["created_at"], "event": json.loads(r["event_json"]) if authorized else {"status": json.loads(r["event_json"])["status"]}} for r in rows]

    def analyze(self, viewer, request_key: str, costs):
        """Freeze bounded deterministic statistics, no content or LLM inference."""
        with self.transaction() as conn:
            self.request_lock(conn, "quality_analysis", viewer.id, request_key)
            prior = conn.execute("SELECT report_id FROM quality_analysis_reports WHERE actor_user_id=? AND request_key=?", (viewer.id, request_key)).fetchone()
            if prior:
                return {"report_id": prior[0], "deduplicated": True}
            where = "" if viewer.role == ROLE_SYSTEM_ADMIN else " WHERE q.department_id=? OR q.owner_user_id=?"
            params = () if viewer.role == ROLE_SYSTEM_ADMIN else (viewer.department_id, viewer.id)
            rows = conn.execute("SELECT c.case_id, c.status, c.trigger_json, c.row_version FROM quality_cases c JOIN quality_snapshots q ON c.snapshot_id=q.snapshot_id" + where + " ORDER BY c.case_id LIMIT 10001", params).fetchall()
            if len(rows) > 10000:
                raise ValueError("analysis window too large for the first-version bounded reporter")
            state_counts, trigger_counts = {}, {}
            for row in rows:
                state_counts[row["status"]] = state_counts.get(row["status"], 0) + 1
                for trigger in json.loads(row["trigger_json"]):
                    trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1
            cost_summary = costs.summary(viewer)
            frozen = {"case_revisions": [[r["case_id"], r["row_version"]] for r in rows],
                      "case_count": len(rows), "case_status_counts": state_counts, "case_trigger_counts": trigger_counts,
                      "costs": cost_summary}
            recommendations = []
            if cost_summary["unknown_cost_calls"]:
                recommendations.append({"recommendation_id": "cost-coverage", "status": "proposed", "hypothesis": "缺失 usage 或未注册价格影响费用覆盖率",
                    "evidence_refs": ["verified_statistics.costs.unknown_cost_calls"], "experiment_plan": "固定 provider/model/cohort 验证 usage 及版本化价格匹配", "success_criteria": "费用覆盖率提高且相同调用不重复计费"})
            if state_counts.get("new", 0):
                recommendations.append({"recommendation_id": "review-backlog", "status": "proposed", "hypothesis": "待复核队列需要领域确认，不能推断全站错误率",
                    "evidence_refs": ["verified_statistics.case_status_counts.new"], "experiment_plan": "分开反馈/随机抽样队列，复核固定问答和证据", "success_criteria": "经确认的案例具有参考答案及可复现回归材料"})
            report = {"schema_version": 1, "revision": 1, "aggregation_version": "quality-cost-v1", "rule_version": "coverage-backlog-v1",
                      "reporter": "deterministic_code", "input_manifest_sha256": sha256(frozen), "verified_statistics": frozen,
                      "recommendations": recommendations, "scope": cost_summary["scope"], "created_at": now_iso(),
                      "limitations": ["all_time_window", "phoenix_not_read", "not_population_quality_estimate", "no_llm_explanation"]}
            report_id = "report-" + uuid.uuid4().hex
            conn.execute("INSERT INTO quality_analysis_reports VALUES(?, ?, ?, ?, ?, ?, ?)", (report_id, viewer.id,
                         None if viewer.role == ROLE_SYSTEM_ADMIN else viewer.department_id, request_key, sha256(frozen), dumps(report), now_iso()))
            return {"report_id": report_id, "deduplicated": False}

    def reports(self, viewer, report_id=None):
        with closing(self.connect()) as conn:
            where, params = [], []
            if viewer.role != ROLE_SYSTEM_ADMIN:
                where.append("department_id=?")
                params.append(viewer.department_id)
            if report_id:
                where.append("report_id=?")
                params.append(report_id)
            sql = "SELECT report_id, report_json FROM quality_analysis_reports"
            if where:
                sql += " WHERE " + " AND ".join(where)
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT 100", params).fetchall()
            if report_id and not rows:
                raise KeyError("report not found")
            return [{"report_id": r["report_id"], **json.loads(r["report_json"])} for r in rows]
