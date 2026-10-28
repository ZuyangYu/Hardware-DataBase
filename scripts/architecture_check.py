"""Architecture fitness gate.

This is deliberately dependency-free: it parses ``src`` with ``ast`` and uses
``networkx`` (already a runtime dependency) for strongly connected components.
The goal is to freeze *new* architectural erosion, not to force a big-bang
cleanup of the current graph.

Checks:
1. No module may newly join a circular dependency (SCC).
2. The number of edges inside existing cycles may not grow.
3. No new reverse-layer dependency (for example ``src.core`` importing
   ``src.api``) beyond the checked-in baseline.
4. Large files may not grow past their checked-in line budget.
5. Runtime ``ALTER TABLE`` occurrences in ``src`` may not increase.

Usage::

    python scripts/architecture_check.py
    python scripts/architecture_check.py --write-baseline

``--write-baseline`` is intentional: updating the ratchet must be a visible
review decision, not an automatic side effect.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Iterable

import networkx as nx

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
BASELINE = ROOT / "architecture_baseline.json"

# Reverse-layer rules.  A source package may not depend on a target package.
# Existing violations are baselined; every new edge fails the gate.
FORBIDDEN_LAYER_EDGES: tuple[tuple[str, str, str], ...] = (
    ("src.core", "src.api", "core must not import the API layer"),
    ("src.core", "src.workers", "core must not import workers"),
    ("src.agents", "src.api", "agents must not import the API layer"),
    ("src.agents", "src.workers", "agents must not import workers"),
    ("src.pipelines", "src.api", "pipelines must not import the API layer"),
    ("src.pipelines", "src.workers", "pipelines must not import workers"),
    ("src.storage", "src.api", "storage must not import the API layer"),
    ("src.storage", "src.agents", "storage must not import agent orchestration"),
    ("src.storage", "src.pipelines", "storage must not import pipeline orchestration"),
    ("src.attachments", "src.api", "attachments must not import the API layer"),
    ("src.memory", "src.api", "memory must not import the API layer"),
    ("src.evaluation", "src.api", "evaluation must not import the API layer"),
    ("src.api", "src.workers", "API must not import worker implementation"),
    ("src.settings", "src.", "settings must stay a leaf package"),
)

LINE_BUDGET_TOLERANCE = 25

LINE_BUDGETS = (
    "src/core/app_pipeline.py",
    "src/agents/runner.py",
    "src/settings.py",
)

_ALTER_RE = re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)


class _ModuleCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.names.add(node.module)
        for alias in node.names:
            if node.module and alias.name != "*":
                self.names.add(f"{node.module}.{alias.name}")


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts or "vendor" in path.parts:
            continue
        files[_module_name(path)] = path
    return files


def _resolve_relative(current: str, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module
    package = current.rsplit(".", 1)[0] if "." in current else current
    parts = package.split(".")
    if node.level > 1:
        parts = parts[: -(node.level - 1)]
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


def _imports_for(path: Path, current: str) -> set[str]:
    collector = _ModuleCollector()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except SyntaxError as exc:
        raise SystemExit(f"cannot parse {path}: {exc}") from exc
    collector.visit(tree)
    resolved = set(collector.names)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            relative = _resolve_relative(current, node)
            if relative:
                resolved.add(relative)
                for alias in node.names:
                    if alias.name != "*":
                        resolved.add(f"{relative}.{alias.name}")
    return resolved


def _known_target(name: str, known: dict[str, Path]) -> str | None:
    if name in known:
        return name
    # ``from src.core import auth`` should attach to src.core.auth when that
    # module exists; otherwise keep the package-level dependency.
    probe = name
    while "." in probe:
        probe = probe.rsplit(".", 1)[0]
        if probe in known:
            return probe
    return None


def _graph() -> tuple[nx.DiGraph, dict[str, Path]]:
    known = _module_files()
    graph = nx.DiGraph()
    graph.add_nodes_from(known)
    for current, path in known.items():
        for raw in _imports_for(path, current):
            target = _known_target(raw, known)
            if target and target != current:
                graph.add_edge(current, target)
    return graph, known


def _scc_state(graph: nx.DiGraph) -> tuple[set[str], int]:
    modules: set[str] = set()
    edge_count = 0
    for component in nx.strongly_connected_components(graph):
        if len(component) < 2:
            continue
        modules.update(component)
        edge_count += graph.subgraph(component).number_of_edges()
    return modules, edge_count


def _layer_violations(graph: nx.DiGraph) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for source, target in graph.edges():
        matched = False
        for source_prefix, target_prefix, reason in FORBIDDEN_LAYER_EDGES:
            source_match = source == source_prefix or source.startswith(source_prefix + ".")
            target_match = (
                target.startswith(target_prefix)
                if target_prefix.endswith(".")
                else target == target_prefix or target.startswith(target_prefix + ".")
            )
            if source_match and target_match:
                violations.append(
                    {"source": source, "target": target, "reason": reason}
                )
                matched = True
                break
        # The API package is the outermost interface.  Only API modules
        # themselves may depend on it; a new top-level probe or a domain module
        # importing it is always erosion.
        if not matched and target.startswith("src.api") and not source.startswith("src.api"):
            violations.append(
                {
                    "source": source,
                    "target": target,
                    "reason": "API layer may only be imported from src.api",
                }
            )
    return violations


def _line_budgets() -> dict[str, int]:
    budgets: dict[str, int] = {}
    for relative in LINE_BUDGETS:
        path = ROOT / relative
        budgets[relative] = len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
    return budgets


def _alter_count() -> int:
    """Count runtime ALTER TABLE string literals, not comments/docstrings.

    Versioned migrations under a ``migrations/`` package are the sanctioned
    place for schema changes, so they are excluded from the runtime ratchet.
    """
    count = 0
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts or "vendor" in path.parts or "migrations" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                count += len(_ALTER_RE.findall(node.value))
            elif isinstance(node, ast.JoinedStr):
                fragment = "".join(
                    part.value
                    for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                if _ALTER_RE.search(fragment):
                    count += 1
    return count


def _current_state() -> dict:
    graph, _known = _graph()
    scc_modules, scc_edges = _scc_state(graph)
    return {
        "version": 1,
        "scc_modules": sorted(scc_modules),
        "scc_edge_count": scc_edges,
        "forbidden_edges": [
            {"source": item["source"], "target": item["target"]}
            for item in sorted(
                _layer_violations(graph), key=lambda item: (item["source"], item["target"])
            )
        ],
        "line_budgets": _line_budgets(),
        "runtime_alter_count": _alter_count(),
    }


def _load_baseline() -> dict:
    if not BASELINE.exists():
        raise SystemExit(
            f"missing {BASELINE.name}; run `python scripts/architecture_check.py --write-baseline` "
            "once and commit the result"
        )
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _edge_key(item: dict[str, str]) -> str:
    return f"{item['source']} -> {item['target']}"


def check() -> list[str]:
    baseline = _load_baseline()
    current = _current_state()
    errors: list[str] = []

    allowed_scc = set(baseline.get("scc_modules", []))
    new_cycle_modules = sorted(set(current["scc_modules"]) - allowed_scc)
    if new_cycle_modules:
        errors.append(
            "new modules joined a circular dependency: "
            + ", ".join(new_cycle_modules[:20])
        )

    allowed_scc_edges = int(baseline.get("scc_edge_count", 0))
    if int(current["scc_edge_count"]) > allowed_scc_edges:
        errors.append(
            "edges inside circular dependencies grew: "
            f"{current['scc_edge_count']} > {allowed_scc_edges}"
        )

    allowed_forbidden = {
        _edge_key(item) for item in baseline.get("forbidden_edges", [])
    }
    current_forbidden = {_edge_key(item) for item in current["forbidden_edges"]}
    new_forbidden = sorted(current_forbidden - allowed_forbidden)
    if new_forbidden:
        errors.append(
            "new reverse-layer dependencies: " + ", ".join(new_forbidden[:20])
        )

    allowed_budgets = baseline.get("line_budgets", {})
    for relative, current_lines in current["line_budgets"].items():
        allowed = int(allowed_budgets.get(relative, current_lines))
        ceiling = allowed + LINE_BUDGET_TOLERANCE
        if current_lines > ceiling:
            errors.append(
                f"{relative} grew from {allowed} to {current_lines} lines "
                f"(allowance {LINE_BUDGET_TOLERANCE}; split it instead of growing the god class)"
            )

    allowed_alters = int(baseline.get("runtime_alter_count", 0))
    if int(current["runtime_alter_count"]) > allowed_alters:
        errors.append(
            "runtime ALTER TABLE count grew: "
            f"{current['runtime_alter_count']} > {allowed_alters} "
            "(use a versioned migration)"
        )

    return errors


def _summary(state: dict) -> str:
    return (
        f"SCC modules={len(state['scc_modules'])} "
        f"SCC edges={state['scc_edge_count']} "
        f"forbidden edges={len(state['forbidden_edges'])} "
        f"runtime ALTER={state['runtime_alter_count']}"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="overwrite architecture_baseline.json with the current graph",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.write_baseline:
        state = _current_state()
        BASELINE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {BASELINE.name}: {_summary(state)}")
        return 0

    errors = check()
    if errors:
        print("architecture gate failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"architecture gate passed: {_summary(_current_state())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
