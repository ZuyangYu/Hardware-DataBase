"""HDB Runtime adapter for the upstream skill-safety-scanner.

The scanner implementation is copied unchanged from the upstream registry.
This adapter only converts one read-only ZIP Artifact into the directory input
expected by that scanner and emits Runtime Contract v1 outputs.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path

from scripts.safety_scan import scan_skill


INPUT_ROOT = Path(os.getenv("HDB_SKILL_IN", "/work/in/assets"))
OUTPUT_ROOT = Path(os.getenv("HDB_SKILL_OUT", "/work/out"))
MAX_FILES = 512
MAX_UNPACKED_BYTES = 16 * 1024 * 1024


def _find_input_zip() -> Path | None:
    candidates = sorted(path for path in INPUT_ROOT.rglob("*") if path.is_file() and path.suffix.lower() == ".zip")
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one input ZIP Artifact, found {len(candidates)}")
    return candidates[0]


def _extract_skill(archive_path: Path, destination: Path) -> Path:
    total = 0
    count = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            count += 1
            if count > MAX_FILES or info.file_size > MAX_UNPACKED_BYTES or total + info.file_size > MAX_UNPACKED_BYTES:
                raise ValueError("input Skill ZIP exceeds adapter limits")
            name = info.filename.replace("\\", "/")
            parts = name.split("/")
            if not name or name.startswith("/") or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("input Skill ZIP contains an unsafe path")
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("input Skill ZIP symlinks are not allowed")
            target = destination.joinpath(*parts).resolve()
            if not str(target).startswith(str(destination.resolve()) + "/"):
                raise ValueError("input Skill ZIP escaped its extraction directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            data = archive.read(info)
            target.write_bytes(data)
            total += len(data)

    roots = sorted(path.parent for path in destination.rglob("SKILL.md"))
    if len(roots) != 1:
        raise ValueError(f"input Skill ZIP must contain exactly one SKILL.md, found {len(roots)}")
    return roots[0]


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path = _find_input_zip()
    if archive_path is None:
        (OUTPUT_ROOT / "result.json").write_text(
            json.dumps(
                {
                    "runtime_contract_version": "v1",
                    "result_status": "needs_user_input",
                    "message": "attach one Skill ZIP Artifact to scan",
                    "issues": [],
                    "required_inputs": ["artifact:target-zip"],
                    "required_tools": [],
                    "artifact_paths": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return
    with tempfile.TemporaryDirectory(prefix="skill-safety-target-", dir="/tmp") as work:
        target = _extract_skill(archive_path, Path(work))
        result = scan_skill(target)
        payload = asdict(result)
        (OUTPUT_ROOT / "scan.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT_ROOT / "summary.md").write_text(
            "# Skill Safety Scan\n\n"
            f"- Skill: `{result.skill_name}`\n"
            f"- Grade: **{result.grade}** ({result.score}/100)\n"
            f"- Secrets: {result.secret_score}/100\n"
            f"- Dangerous code: {result.dangerous_code_score}/100\n"
            f"- Permissions: {', '.join(result.permissions) or 'none'}\n\n"
            f"Findings: {len(result.findings)}\n",
            encoding="utf-8",
        )
        issues = [
            {
                "code": finding.rule_id or finding.dimension,
                "severity": finding.severity,
                "message": finding.title,
                "path": finding.file_path,
                "line": finding.line_number,
            }
            for finding in result.findings
            if finding.dimension != "permission"
        ][:16]
        status = "completed" if result.grade in {"A", "B"} else "validation_failed"
        (OUTPUT_ROOT / "result.json").write_text(
            json.dumps(
                {
                    "runtime_contract_version": "v1",
                    "result_status": status,
                    "message": f"upstream skill-safety-scanner grade {result.grade} ({result.score}/100)",
                    "issues": issues,
                    "required_inputs": [],
                    "required_tools": [],
                    "artifact_paths": ["scan.json", "summary.md"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - Runtime result must remain structured
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        (OUTPUT_ROOT / "result.json").write_text(
            json.dumps(
                {
                    "runtime_contract_version": "v1",
                    "result_status": "failed",
                    "error_code": "adapter_failed",
                    "issues": [{"code": "adapter_failed", "severity": "error", "message": str(exc)[:500]}],
                    "required_inputs": [],
                    "required_tools": [],
                    "artifact_paths": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        raise
