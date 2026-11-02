#!/usr/bin/env python3
"""BOM compliance audit Skill entrypoint (Runtime Contract v1)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checks import audit_rows, build_summary, find_input_rows, load_rows_from_text  # noqa: E402


def _work_paths() -> tuple[Path, Path]:
    # The production Runner mounts /work/in and /work/out.  The environment
    # override keeps the same contract executable in local acceptance tests.
    return (
        Path(os.environ.get("HDB_SKILL_IN", "/work/in")),
        Path(os.environ.get("HDB_SKILL_OUT", "/work/out")),
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _result_payload(
    status: str,
    *,
    artifact_paths: list[str] | None = None,
    checkpoint_path: str = "",
    issues: list[dict[str, object]] | None = None,
    required_inputs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "runtime_contract_version": "v1",
        "result_status": status,
        "artifact_paths": artifact_paths or [],
        "checkpoint_path": checkpoint_path,
        "issues": issues or [],
        "required_inputs": required_inputs or [],
        "required_tools": [],
    }


def main() -> int:
    work_in, work_out = _work_paths()
    work_out.mkdir(parents=True, exist_ok=True)
    params_path = work_in / "parameters.json"
    try:
        parameters = json.loads(params_path.read_text(encoding="utf-8")) if params_path.is_file() else {}
    except (OSError, ValueError):
        parameters = {}
    if not isinstance(parameters, dict):
        parameters = {}

    raw_rows = parameters.get("bom_rows", parameters.get("rows"))
    if isinstance(raw_rows, str):
        rows = load_rows_from_text(raw_rows)
    elif isinstance(raw_rows, list):
        rows = [dict(item) for item in raw_rows if isinstance(item, dict)]
    else:
        rows = []
    source_name = "parameters.bom_rows"
    if not rows:
        rows, source_name = find_input_rows(work_in / "assets")

    if not rows:
        _write_json(
            work_out / "result.json",
            _result_payload(
                "needs_user_input",
                required_inputs=["bom_rows", "bom_csv_or_json_asset"],
            ),
        )
        return 0

    normalized, issues = audit_rows(rows)
    summary = build_summary(normalized, issues)
    _write_json(work_out / "normalized-bom.json", {
        "source": source_name,
        "row_count": len(normalized),
        "rows": normalized,
    })
    (work_out / "summary.md").write_text(summary, encoding="utf-8")

    if issues:
        _write_json(work_out / "validation-report.json", {
            "source": source_name,
            "row_count": len(normalized),
            "issue_count": len(issues),
            "issues": issues,
        })
        _write_json(
            work_out / "result.json",
            _result_payload(
                "validation_failed",
                artifact_paths=["validation-report.json", "normalized-bom.json", "summary.md"],
                issues=issues,
            ),
        )
        return 0

    _write_json(
        work_out / "result.json",
        _result_payload(
            "completed",
            artifact_paths=["normalized-bom.json", "summary.md"],
            checkpoint_path="normalized-bom.json",
        ),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # fail closed with a contract-shaped result
        work_in, work_out = _work_paths()
        _write_json(
            work_out / "result.json",
            _result_payload(
                "failed",
                issues=[{"code": "entrypoint_error", "message": type(exc).__name__}],
            ),
        )
        raise
