"""Repeatable offline production-pilot evidence for document authoring.

The command exercises only local contracts and temporary SQLite files.  It
does not enable rollout flags, contact a model/provider, enqueue a job, or
change a deployment.  Its JSON output is deliberately suitable for an
operator to sign after adding live smoke evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.document_authoring.compatibility import (
    create_sqlite_backup,
    restore_sqlite_backup,
)
from src.document_authoring.document_model import (
    DocumentModel,
    ParagraphBlock,
    SectionBlock,
    TypedTableBlock,
)
from src.document_authoring.models import TypedTableRow
from src.document_authoring.planning.models import (
    ArtifactSpec,
    DeliverableSpec,
    DocumentPlan,
    OutputSpec,
    SystemRecipeSource,
    TableRequirement,
    OutlineUnitSpec,
)
from src.document_authoring.planning.release import (
    ArtifactLineage,
    build_builtin_approval_policy_registry,
    reconstruct_artifact_lineage,
)
from src.document_authoring.planning.recipes import (
    StructureBindingCompiler,
    build_builtin_recipe_registry,
)
from src.document_authoring.planning.service import TemplateFreePlanningAdapter
from src.document_authoring.renderers.structured import (
    StructuredDocxRenderer,
    StructuredPdfRenderer,
    StructuredXlsxRenderer,
    validate_structured_artifact,
    visual_baseline_fingerprint,
)
from src.document_authoring.harness.idempotency import canonical_json


def run_offline_pilot(*, output_path: str | Path | None = None) -> dict[str, Any]:
    """Run the complete offline pilot matrix and optionally persist JSON."""

    checks = [
        _run_check("planning_contracts", _check_planning_contracts),
        _run_check("failure_boundaries", _check_failure_boundaries),
        _run_check("parser_visual_baselines", _check_parser_visual_baselines),
        _run_check("lineage_reconstruction", _check_lineage_reconstruction),
        _run_check("backup_restore_rollback", _check_backup_restore_rollback),
    ]
    status = "passed" if all(item["status"] == "passed" for item in checks) else "failed"
    bundle: dict[str, Any] = {
        "pilot_version": "document-authoring-offline-v1",
        "status": status,
        "production_enabled": False,
        "operator_signoff_required": True,
        "deployment_defaults": {
            "DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED": False,
            "DOCUMENT_PLAN_DAG_EXECUTION_ENABLED": False,
            "DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED": False,
            "allowlists": [],
        },
        "checks": checks,
        "operator_signoff": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    bundle["evidence_hash"] = _hash_payload(bundle)
    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the JSON evidence bundle to this path")
    args = parser.parse_args(argv)
    bundle = run_offline_pilot(output_path=args.output)
    if args.output is None:
        print(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if bundle["status"] == "passed" else 1


def _check_planning_contracts() -> dict[str, Any]:
    cases = [_case("docx", "generic-report", "generic_report"),
             _case("pdf", "generic-report", "generic_report"),
             _case("xlsx", "structured-table", "generic")]
    plans = []
    for case in cases:
        plan = _compile_plan(case)
        if plan.status != "proposed" or not plan.is_executable:
            raise ValueError("pilot planning contract is not executable")
        plans.append({
            "format": case["format"],
            "recipe": case["recipe"],
            "plan_hash": plan.plan_hash,
            "unit_count": len(plan.semantic_units),
        })
    return {"cases": plans}


def _check_failure_boundaries() -> dict[str, Any]:
    case = _case("docx", "generic-report", "generic_report")
    spec = case["spec"]
    payload = spec.model_dump(mode="json", exclude={"content_hash"})
    payload["layout_source"] = {
        "mode": "system_recipe",
        "recipe_id": "unknown-recipe",
        "recipe_version": "1",
    }
    blocked_spec = OutputSpec.model_validate(payload)
    plan = TemplateFreePlanningAdapter().compile(
        output_spec=blocked_spec,
        source_snapshot_id="pilot-snapshot",
        source_snapshot_hash="sha256:pilot-snapshot",
    )
    if plan.status != "blocked" or not any(issue.code == "recipe_missing" for issue in plan.issues):
        raise ValueError("unknown recipe did not fail at planning")
    return {"unknown_recipe_status": plan.status, "issue_codes": [issue.code for issue in plan.issues]}


def _check_parser_visual_baselines() -> dict[str, Any]:
    baseline_path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "document_authoring" / "structure_visual_baselines.json"
    baselines = json.loads(baseline_path.read_text(encoding="utf-8"))
    cases = [
        ("docx", "generic-report", StructuredDocxRenderer()),
        ("pdf", "generic-report", StructuredPdfRenderer()),
        ("xlsx", "structured-table", StructuredXlsxRenderer()),
    ]
    reports = []
    for output_format, recipe_id, renderer in cases:
        case = _case(
            output_format,
            recipe_id,
            "generic" if output_format == "xlsx" else "generic_report",
        )
        plan = _compile_plan(case).model_copy(update={"status": "accepted"})
        model = _model(plan)
        binding = StructureBindingCompiler(build_builtin_recipe_registry()).compile(plan, model)
        rendered = renderer.render(
            model,
            build_builtin_recipe_registry().resolve(recipe_id, "1"),
            binding,
        )
        parsed = validate_structured_artifact(rendered.content, output_format)
        if parsed.status != "passed":
            raise ValueError(f"pilot parser failed for {output_format}")
        expected = baselines[f"{recipe_id}@1"][output_format]
        actual_artifact_hash = rendered.integrity_manifest["artifact_hash"]
        actual_visual_hash = visual_baseline_fingerprint(rendered.content, output_format)
        if actual_artifact_hash != expected["artifact_hash"] or actual_visual_hash != expected["visual_baseline_hash"]:
            raise ValueError(f"pilot visual baseline mismatch for {output_format}")
        reports.append({
            "format": output_format,
            "recipe": recipe_id,
            "artifact_hash": actual_artifact_hash,
            "visual_baseline_hash": actual_visual_hash,
            "parser_status": parsed.status,
        })
    return {"cases": reports}


def _check_lineage_reconstruction() -> dict[str, Any]:
    policy = build_builtin_approval_policy_registry().resolve("default-document-v1", "1")
    parent = ArtifactLineage(
        artifact_id="pilot-artifact-parent", artifact_hash="sha256:pilot-parent",
        plan_id="pilot-plan", plan_version=1, plan_hash="sha256:pilot-plan",
        source_snapshot_id="pilot-snapshot", source_snapshot_hash="sha256:pilot-source",
        strategy_id="generic_report", strategy_version="1", strategy_hash="sha256:pilot-strategy",
        policy_id=policy.policy_id, policy_version=policy.version, policy_hash=policy.policy_hash,
        run_manifest_hash="sha256:pilot-manifest-parent",
    )
    child = parent.model_copy(update={
        "artifact_id": "pilot-artifact-child",
        "artifact_hash": "sha256:pilot-child",
        "parent_artifact_id": parent.artifact_id,
        "run_manifest_hash": "sha256:pilot-manifest-child",
    })
    chain = reconstruct_artifact_lineage([child, parent], child.artifact_id)
    if [item.artifact_id for item in chain] != [parent.artifact_id, child.artifact_id]:
        raise ValueError("lineage chain is not oldest-to-newest")
    return {"chain": [item.artifact_id for item in chain], "lineage_hash": child.lineage_hash}


def _check_backup_restore_rollback() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="document-authoring-pilot-") as directory:
        root = Path(directory)
        database = root / "pilot.db"
        backup = root / "pilot.backup.db"
        safety = root / "pilot.safety.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE pilot_marker (value TEXT)")
            connection.execute("INSERT INTO pilot_marker VALUES ('before')")
        backup_report = create_sqlite_backup(database, backup)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE pilot_marker SET value = 'after'")
        restore_report = restore_sqlite_backup(database, backup, safety_backup_path=safety)
        with sqlite3.connect(database) as connection:
            value = connection.execute("SELECT value FROM pilot_marker").fetchone()[0]
        if value != "before" or not backup_report.verified or not restore_report.verified:
            raise ValueError("backup/restore rollback did not restore the frozen database")
    return {
        "backup_verified": backup_report.verified,
        "restore_verified": restore_report.verified,
        "safety_backup_created": restore_report.safety_backup_path is not None,
    }


def _case(output_format: str, recipe_id: str, document_type: str) -> dict[str, Any]:
    outline = [
        OutlineUnitSpec(unit_id="overview", kind="section", title="Overview"),
        OutlineUnitSpec(unit_id="summary", kind="paragraph", title="Summary"),
        OutlineUnitSpec(unit_id="signals", kind="table", title="Signals"),
    ]
    spec = OutputSpec(
        output_spec_id=f"pilot-spec-{output_format}", version=1, status="proposed",
        purpose="Offline pilot report", audience=["pilot"], document_type=document_type,
        artifact=ArtifactSpec(deliverables=[DeliverableSpec(
            format=output_format, role="primary", required=True,
        )]),
        layout_source=SystemRecipeSource(
            recipe_id=recipe_id, recipe_version="1",
        ),
        outline=outline,
        table_requirements=[TableRequirement(
            unit_id="signals", row_scope="pilot signals",
            required_columns=["signal", "value"], row_keys=["J1:1", "J1:2"],
        )],
        approval_policy_id="default-document-v1",
    )
    return {"format": output_format, "recipe": recipe_id, "spec": spec}


def _compile_plan(case: dict[str, Any]) -> DocumentPlan:
    return TemplateFreePlanningAdapter().compile(
        output_spec=case["spec"],
        source_snapshot_id="pilot-snapshot",
        source_snapshot_hash="sha256:pilot-snapshot",
    )


def _model(plan: DocumentPlan) -> DocumentModel:
    return DocumentModel(
        document_id="pilot-document",
        plan_id=plan.document_plan_id,
        plan_version=plan.version,
        plan_hash=plan.plan_hash,
        blocks=[
            SectionBlock(
                unit_id="overview", block_id="block:overview",
                title="Overview", content="Safe report",
            ),
            ParagraphBlock(
                unit_id="summary", block_id="block:summary",
                content="The result is evidence-bound.",
            ),
            TypedTableBlock(
                unit_id="signals", block_id="block:signals",
                columns=["signal", "value"],
                expected_row_keys=["J1:1", "J1:2"],
                rows=[
                    TypedTableRow(
                        row_key="J1:1", cells={"signal": "CAN_H", "value": "3.3V"},
                        evidence_ids=["ev-1"], cell_evidence_ids={"signal": ["ev-1"], "value": ["ev-1"]},
                    ),
                    TypedTableRow(
                        row_key="J1:2", cells={"signal": "CAN_L", "value": "1.7V"},
                        evidence_ids=["ev-2"], cell_evidence_ids={"signal": ["ev-2"], "value": ["ev-2"]},
                    ),
                ],
            ),
        ],
    )


def _run_check(name: str, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"name": name, "status": "passed", "evidence": callback()}
    except Exception as exc:
        # Keep failure output safe and useful without persisting exception
        # text that might contain a path, source name or provider response.
        return {
            "name": name,
            "status": "failed",
            "evidence": {"error_type": exc.__class__.__name__},
        }


def _hash_payload(value: MappingLike) -> str:
    payload = dict(value)
    payload.pop("evidence_hash", None)
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


MappingLike = dict[str, Any]


__all__ = ["main", "run_offline_pilot"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
