from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .dataset_loader import DatasetValidationError, load_dataset, validate_dataset
from .reporters import write_reports
from .schemas import MetricResult
from .service import EvaluationService, new_run_id
from .snapshot_store import SnapshotStore


CHECKPOINT_FILE = ".score_checkpoint.json"


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
    parser = argparse.ArgumentParser(prog="hardware-database-eval", description="Evaluate Hardware DataBase answers")
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


def _load_checkpoint(path: Path) -> dict:
    if not path.is_file():
        return {"done_groups": [], "metrics": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"done_groups": [], "metrics": {}}
    data.setdefault("done_groups", [])
    data.setdefault("metrics", {})
    return data


def _write_checkpoint(path: Path, done_groups: list[str], ordered_results) -> None:
    payload = {
        "done_groups": done_groups,
        "metrics": {
            result.sample_id: [metric.model_dump(mode="json") for metric in result.metrics]
            for result in ordered_results
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run_score(args, service: EvaluationService) -> int:
    samples = _filtered_samples(args)
    snapshots = SnapshotStore(args.snapshot).load_all()
    run_dir = args.output / new_run_id()
    run_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = run_dir / CHECKPOINT_FILE
    checkpoint = _load_checkpoint(checkpoint_path)
    done_groups = list(checkpoint.get("done_groups") or [])
    seeded_metrics = {
        sample_id: [MetricResult.model_validate(item) for item in items]
        for sample_id, items in (checkpoint.get("metrics") or {}).items()
    }

    def on_group_done(summary, ordered_results, completed, total) -> bool:
        done = sorted(
            {
                *done_groups,
                *{
                    metric.metric_name
                    for result in ordered_results
                    for metric in result.metrics
                },
            }
        )
        _write_checkpoint(checkpoint_path, done, ordered_results)
        return True

    thresholds = parse_thresholds(args.threshold)
    summary, results = service.score(
        samples,
        snapshots,
        metric_names=args.metric,
        thresholds=thresholds,
        fail_on_threshold=args.fail_on_threshold,
        completed_groups=set(done_groups),
        seeded_metrics=seeded_metrics,
        progress_callback=on_group_done,
    )
    paths = write_reports(run_dir, summary, results)
    checkpoint_path.unlink(missing_ok=True)
    _write_run_state(
        run_dir,
        run_id=summary.run_id,
        dataset_path=str(args.dataset),
        mode="offline",
        summary=summary,
        total_samples=len(samples),
        metrics=args.metric,
        report_path=str(paths.report_html),
    )
    print(f"report: {paths.report_html}")
    return summary.gate.exit_code if summary.gate else 0


def _write_run_state(
    run_dir: Path,
    *,
    run_id: str,
    dataset_path: str,
    mode: str,
    summary,
    total_samples: int,
    metrics: list[str] | None,
    report_path: str,
) -> None:
    from .schemas import EvaluationRunState

    state = EvaluationRunState(
        run_id=run_id,
        dataset_path=dataset_path,
        snapshot_path="",
        mode=mode,
        score_enabled=bool(metrics),
        status="completed",
        total_samples=total_samples,
        completed_samples=total_samples,
        successful_samples=summary.successful_samples,
        failed_samples=summary.failed_samples,
        scoring_completed_groups=summary.metadata.get("run_outcome", {}).get(
            "completed_groups", len(metrics or [])
        ),
        scoring_total_groups=summary.metadata.get("run_outcome", {}).get(
            "total_groups", len(metrics or [])
        ),
        scoring_completed_items=summary.scoring_completed_items,
        scoring_total_items=summary.scoring_total_items,
        finished_at=summary.created_at,
        updated_at=summary.created_at,
        report_path=report_path,
    )
    (run_dir / "run_state.json").write_text(
        state.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


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
            return _run_score(args, service)

        summary, _, paths = service.run(
            args.dataset,
            args.output,
            metric_names=args.metric,
            thresholds=thresholds,
            fail_on_threshold=args.fail_on_threshold,
            sample_ids=set(args.sample_id),
            tags=set(args.tag),
        )
        _write_run_state(
            paths.report_html.parent,
            run_id=summary.run_id,
            dataset_path=str(args.dataset),
            mode="online",
            summary=summary,
            total_samples=summary.sample_count,
            metrics=args.metric,
            report_path=str(paths.report_html),
        )
        print(f"report: {paths.report_html}")
        return summary.gate.exit_code if summary.gate else 0
    except (DatasetValidationError, ValueError, OSError, RuntimeError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
