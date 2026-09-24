from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Measurement:
    name: str
    value: float | str | None = None
    unit: str | None = None
    pass_fail: str | None = None  # "pass" | "fail" | None


@dataclass
class TestCase:
    case_id: str
    name: str
    measurements: list[Measurement] = field(default_factory=list)
    notes: str = ""


@dataclass
class TestRun:
    run_id: str
    operator: str | None = None
    started_at: str | None = None
    cases: list[TestCase] = field(default_factory=list)


@dataclass
class TestReport:
    report_id: str
    kb_name: str
    title: str = ""
    source_file: str = ""
    runs: list[TestRun] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestReport":
        runs = []
        for run in data.get("runs", []):
            cases = []
            for case in run.get("cases", []):
                measurements = [Measurement(**m) for m in case.get("measurements", [])]
                case_payload = dict(case)
                case_payload["measurements"] = measurements
                cases.append(TestCase(**case_payload))
            run_payload = dict(run)
            run_payload["cases"] = cases
            runs.append(TestRun(**run_payload))
        return cls(
            report_id=data["report_id"],
            kb_name=data["kb_name"],
            title=data.get("title", ""),
            source_file=data.get("source_file", ""),
            runs=runs,
            metadata=dict(data.get("metadata", {})),
        )
