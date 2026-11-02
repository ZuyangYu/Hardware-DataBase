"""Local acceptance of the public skill-safety-scanner fixture.

This exercises the upstream scanner through the HDB Runtime adapter only. It
does not start Docker, HDB or a Runner; the real Runner acceptance is recorded
in the fixture README.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from src.sandbox.skill_packages import validate_skill_package


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "examples" / "skills" / "external-skill-safety-scanner"


def _archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _run_adapter(tmp_path: Path, target: bytes | None) -> dict:
    assets = tmp_path / "assets"
    assets.mkdir()
    if target is not None:
        (assets / "target.zip").write_bytes(target)
    output = tmp_path / "out"
    output.mkdir()
    env = os.environ | {
        "HDB_SKILL_IN": str(assets),
        "HDB_SKILL_OUT": str(output),
        "PYTHONPATH": str(SKILL_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(SKILL_ROOT / "entrypoint.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads((output / "result.json").read_text(encoding="utf-8"))


def test_upstream_scanner_package_has_complete_source_manifest():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SKILL_ROOT.rglob("*")):
            if path.is_file() and path.name not in {"README.md", "LICENSE"}:
                archive.write(path, path.relative_to(SKILL_ROOT).as_posix())
    package = validate_skill_package(buffer.getvalue())
    # The repository keeps LICENSE and README for attribution; the uploadable
    # package contains only files the fixed HDB build template accepts.
    assert set(package.files) == {
        "MANIFEST.json", "SKILL.md", "entrypoint.py", "scripts/safety_scan.py",
    }
    assert {item["path"] for item in package.source_manifest} == set(package.files)


def test_clean_target_completes_through_adapter(tmp_path):
    target = _archive({
        "SKILL.md": "---\nname: clean-target\ndescription: clean\n---\n# clean\n",
        "scripts/main.py": "from pathlib import Path\nPath('report.txt').write_text('ok')\n",
    })
    result = _run_adapter(tmp_path, target)
    assert result["result_status"] == "completed"
    assert result["artifact_paths"] == ["scan.json", "summary.md"]
    scan = json.loads((tmp_path / "out" / "scan.json").read_text(encoding="utf-8"))
    assert scan["grade"] == "A" and scan["score"] == 100


def test_unsafe_target_is_reported_as_validation_failed(tmp_path):
    target = _archive({
        "SKILL.md": "---\nname: unsafe-target\ndescription: unsafe\n---\n# unsafe\n",
        "scripts/main.py": (
            "import os\n"
            "password = \"hardcoded-secret-value\"\n"
            "os.system(input(\"command: \"))\n"
        ),
    })
    result = _run_adapter(tmp_path, target)
    assert result["result_status"] == "validation_failed"
    codes = {issue["code"] for issue in result["issues"]}
    assert {"generic-password", "py-os-system"} <= codes


def test_missing_zip_returns_needs_user_input(tmp_path):
    result = _run_adapter(tmp_path, None)
    assert result["result_status"] == "needs_user_input"
    assert result["required_inputs"] == ["artifact:target-zip"]
    assert result["artifact_paths"] == []
