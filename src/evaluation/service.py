from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.core.app_pipeline import AppPipeline

from .answer_runner import AnswerRunner
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
    ) -> tuple[EvaluationSummary, list[SampleResult]]:
        metric_names = DEFAULT_STANDARD_METRICS if metric_names is None else metric_names
        snapshot_by_id = {snapshot.sample_id: snapshot for snapshot in snapshots}
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
            if snapshot is not None and snapshot.retrieval_summary:
                sample_results[sample.id].metadata["retrieval_summary"] = (
                    snapshot.retrieval_summary
                )
            if snapshot is not None and snapshot.metadata.get("scored_response_filter"):
                sample_results[sample.id].metadata["scored_response_filter"] = (
                    snapshot.metadata["scored_response_filter"]
                )

        if metric_names:
            adapter = self.ragas_adapter
            if adapter is None:
                config = self.config or EvaluationConfig.from_environment()
                adapter = RagasAdapter(config)
            scoring_snapshots = snapshots
            if isinstance(adapter, RagasAdapter):
                scoring_snapshots, diagnostics = adapter.prepare_snapshots_for_scoring(snapshots)
                for sample_id, diagnostic in diagnostics.items():
                    if sample_id in sample_results:
                        sample_results[sample_id].metadata["ragas_scoring"] = diagnostic
                metrics = adapter.score(
                    samples,
                    scoring_snapshots,
                    metric_names,
                    snapshots_prepared=True,
                )
            else:
                metrics = adapter.score(samples, snapshots, metric_names)
            for metric in metrics:
                sample_results[metric.sample_id].metrics.append(metric)

        ordered_results = [sample_results[sample.id] for sample in samples]
        gate = evaluate_gate(
            ordered_results,
            thresholds or DEFAULT_THRESHOLDS,
            fail_on_threshold=fail_on_threshold,
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
            gate=gate,
        )
        return summary, ordered_results

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
