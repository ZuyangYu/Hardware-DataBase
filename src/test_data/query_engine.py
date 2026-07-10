from __future__ import annotations

import re

from src.test_data.store import TestDataStore


class TestDataQueryEngine:
    def __init__(self, store: TestDataStore | None = None):
        self.store = store or TestDataStore()

    def list_reports(self, kb_name: str) -> list[dict]:
        return [
            {
                "report_id": report.report_id,
                "title": report.title or report.source_file,
                "run_count": len(report.runs),
                "case_count": sum(len(run.cases) for run in report.runs),
            }
            for report in self.store.list_reports(kb_name)
        ]

    def search_measurements(self, kb_name: str, query: str = "", limit: int = 20) -> list[dict]:
        needles = [token.upper() for token in re.findall(r"[A-Za-z0-9_./%+-]{2,}", query)]
        results = []
        for report in self.store.list_reports(kb_name):
            for run in report.runs:
                for case in run.cases:
                    for measurement in case.measurements:
                        haystack = f"{case.name} {measurement.name} {measurement.value} {measurement.unit or ''}".upper()
                        if needles and not any(needle in haystack for needle in needles):
                            continue
                        results.append(
                            {
                                "report_id": report.report_id,
                                "case": case.name,
                                "measurement": measurement.name,
                                "value": measurement.value,
                                "unit": measurement.unit,
                                "pass_fail": measurement.pass_fail,
                            }
                        )
                        if len(results) >= limit:
                            return results
        return results
