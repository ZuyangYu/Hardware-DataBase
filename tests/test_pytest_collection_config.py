"""The default test command must collect only maintained automated tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_pytest_collection_excludes_retired_acceptance_scripts():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-4000:]
    assert "storage/acceptance/test_dept_r3_20261019/run_live_test.py" not in (
        result.stdout + result.stderr
    )
