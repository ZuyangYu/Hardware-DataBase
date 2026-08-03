#!/usr/bin/env python3
"""Dry-run-first command line wrapper for evaluation history imports."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


if __package__ in (None, ""):
    # Allow ``python scripts/import_evaluation_history.py`` from any working
    # directory without requiring an editable package installation.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.history_import import (  # noqa: E402
    ImportApplyError,
    apply_import_plan,
    discover_imports,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and (with --apply) atomically import complete evaluation history reports."
    )
    parser.add_argument("source_root", nargs="?", type=Path, help="directory containing historical run directories")
    parser.add_argument("target_root", nargs="?", type=Path, help="directory in which imported runs are published")
    parser.add_argument("--source-root", dest="source_root_option", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--target-root", dest="target_root_option", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="publish eligible reports; without this flag the command only performs a dry-run",
    )
    return parser


def _resolve_roots(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[Path, Path]:
    source_root = args.source_root_option or args.source_root
    target_root = args.target_root_option or args.target_root
    if source_root is None or target_root is None:
        parser.error("source_root and target_root are required")
    return source_root, target_root


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    source_root, target_root = _resolve_roots(args, parser)
    try:
        plan = discover_imports(source_root, target_root)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    mode = "apply" if args.apply else "dry-run"
    print(f"{mode}: {plan.source_root} -> {plan.target_root}")
    for candidate in plan.candidates:
        detail = f"; {candidate.reason}" if candidate.reason else ""
        print(f"{candidate.status}\t{candidate.source_path.name}{detail}")

    if not args.apply:
        if plan.has_conflicts:
            print("conflict(s) found; re-run with a resolved target or omit --apply", file=sys.stderr)
            return 1
        return 0

    try:
        result = apply_import_plan(plan)
    except ImportApplyError as exc:
        print(f"apply failed: {exc}", file=sys.stderr)
        return 1

    print(f"published={len(result.published)} skipped={len(result.skipped)} invalid={len(result.invalid)} conflicts={len(result.conflicts)}")
    return 1 if result.conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
