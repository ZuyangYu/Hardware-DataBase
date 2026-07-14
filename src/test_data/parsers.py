"""Best-effort CSV/Excel/JSON test-report parser.

This is a deliberately minimal placeholder so the parser registry can dispatch
TEST_GROUP uploads end-to-end without raising. The test-data sub-team is
expected to replace this with a full parser that understands their lab tools.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Callable

from llama_index.core.schema import TextNode

from src.test_data.models import Measurement, TestCase, TestReport, TestRun
from src.test_data.store import TestDataStore, make_report_id


def _read_csv_rows(file_path: str) -> list[dict]:
    import csv

    with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _read_json(file_path: str) -> list[dict]:
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return [row for row in data["rows"] if isinstance(row, dict)]
    return []


def _rows_to_report(rows: list[dict], filename: str, kb_name: str) -> TestReport:
    cases: dict[str, TestCase] = {}
    for row in rows:
        case_name = str(row.get("case") or row.get("test_case") or "default")
        case = cases.setdefault(
            case_name,
            TestCase(case_id=f"case_{len(cases) + 1:03d}", name=case_name),
        )
        measurement_name = str(row.get("measurement") or row.get("metric") or "value")
        case.measurements.append(
            Measurement(
                name=measurement_name,
                value=row.get("value"),
                unit=row.get("unit"),
                pass_fail=row.get("pass_fail") or row.get("status"),
            )
        )
    run = TestRun(run_id=f"run_{uuid.uuid4().hex[:8]}", cases=list(cases.values()))
    return TestReport(
        report_id=make_report_id(filename),
        kb_name=kb_name,
        title=Path(filename).stem,
        source_file=filename,
        runs=[run] if run.cases else [],
    )


def parse_test_data(
    file_path: str,
    filename: str,
    kb_name: str,
    progress_callback: Callable[[int, str], None] | None = None,
) -> list[TextNode]:
    if progress_callback:
        progress_callback(42, "Reading test data")
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".csv":
        rows = _read_csv_rows(file_path)
    elif ext == ".json":
        rows = _read_json(file_path)
    else:
        rows = []

    report = _rows_to_report(rows, filename, kb_name)
    store = TestDataStore()
    store.save(report)

    if progress_callback:
        progress_callback(65, f"Test data parsed: {sum(len(r.cases) for r in report.runs)} cases")

    summary_lines = [f"Test report: {report.title}", f"Source: {filename}"]
    for run in report.runs:
        summary_lines.append(f"Run {run.run_id}: {len(run.cases)} cases")
        for case in run.cases[:20]:
            summary_lines.append(
                f"- {case.name}: {len(case.measurements)} measurements"
            )
    return [
        TextNode(
            text="\n".join(summary_lines),
            metadata={
                "file_name": filename,
                "source_type": "test_report",
                "report_id": report.report_id,
                "case_count": sum(len(r.cases) for r in report.runs),
            },
        )
    ]
