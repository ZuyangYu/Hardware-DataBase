"""The architecture ratchet must run as part of the normal test suite.

Pre-commit hooks can be bypassed; a pytest test cannot be skipped silently by
``git commit --no-verify``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import networkx as nx

ROOT = Path(__file__).resolve().parent.parent


def _load_gate():
    path = ROOT / "scripts" / "architecture_check.py"
    spec = importlib.util.spec_from_file_location("architecture_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_architecture_gate_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "architecture_check.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "architecture gate passed" in result.stdout


def test_gate_detects_a_new_reverse_layer_edge() -> None:
    gate = _load_gate()
    graph = nx.DiGraph()
    graph.add_edge("src.core.probe", "src.api.app")
    violations = gate._layer_violations(graph)
    assert any(
        item["source"] == "src.core.probe" and item["target"] == "src.api.app"
        for item in violations
    )
