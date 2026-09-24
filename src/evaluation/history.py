"""Read-only helpers for displaying and comparing saved evaluation reports.

The report files are treated as immutable history.  This module deliberately
does not repair, enrich, or rewrite a report while loading it; a malformed
``results.jsonl`` line is rejected so the UI cannot silently compare partial
data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .schemas import EvaluationSummary, SampleResult


def _canonical_sample_ids(sample_ids: Iterable[str]) -> list[str]:
    """Return sorted unique, trimmed, nonblank sample IDs."""

    return sorted(
        {
            str(sample_id).strip()
            for sample_id in sample_ids
            if str(sample_id).strip()
        }
    )


def cohort_fingerprint(sample_ids: Iterable[str]) -> str:
    """Hash the canonical JSON representation of a sample-ID cohort.

    Sorting and de-duplicating makes the fingerprint independent of report
    result order and accidental duplicate rows.  Empty input still has a
    deterministic digest; callers that need a usable cohort must additionally
    require a non-empty ID set/fingerprint source.
    """

    canonical = json.dumps(
        _canonical_sample_ids(sample_ids),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass
class EvaluationHistoryRun:
    """Parsed, immutable-on-disk view of one saved evaluation report."""

    run_dir: Path
    summary: EvaluationSummary
    results: list[SampleResult] = field(default_factory=list)
    sample_ids: list[str] = field(default_factory=list)
    sample_count: int = 0
    cohort_fingerprint: str = ""
    origin: str = "local"
    validation_warnings: list[str] = field(default_factory=list)

    @property
    def run_name(self) -> str:
        return self.run_dir.name

    @property
    def run_id(self) -> str:
        return self.summary.run_id

    @property
    def path(self) -> Path:
        """Alias useful to callers that refer to a report as a path."""

        return self.run_dir

    @property
    def fingerprint(self) -> str:
        """Short alias for the cohort fingerprint."""

        return self.cohort_fingerprint

    @property
    def origin_label(self) -> str:
        return "导入" if self.origin == "imported" else "本地"


def _warning_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        for key in ("message", "warning", "reason", "detail"):
            if value.get(key):
                return _warning_values(value[key])
        return []
    if isinstance(value, (list, tuple, set)):
        warnings: list[str] = []
        for item in value:
            warnings.extend(_warning_values(item))
        return warnings
    return [str(value).strip()] if str(value).strip() else []


def _manifest_origin(manifest: dict[str, Any]) -> str:
    raw = (
        manifest.get("origin")
        or manifest.get("run_origin")
        or manifest.get("import_origin")
        or manifest.get("source")
        or manifest.get("kind")
    )
    if isinstance(raw, dict):
        raw = raw.get("type") or raw.get("kind") or raw.get("name")
    if manifest.get("imported") is True or manifest.get("is_imported") is True:
        return "imported"
    normalized = str(raw or "").strip().casefold().replace("-", "_")
    if normalized in {"local", "local_run", "native", "generated", "本地"}:
        return "local"
    if normalized in {
        "import",
        "imported",
        "external",
        "upload",
        "uploaded",
        "导入",
    }:
        return "imported"
    # The history importer records provenance fields rather than a separate
    # origin enum.  A present sidecar therefore identifies an imported run
    # unless it explicitly says ``local`` above.
    if any(
        key in manifest
        for key in (
            "source_root",
            "source_path",
            "source_directory_name",
            "imported_at",
            "file_sha256",
        )
    ):
        return "imported"
    return "local"


def _manifest_warnings(manifest: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for key in ("validation_warnings", "warnings", "validationWarnings"):
        warnings.extend(_warning_values(manifest.get(key)))
    validation = manifest.get("validation")
    if isinstance(validation, dict):
        warnings.extend(
            _warning_values(
                validation.get("warnings") or validation.get("validation_warnings")
            )
        )
    return warnings


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _load_summary(path: Path) -> EvaluationSummary:
    try:
        return EvaluationSummary.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid evaluation summary: {exc}") from exc


def _load_results(path: Path) -> list[SampleResult]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"unable to read results.jsonl: {exc}") from exc
    results: list[SampleResult] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            results.append(SampleResult.model_validate_json(line))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"invalid results.jsonl record at line {line_number}: {exc}"
            ) from exc
    return results


def load_history_run(run_dir: Path) -> EvaluationHistoryRun:
    """Load one saved report and derive its cohort metadata.

    A missing ``import_manifest.json`` is the legacy/local format and is
    intentionally accepted.  Import metadata is advisory display data; the
    report summary and every result record are still validated strictly.
    """

    run_dir = Path(run_dir)
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file():
        raise ValueError(f"evaluation summary is missing: {summary_path}")
    summary = _load_summary(summary_path)
    results = _load_results(run_dir / "results.jsonl")
    sample_ids = _canonical_sample_ids(result.sample_id for result in results)
    fingerprint = cohort_fingerprint(sample_ids) if sample_ids else ""

    origin = "local"
    warnings: list[str] = []
    manifest_path = run_dir / "import_manifest.json"
    if manifest_path.is_file():
        manifest = _read_json_object(manifest_path)
        origin = _manifest_origin(manifest)
        warnings.extend(_manifest_warnings(manifest))

    metadata = summary.metadata or {}
    for key in ("validation_warnings", "warnings", "validationWarnings"):
        warnings.extend(_warning_values(metadata.get(key)))
    # Preserve order while avoiding duplicate notices from a sidecar and the
    # summary metadata carrying the same validation result.
    warnings = list(dict.fromkeys(warning for warning in warnings if warning))

    return EvaluationHistoryRun(
        run_dir=run_dir,
        summary=summary,
        results=results,
        sample_ids=sample_ids,
        sample_count=len(sample_ids),
        cohort_fingerprint=fingerprint,
        origin=origin,
        validation_warnings=warnings,
    )


def compatible_baselines(
    selected: EvaluationHistoryRun,
    candidates: Iterable[EvaluationHistoryRun],
) -> list[EvaluationHistoryRun]:
    """Return distinct candidate runs with the same non-empty sample cohort."""

    fingerprint = selected.cohort_fingerprint
    if not fingerprint or not selected.sample_ids:
        return []
    selected_path = selected.run_dir.resolve()
    compatible: list[EvaluationHistoryRun] = []
    for candidate in candidates:
        if candidate.run_dir.resolve() == selected_path:
            continue
        if candidate.cohort_fingerprint and candidate.sample_ids:
            if candidate.cohort_fingerprint == fingerprint:
                compatible.append(candidate)
    return compatible


def _summary_metric_names(history: EvaluationHistoryRun) -> list[str]:
    names = set(history.summary.metric_scores)
    names.update(history.summary.metric_counts)
    if not names:
        for result in history.results:
            names.update(metric.metric_name for metric in result.metrics)
    return sorted(str(name) for name in names)


def _model_config(history: EvaluationHistoryRun) -> dict[str, Any]:
    """Return only the public evaluator configuration used for comparison."""

    config = dict(history.summary.evaluation_config or {})
    if history.summary.llm_model:
        config["llm_model"] = history.summary.llm_model
    if history.summary.embedding_model:
        config["embedding_model"] = history.summary.embedding_model
    return config


def _comparison_item(current: Any, baseline: Any, *, reason: str = "") -> dict[str, Any]:
    return {
        "match": current == baseline,
        "current": current,
        "baseline": baseline,
        "reason": reason,
    }


def compatibility_report(
    selected: EvaluationHistoryRun,
    baseline: EvaluationHistoryRun,
) -> dict[str, Any]:
    """Explain whether two immutable reports are safe for strict comparison.

    Missing metadata is intentionally a mismatch.  This keeps pre-metadata
    legacy reports viewable while preventing them from being presented as a
    reproducible baseline by accident.
    """

    current_summary = selected.summary
    baseline_summary = baseline.summary
    current_kb = current_summary.kb_id
    baseline_kb = baseline_summary.kb_id
    kb_match = current_kb is not None and baseline_kb is not None and current_kb == baseline_kb

    current_cohort = current_summary.cohort_fingerprint
    baseline_cohort = baseline_summary.cohort_fingerprint
    cohort_match = bool(current_cohort and baseline_cohort and current_cohort == baseline_cohort)

    current_metrics = _summary_metric_names(selected)
    baseline_metrics = _summary_metric_names(baseline)
    metrics_match = current_metrics == baseline_metrics

    current_config = _model_config(selected)
    baseline_config = _model_config(baseline)
    model_match = bool(current_config and baseline_config) and current_config == baseline_config

    current_snapshot_verified = bool(current_summary.snapshot_ownership_verified)
    baseline_snapshot_verified = bool(baseline_summary.snapshot_ownership_verified)

    current_ownership = {
        "kb_id": current_kb,
        "cohort_fingerprint": current_cohort,
        "verified": current_snapshot_verified,
    }
    baseline_ownership = {
        "kb_id": baseline_kb,
        "cohort_fingerprint": baseline_cohort,
        "verified": baseline_snapshot_verified,
    }
    ownership_match = (
        current_kb is not None
        and baseline_kb is not None
        and bool(current_cohort)
        and bool(baseline_cohort)
        and current_snapshot_verified
        and baseline_snapshot_verified
        and current_ownership == baseline_ownership
    )

    compatibility = {
        "kb_id": _comparison_item(
            current_kb,
            baseline_kb,
            reason="两次运行必须绑定同一个 kb_id" if not kb_match else "",
        ),
        "cohort_fingerprint": _comparison_item(
            current_cohort,
            baseline_cohort,
            reason="样本集指纹缺失或不一致" if not cohort_match else "",
        ),
        "metrics": _comparison_item(
            current_metrics,
            baseline_metrics,
            reason="指标集合缺失或不一致" if not metrics_match else "",
        ),
        "model_config": _comparison_item(
            current_config,
            baseline_config,
            reason="评估模型配置缺失或不一致" if not model_match else "",
        ),
        "snapshot_ownership": _comparison_item(
            current_ownership,
            baseline_ownership,
            reason="快照归属缺失或与知识库/样本集不一致" if not ownership_match else "",
        ),
    }
    warnings = [
        f"{key}: {item['reason']}"
        for key, item in compatibility.items()
        if not item["match"] and item["reason"]
    ]
    return {
        "compatible": all(
            compatibility[key]["match"]
            for key in (
                "kb_id",
                "cohort_fingerprint",
                "metrics",
                "model_config",
                "snapshot_ownership",
            )
        ),
        "warnings": warnings,
        "compatibility": compatibility,
    }
