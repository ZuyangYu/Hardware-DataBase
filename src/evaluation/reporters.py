from __future__ import annotations

import csv
import html
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schemas import EvaluationSummary, SampleResult


@dataclass(frozen=True)
class ReportPaths:
    summary_json: Path
    results_jsonl: Path
    summary_csv: Path
    report_html: Path


def _atomic_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temp_path.write_text(content, encoding=encoding)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_manifest(run_dir: str | Path, manifest: dict[str, Any]) -> Path:
    path = Path(run_dir) / "manifest.json"
    _atomic_text(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return path


def write_reports(
    run_dir: str | Path,
    summary: EvaluationSummary,
    results: list[SampleResult],
) -> ReportPaths:
    run_dir = Path(run_dir)
    summary_json = run_dir / "summary.json"
    results_jsonl = run_dir / "results.jsonl"
    summary_csv = run_dir / "summary.csv"
    report_html = run_dir / "report.html"

    _atomic_text(summary_json, summary.model_dump_json(indent=2) + "\n")
    _atomic_text(results_jsonl, "\n".join(item.model_dump_json() for item in results) + ("\n" if results else ""))

    metric_names = sorted({metric.metric_name for result in results for metric in result.metrics})
    csv_temp = summary_csv.with_suffix(summary_csv.suffix + ".tmp")
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        with csv_temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "sample_id",
                    "snapshot_status",
                    "question",
                    "reference_answer",
                    "response",
                    "retrieved_contexts",
                    *metric_names,
                ],
            )
            writer.writeheader()
            for result in results:
                row: dict[str, Any] = {
                    "sample_id": result.sample_id,
                    "snapshot_status": result.snapshot_status,
                    "question": result.question,
                    "reference_answer": result.reference_answer,
                    "response": result.response,
                    "retrieved_contexts": json.dumps(result.retrieved_contexts, ensure_ascii=False),
                }
                for metric in result.metrics:
                    row[metric.metric_name] = metric.score if metric.status == "success" else metric.status
                writer.writerow(row)
        os.replace(csv_temp, summary_csv)
    finally:
        if csv_temp.exists():
            csv_temp.unlink()

    headers = ["sample_id", "snapshot_status", *metric_names]
    rows = []
    for result in results:
        cells = [html.escape(result.sample_id), html.escape(result.snapshot_status)]
        by_name = {metric.metric_name: metric for metric in result.metrics}
        for name in metric_names:
            metric = by_name.get(name)
            value = "-" if metric is None else (f"{metric.score:.3f}" if metric.score is not None else metric.status)
            cells.append(html.escape(value))
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
        detail = (
            f"<strong>问题：</strong>{html.escape(result.question)}<br>"
            f"<strong>参考答案：</strong>{html.escape(result.reference_answer)}<br>"
            f"<strong>实际回答：</strong>{html.escape(result.response)}<br>"
            f"<strong>检索上下文：</strong>{html.escape(' | '.join(result.retrieved_contexts))}"
        )
        rows.append(f"<tr><td colspan='{len(headers)}'>{detail}</td></tr>")
    html_text = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Hardware DataBase Evaluation</title><style>body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:.4rem}th{background:#f5f5f5}</style></head><body>"""
    html_text += f"<h1>评估报告 {html.escape(summary.run_id)}</h1>"
    html_text += f"<p>样本 {summary.sample_count}；成功 {summary.successful_samples}；失败 {summary.failed_samples}</p>"
    html_text += "<table><thead><tr>" + "".join(f"<th>{html.escape(name)}</th>" for name in headers) + "</tr></thead>"
    html_text += "<tbody>" + "".join(rows) + "</tbody></table></body></html>"
    _atomic_text(report_html, html_text)

    return ReportPaths(summary_json, results_jsonl, summary_csv, report_html)
