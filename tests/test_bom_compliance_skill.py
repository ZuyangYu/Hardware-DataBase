"""End-to-end acceptance for the bundled BOM compliance example Skill."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from src.sandbox.runtime_contract import parse_skill_result
from src.sandbox.skill_packages import validate_skill_package


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "examples" / "skills" / "bom-compliance-audit"


def _package_bytes() -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SKILL_DIR.rglob("*")):
            relative = path.relative_to(SKILL_DIR)
            if path.is_file() and "__pycache__" not in relative.parts and path.suffix != ".pyc":
                archive.write(path, relative.as_posix())
    return buffer.getvalue()


def _run_skill(tmp_path: Path, parameters: dict, *, assets: dict[str, str] | None = None):
    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    work_in.mkdir()
    work_out.mkdir()
    (work_in / "parameters.json").write_text(json.dumps(parameters), encoding="utf-8")
    if assets:
        asset_dir = work_in / "assets" / ("art-" + "a" * 32)
        asset_dir.mkdir(parents=True)
        for name, content in assets.items():
            (asset_dir / name).write_text(content, encoding="utf-8")
    env = {**os.environ, "HDB_SKILL_IN": str(work_in), "HDB_SKILL_OUT": str(work_out)}
    completed = subprocess.run(
        [sys.executable, str(SKILL_DIR / "entrypoint.py")],
        text=True,
        capture_output=True,
        timeout=20,
        env=env,
        check=False,
    )
    return completed, work_out


def test_package_contract_and_metadata():
    package = validate_skill_package(_package_bytes())
    assert package.metadata["skill_name"] == "bom-compliance-audit"
    assert "BOM" in package.metadata["summary"]
    assert package.metadata["intent_examples"]
    assert package.metadata["input_kinds"] == ["json", "csv"]
    assert package.metadata["risk"] == "low"
    assert set(package.files) == {"SKILL.md", "README.md", "entrypoint.py", "checks.py"}


def test_bundled_zip_matches_source_tree():
    archive_path = SKILL_DIR.parent / "bom-compliance-audit.zip"
    package = validate_skill_package(archive_path.read_bytes())
    assert package.metadata["skill_name"] == "bom-compliance-audit"
    with zipfile.ZipFile(archive_path) as archive:
        for name in package.files:
            assert archive.read(name) == (SKILL_DIR / name).read_bytes()


def test_valid_bom_returns_completed_artifacts(tmp_path):
    completed, work_out = _run_skill(
        tmp_path,
        {
            "bom_rows": [
                {"reference": "R1", "part_number": "10k", "quantity": 2,
                 "voltage_v": 3.3, "min_voltage_v": 2.0, "max_voltage_v": 5.0},
                {"reference": "C1", "part_number": "100nF", "quantity": 1},
            ]
        },
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads((work_out / "result.json").read_text(encoding="utf-8"))
    parsed = parse_skill_result(result)
    assert parsed.result_status == "completed"
    assert set(result["artifact_paths"]) == {"normalized-bom.json", "summary.md"}
    assert "未发现结构性问题" in (work_out / "summary.md").read_text(encoding="utf-8")
    normalized = json.loads((work_out / "normalized-bom.json").read_text(encoding="utf-8"))
    assert normalized["row_count"] == 2


def test_invalid_bom_returns_validation_report(tmp_path):
    completed, work_out = _run_skill(
        tmp_path,
        {
            "bom_rows": [
                {"reference": "R1", "part_number": "10k", "quantity": 1},
                {"reference": "R1", "part_number": "", "quantity": 0, "voltage_v": 1.0,
                 "min_voltage_v": 2.0},
            ]
        },
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads((work_out / "result.json").read_text(encoding="utf-8"))
    parsed = parse_skill_result(result)
    assert parsed.result_status == "validation_failed"
    codes = {item["code"] for item in result["issues"]}
    assert {"duplicate_reference", "missing_part_number", "invalid_quantity",
            "voltage_below_minimum"} <= codes
    report = json.loads((work_out / "validation-report.json").read_text(encoding="utf-8"))
    assert report["issue_count"] == len(result["issues"])


def test_missing_bom_requests_user_input(tmp_path):
    completed, work_out = _run_skill(tmp_path, {})
    assert completed.returncode == 0, completed.stderr
    result = json.loads((work_out / "result.json").read_text(encoding="utf-8"))
    parsed = parse_skill_result(result)
    assert parsed.result_status == "needs_user_input"
    assert result["required_inputs"]
    assert not result["artifact_paths"]


def test_csv_input_artifact_is_consumed(tmp_path):
    completed, work_out = _run_skill(
        tmp_path,
        {},
        assets={"bom.csv": "reference,part_number,quantity\nR1,10k,1\nR2,1k,2\n"},
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads((work_out / "result.json").read_text(encoding="utf-8"))
    assert result["result_status"] == "completed"
    normalized = json.loads((work_out / "normalized-bom.json").read_text(encoding="utf-8"))
    assert normalized["source"] == "bom.csv"
    assert normalized["row_count"] == 2
