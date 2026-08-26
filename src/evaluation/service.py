from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.core.app_pipeline import AppPipeline
from src.observability import observe, submit_with_current_context
from src.observability.metrics import (
    record_evaluation_sample,
    record_evaluation_score,
)

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
                        futures[submit_with_current_context(executor, self._collect_sample, sample)] = sample
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
            snapshot = self._collect_sample(sample)
            store.append(snapshot)
            completed_count += 1
            if after_sample is not None:
                after_sample(snapshot, completed_count, total)
        return store.load_all()

    def _collect_sample(self, sample: EvaluationSample) -> AnswerSnapshot:
        started = datetime.now(timezone.utc).timestamp()
        status = "success"
        with observe.evaluator(
            "hdb.evaluation.sample",
            sample_id=sample.id,
            stage="collect",
        ) as observation:
            try:
                snapshot = self.answer_runner.collect(sample)
                status = snapshot.status
                observation.set("hdb.evaluation.sample.status", status)
                return snapshot
            except Exception:
                status = "failed"
                raise
            finally:
                record_evaluation_sample(
                    status=status,
                    duration_s=max(0.0, datetime.now(timezone.utc).timestamp() - started),
                    mode="online",
                )

    def preflight_online(self, samples: list[EvaluationSample]) -> list[str]:
        return EvaluationPreflight(self._pipeline_factory).validate(samples)

    def preflight_scoring(self) -> list[str]:
        """Check native RAGAS prerequisites before a score-enabled run starts."""

        adapter = self.ragas_adapter
        # Injected adapters are already responsible for their own runtime. In
        # tests and alternative deployments they may not use the optional
        # native RAGAS packages at all.
        if adapter is not None and not (
            isinstance(adapter, RagasAdapter) and getattr(adapter, "_backend", None) is None
        ):
            return []
        config = adapter.config if isinstance(adapter, RagasAdapter) else self.config
        return EvaluationPreflight.validate_scoring(config=config)

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
        scoring_config_metadata: dict[str, str | int | float] = {}
        if self.config is not None:
            scoring_config_metadata = self.config.public_metadata()
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
                    scoring_config_metadata = config.public_metadata()
                elif isinstance(adapter, RagasAdapter):
                    scoring_config_metadata = adapter.config.public_metadata()
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
        if scoring_config_metadata:
            summary.metadata["scoring_config"] = scoring_config_metadata
        for metric_name, score in summary.metric_scores.items():
            try:
                record_evaluation_score(metric=metric_name, score=float(score))
            except (TypeError, ValueError):
                continue
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
                },
                "scoring_diagnostics": EvaluationService._build_scoring_diagnostics(
                    ordered_results
                ),
            },
        )
        return summary

    @staticmethod
    def _build_scoring_diagnostics(
        results: list[SampleResult],
    ) -> dict[str, object]:
        """Summarize conditions that can make a score misleading.

        This is deliberately separate from the Gate: a low score is a quality
        signal, while missing evidence, judge failures, and truncated inputs
        are execution conditions that must be fixed or acknowledged first.
        """

        collection_failures = sum(result.snapshot_status == "failed" for result in results)
        evidence_samples = sum(bool(result.retrieved_contexts) for result in results)
        no_evidence_samples = sum(
            result.snapshot_status == "success" and not result.retrieved_contexts
            for result in results
        )
        metric_failures = sum(
            metric.status == "failed"
            for result in results
            for metric in result.metrics
        )
        truncated_samples = sum(
            bool((result.metadata.get("ragas_scoring") or {}).get("contexts_truncated"))
            for result in results
        )

        retrieval_status_counts: Counter[str] = Counter()
        retrieval_partial_failures = 0
        metric_stats: dict[str, dict[str, int]] = {}
        for result in results:
            retrieval_summary = result.metadata.get("retrieval_summary") or {}
            if retrieval_summary:
                status = str(retrieval_summary.get("status") or "unknown")
                retrieval_status_counts[status] += 1
                if status not in {"success", "unknown"}:
                    retrieval_partial_failures += 1
            for metric in result.metrics:
                stats = metric_stats.setdefault(
                    metric.metric_name,
                    {"success": 0, "failed": 0, "not_applicable": 0, "zero_scores": 0},
                )
                stats[metric.status] += 1
                if metric.status == "success" and metric.score == 0:
                    stats["zero_scores"] += 1

        all_zero_metrics = sorted(
            name
            for name, stats in metric_stats.items()
            if stats["success"] > 0
            and stats["zero_scores"] == stats["success"]
        )
        warnings: list[str] = []
        if collection_failures:
            warnings.append(f"{collection_failures} 个样本采集失败，不能用整体分数代表完整数据集。")
        if no_evidence_samples:
            warnings.append(
                f"{no_evidence_samples} 个成功样本没有检索证据；上下文相关指标不应按普通低分解读。"
            )
        if retrieval_partial_failures:
            warnings.append(
                f"{retrieval_partial_failures} 个样本的检索状态不是 success，可能导致答案和上下文指标被低估。"
            )
        if truncated_samples:
            warnings.append(
                f"{truncated_samples} 个样本的评分上下文经过了数量或字符裁剪；请结合上下文选择诊断解读分数。"
            )
        if metric_failures:
            warnings.append(f"有 {metric_failures} 条评分任务失败，失败项不应当当作 0 分。")
        if all_zero_metrics:
            warnings.append(
                "以下指标的有效评分样本全部为 0："
                + "、".join(all_zero_metrics)
                + "；优先检查参考答案/上下文对齐和评估模型兼容性。"
            )

        if collection_failures or metric_failures:
            status = "technical_failure"
        elif not any(stats["success"] for stats in metric_stats.values()):
            status = "insufficient_coverage"
        elif warnings:
            status = "interpret_with_caution"
        else:
            status = "ready"

        return {
            "status": status,
            "collection_failures": collection_failures,
            "evidence_samples": evidence_samples,
            "no_evidence_samples": no_evidence_samples,
            "retrieval_status_counts": dict(retrieval_status_counts),
            "retrieval_partial_failures": retrieval_partial_failures,
            "truncated_context_samples": truncated_samples,
            "metric_failures": metric_failures,
            "metric_stats": metric_stats,
            "all_zero_metrics": all_zero_metrics,
            "warnings": warnings,
        }

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
                "config": summary.metadata.get("scoring_config")
                or (self.config.public_metadata() if self.config else {}),
            },
        )
        return summary, results, paths
