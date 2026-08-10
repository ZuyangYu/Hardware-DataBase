from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.core.app_pipeline import AppPipeline

from .answer_runner import AnswerRunner
from .cohorts import evaluation_cohort, is_ragas_metric
from .config import EvaluationConfig
from .dataset_loader import load_dataset, validate_dataset
from .gates import DEFAULT_THRESHOLDS, evaluate_gate
from .hardware_metrics import score_hardware_rules
from .preflight import EvaluationPreflight
from .ragas_adapter import RagasAdapter
from .schemas import (
    AnswerSnapshot,
    EvaluationSample,
    EvaluationSummary,
    MetricResult,
    SampleResult,
)
from .snapshot_store import SnapshotStore


DEFAULT_STANDARD_METRICS = [
    "answer_correctness",
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
]


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class EvaluationService:
    def __init__(
        self,
        *,
        ragas_adapter=None,
        pipeline_factory: Callable[[], object] = AppPipeline,
        config: EvaluationConfig | None = None,
    ):
        self.config = config
        self.ragas_adapter = ragas_adapter
        self._pipeline_factory = pipeline_factory
        self.answer_runner = AnswerRunner(pipeline_factory)

    @staticmethod
    def validate(dataset_path: str | Path) -> list[str]:
        return validate_dataset(dataset_path)

    def collect(
        self,
        samples: list[EvaluationSample],
        snapshot_path: str | Path,
        *,
        resume: bool = True,
        before_sample: Callable[[EvaluationSample, int, int], bool] | None = None,
        after_sample: Callable[[AnswerSnapshot, int, int], None] | None = None,
    ) -> list[AnswerSnapshot]:
        store = SnapshotStore(snapshot_path)
        completed_ids = store.completed_ids() if resume else set()
        completed_count = len(completed_ids)
        total = len(samples)
        pending_samples = [sample for sample in samples if sample.id not in completed_ids]
        max_workers = (self.config or EvaluationConfig.from_environment()).max_workers
        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                pending_iterator = iter(pending_samples)
                futures = {}
                submitting = True
                while submitting or futures:
                    while submitting and len(futures) < max_workers:
                        try:
                            sample = next(pending_iterator)
                        except StopIteration:
                            submitting = False
                            break
                        if before_sample is not None and not before_sample(
                            sample, completed_count, total
                        ):
                            submitting = False
                            break
                        futures[executor.submit(self.answer_runner.collect, sample)] = sample
                    if not futures:
                        continue
                    future = next(as_completed(futures))
                    snapshot = future.result()
                    del futures[future]
                    store.append(snapshot)
                    completed_count += 1
                    if after_sample is not None:
                        after_sample(snapshot, completed_count, total)
            return store.load_all()
        for sample in samples:
            if sample.id in completed_ids:
                continue
            if before_sample is not None and not before_sample(sample, completed_count, total):
                break
            snapshot = self.answer_runner.collect(sample)
            store.append(snapshot)
            completed_count += 1
            if after_sample is not None:
                after_sample(snapshot, completed_count, total)
        return store.load_all()

    def preflight_online(self, samples: list[EvaluationSample]) -> list[str]:
        return EvaluationPreflight(self._pipeline_factory).validate(samples)

    def score(
        self,
        samples: list[EvaluationSample],
        snapshots: list[AnswerSnapshot],
        *,
        metric_names: list[str] | None = None,
        thresholds: dict[str, float] | None = None,
        fail_on_threshold: bool = False,
        run_id: str | None = None,
        progress_callback: Callable[[EvaluationSummary, list[SampleResult], int, int], bool] | None = None,
        item_progress_callback: Callable[[EvaluationSummary, list[SampleResult], int, int], None]
        | None = None,
    ) -> tuple[EvaluationSummary, list[SampleResult]]:
        metric_names = DEFAULT_STANDARD_METRICS if metric_names is None else metric_names
        scoring_skipped_reason = ""
        if metric_names and samples and not any(snapshot.status == "success" for snapshot in snapshots):
            metric_names = []
            scoring_skipped_reason = "no_successful_snapshots"
        snapshot_by_id = {snapshot.sample_id: snapshot for snapshot in snapshots}
        sample_cohorts = {sample.id: evaluation_cohort(sample) for sample in samples}
        sample_results: dict[str, SampleResult] = {}
        for sample in samples:
            snapshot = snapshot_by_id.get(sample.id)
            status = snapshot.status if snapshot is not None else "failed"
            metrics = score_hardware_rules(sample, snapshot) if snapshot is not None and status == "success" else []
            sample_results[sample.id] = SampleResult(
                sample_id=sample.id,
                question=sample.question,
                reference_answer=sample.reference_answer,
                response=snapshot.response if snapshot is not None else "",
                scored_response=(snapshot.scored_response or snapshot.response) if snapshot is not None else "",
                retrieved_contexts=snapshot.retrieved_contexts if snapshot is not None else [],
                critical=sample.critical,
                snapshot_status=status,
                metrics=metrics,
            )
            sample_results[sample.id].metadata["evaluation_cohort"] = sample_cohorts[sample.id]
            if snapshot is not None and snapshot.retrieval_summary:
                sample_results[sample.id].metadata["retrieval_summary"] = (
                    snapshot.retrieval_summary
                )
            if snapshot is not None and snapshot.metadata.get("scored_response_filter"):
                sample_results[sample.id].metadata["scored_response_filter"] = (
                    snapshot.metadata["scored_response_filter"]
                )

        total_groups = 0
        completed_groups = 0
        completed_items = 0
        total_items = 0
        stopped = False
        if metric_names:
            retrieval_samples = [
                sample for sample in samples if sample_cohorts[sample.id] == "retrieval"
            ]
            retrieval_ids = {sample.id for sample in retrieval_samples}
            retrieval_snapshots = [
                snapshot for snapshot in snapshots if snapshot.sample_id in retrieval_ids
            ]
            for sample in samples:
                if sample_cohorts[sample.id] != "non_retrieval":
                    continue
                for metric_name in metric_names:
                    if is_ragas_metric(metric_name):
                        sample_results[sample.id].metrics.append(
                            MetricResult(
                                sample_id=sample.id,
                                metric_name=metric_name,
                                status="not_applicable",
                                reason="sample intentionally does not use knowledge-base retrieval",
                            )
                        )
            if retrieval_samples:
                adapter = self.ragas_adapter
                if adapter is None:
                    config = self.config or EvaluationConfig.from_environment()
                    adapter = RagasAdapter(config)
                scoring_snapshots = retrieval_snapshots
                total_groups = len(metric_names)
                total_items = len(retrieval_samples) * len(metric_names)
                if isinstance(adapter, RagasAdapter):
                    scoring_snapshots, diagnostics = adapter.prepare_snapshots_for_scoring(
                        retrieval_snapshots
                    )
                    for sample_id, diagnostic in diagnostics.items():
                        if sample_id in sample_results:
                            sample_results[sample_id].metadata["ragas_scoring"] = diagnostic
                else:
                    scoring_snapshots = retrieval_snapshots
                for metric_name in metric_names:
                    emitted_keys: set[tuple[str, str]] = set()

                    def on_metric_result(metric: MetricResult) -> None:
                        nonlocal completed_items
                        key = (metric.sample_id, metric.metric_name)
                        emitted_keys.add(key)
                        target = sample_results[metric.sample_id]
                        if not any(item.metric_name == metric.metric_name for item in target.metrics):
                            target.metrics.append(metric)
                        completed_items += 1
                        if item_progress_callback is not None:
                            ordered = [sample_results[sample.id] for sample in samples]
                            current_summary = self._build_summary(
                                samples,
                                ordered,
                                thresholds=thresholds,
                                fail_on_threshold=fail_on_threshold,
                                run_id=run_id,
                                completed_groups=completed_groups,
                                total_groups=total_groups,
                                completed_items=completed_items,
                                total_items=total_items,
                                outcome_kind="in_progress",
                            )
                            item_progress_callback(
                                current_summary,
                                ordered,
                                completed_items,
                                total_items,
                            )

                    if isinstance(adapter, RagasAdapter):
                        metrics = adapter.score(
                            retrieval_samples,
                            scoring_snapshots,
                            [metric_name],
                            snapshots_prepared=True,
                            on_result=on_metric_result if item_progress_callback is not None else None,
                        )
                    else:
                        metrics = adapter.score(retrieval_samples, scoring_snapshots, [metric_name])
                    for metric in metrics:
                        if metric.metric_name == metric_name and (
                            metric.sample_id,
                            metric.metric_name,
                        ) not in emitted_keys:
                            sample_results[metric.sample_id].metrics.append(metric)
                            emitted_keys.add((metric.sample_id, metric.metric_name))
                            completed_items += 1
                            if item_progress_callback is not None:
                                ordered = [sample_results[sample.id] for sample in samples]
                                current_summary = self._build_summary(
                                    samples,
                                    ordered,
                                    thresholds=thresholds,
                                    fail_on_threshold=fail_on_threshold,
                                    run_id=run_id,
                                    completed_groups=completed_groups,
                                    total_groups=total_groups,
                                    completed_items=completed_items,
                                    total_items=total_items,
                                    outcome_kind="in_progress",
                                )
                                item_progress_callback(
                                    current_summary,
                                    ordered,
                                    completed_items,
                                    total_items,
                                )
                    completed_groups += 1
                    ordered_results = [sample_results[sample.id] for sample in samples]
                    summary = self._build_summary(
                        samples,
                        ordered_results,
                        thresholds=thresholds,
                        fail_on_threshold=fail_on_threshold,
                        run_id=run_id,
                        completed_groups=completed_groups,
                        total_groups=total_groups,
                        completed_items=completed_items,
                        total_items=total_items,
                        outcome_kind="in_progress",
                    )
                    if progress_callback is not None and not progress_callback(
                        summary, ordered_results, completed_groups, total_groups
                    ):
                        stopped = True
                        break
                    if stopped:
                        break

        ordered_results = [sample_results[sample.id] for sample in samples]
        summary = self._build_summary(
            samples,
            ordered_results,
            thresholds=thresholds,
            fail_on_threshold=fail_on_threshold,
            run_id=run_id,
            completed_groups=completed_groups,
            total_groups=total_groups,
            completed_items=completed_items,
            total_items=total_items,
            outcome_kind="in_progress" if stopped else None,
        )
        if scoring_skipped_reason:
            summary.metadata["scoring_skipped"] = scoring_skipped_reason
        return summary, ordered_results

    @staticmethod
    def _build_summary(
        samples: list[EvaluationSample],
        ordered_results: list[SampleResult],
        *,
        thresholds: dict[str, float] | None,
        fail_on_threshold: bool,
        run_id: str | None,
        completed_groups: int,
        total_groups: int,
        completed_items: int = 0,
        total_items: int = 0,
        outcome_kind: str | None,
    ) -> EvaluationSummary:
        sample_cohorts = {sample.id: evaluation_cohort(sample) for sample in samples}
        gate = evaluate_gate(
            ordered_results,
            thresholds or DEFAULT_THRESHOLDS,
            fail_on_threshold=fail_on_threshold,
            sample_cohorts=sample_cohorts,
        )
        metric_failures = Counter(
            metric.metric_name
            for result in ordered_results
            for metric in result.metrics
            if metric.status == "failed"
        )
        successful = sum(result.snapshot_status == "success" for result in ordered_results)
        summary = EvaluationSummary(
            run_id=run_id or new_run_id(),
            sample_count=len(samples),
            successful_samples=successful,
            failed_samples=len(samples) - successful,
            metric_scores=gate.metric_scores,
            metric_counts=gate.metric_counts,
            metric_failures=dict(metric_failures),
            scoring_completed_items=completed_items,
            scoring_total_items=total_items,
            gate=gate,
            metadata={
                "run_outcome": {
                    "kind": outcome_kind or "completed",
                    "completed_groups": completed_groups,
                    "total_groups": total_groups,
                }
            },
        )
        return summary

    def run(
        self,
        dataset_path: str | Path,
        output_root: str | Path,
        *,
        metric_names: list[str] | None = None,
        thresholds: dict[str, float] | None = None,
        fail_on_threshold: bool = False,
        sample_ids: set[str] | None = None,
        tags: set[str] | None = None,
    ):
        from .reporters import write_manifest, write_reports

        samples = load_dataset(dataset_path)
        if sample_ids:
            samples = [sample for sample in samples if sample.id in sample_ids]
        if tags:
            samples = [sample for sample in samples if tags.intersection(sample.tags)]
        run_id = new_run_id()
        run_dir = Path(output_root) / run_id
        snapshots = self.collect(samples, run_dir / "snapshot.jsonl")
        summary, results = self.score(
            samples,
            snapshots,
            metric_names=metric_names,
            thresholds=thresholds,
            fail_on_threshold=fail_on_threshold,
            run_id=run_id,
        )
        paths = write_reports(run_dir, summary, results)
        digest = hashlib.sha256(Path(dataset_path).read_bytes()).hexdigest()
        write_manifest(
            run_dir,
            {
                "run_id": run_id,
                "dataset": str(dataset_path),
                "dataset_sha256": digest,
                "metrics": metric_names or DEFAULT_STANDARD_METRICS,
                "config": self.config.public_metadata() if self.config else {},
            },
        )
        return summary, results, paths
