from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .dataset_loader import DatasetValidationError, load_dataset, validate_dataset
from .reporters import write_reports
from .service import EvaluationService, new_run_id
from .snapshot_store import SnapshotStore


def parse_thresholds(values: list[str] | None) -> dict[str, float] | None:
    if not values:
        return None
    thresholds: dict[str, float] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"threshold must use name=value: {value}")
        name, raw_score = value.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError("threshold metric name must not be blank")
        try:
            score = float(raw_score)
        except ValueError as exc:
            raise ValueError(f"threshold is not a number: {value}") from exc
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"threshold must be between 0 and 1: {value}")
        thresholds[name] = score
    return thresholds


def _add_dataset(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", required=True, type=Path)


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])


def _add_scoring(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--metric", action="append", default=None)
    parser.add_argument("--threshold", action="append", default=[])
    parser.add_argument("--fail-on-threshold", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hardware-rag-eval", description="Evaluate Hardware RAG answers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate an evaluation JSONL dataset")
    _add_dataset(validate_parser)

    collect_parser = subparsers.add_parser("collect", help="collect answers and retrieval snapshots")
    _add_dataset(collect_parser)
    collect_parser.add_argument("--output", required=True, type=Path)
    collect_parser.add_argument("--no-resume", action="store_true")
    _add_filters(collect_parser)

    score_parser = subparsers.add_parser("score", help="score an existing answer snapshot")
    _add_dataset(score_parser)
    score_parser.add_argument("--snapshot", required=True, type=Path)
    score_parser.add_argument("--output", required=True, type=Path)
    _add_filters(score_parser)
    _add_scoring(score_parser)

    run_parser = subparsers.add_parser("run", help="collect, score and report in one command")
    _add_dataset(run_parser)
    run_parser.add_argument("--output", required=True, type=Path)
    _add_filters(run_parser)
    _add_scoring(run_parser)
    return parser


def _filtered_samples(args) -> list:
    samples = load_dataset(args.dataset)
    ids = set(getattr(args, "sample_id", []) or [])
    tags = set(getattr(args, "tag", []) or [])
    if ids:
        samples = [sample for sample in samples if sample.id in ids]
    if tags:
        samples = [sample for sample in samples if tags.intersection(sample.tags)]
    return samples


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[], EvaluationService] = EvaluationService,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            errors = validate_dataset(args.dataset)
            if errors:
                for error in errors:
                    print(error, file=sys.stderr)
                return 1
            print(f"dataset valid: {args.dataset}")
            return 0

        service = service_factory()
        if args.command == "collect":
            samples = _filtered_samples(args)
            snapshot_path = args.output / new_run_id() / "snapshot.jsonl"
            snapshots = service.collect(samples, snapshot_path, resume=not args.no_resume)
            print(f"collected {len(snapshots)} snapshots: {snapshot_path}")
            return 0

        thresholds = parse_thresholds(args.threshold)
        if args.command == "score":
            samples = _filtered_samples(args)
            snapshots = SnapshotStore(args.snapshot).load_all()
            run_dir = args.output / new_run_id()
            summary, results = service.score(
                samples,
                snapshots,
                metric_names=args.metric,
                thresholds=thresholds,
                fail_on_threshold=args.fail_on_threshold,
            )
            paths = write_reports(run_dir, summary, results)
            print(f"report: {paths.report_html}")
            return summary.gate.exit_code if summary.gate else 0

        summary, _, paths = service.run(
            args.dataset,
            args.output,
            metric_names=args.metric,
            thresholds=thresholds,
            fail_on_threshold=args.fail_on_threshold,
            sample_ids=set(args.sample_id),
            tags=set(args.tag),
        )
        print(f"report: {paths.report_html}")
        return summary.gate.exit_code if summary.gate else 0
    except (DatasetValidationError, ValueError, OSError, RuntimeError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
