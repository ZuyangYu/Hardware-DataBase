#!/usr/bin/env python3
"""Single-variable deterministic screening of retrieval limits.

This does not replace the two real-model gates. It records whether the six
source anchors still pass when one limit is lowered/raised while the others
stay at the accepted baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import src.settings as settings  # noqa: E402
from multi_kb_retrieval_regression import _execute_deterministic  # noqa: E402


EXPERIMENTS = (
    ("calls_12", {"MULTI_KB_MAX_CALLS_PER_TURN": 12}),
    ("calls_48", {"MULTI_KB_MAX_CALLS_PER_TURN": 48}),
    ("calls_96", {"MULTI_KB_MAX_CALLS_PER_TURN": 96}),
    ("deadline_15", {"MULTI_KB_RETRIEVAL_DEADLINE_SECONDS": 15}),
    ("deadline_60", {"MULTI_KB_RETRIEVAL_DEADLINE_SECONDS": 60}),
    ("deadline_90", {"MULTI_KB_RETRIEVAL_DEADLINE_SECONDS": 90}),
    ("bytes_4000", {"MULTI_KB_FUSION_MAX_BYTES": 4000}),
    ("bytes_16000", {"MULTI_KB_FUSION_MAX_BYTES": 16000}),
    ("bytes_24000", {"MULTI_KB_FUSION_MAX_BYTES": 24000}),
)

# Four kilobytes is intentionally retained as a negative control: it should
# expose the known three-project supply failure and therefore is expected to
# return fewer than six passing anchors.  All other sweep points are expected
# to pass the deterministic anchor set.
EXPECTED_RESULTS = {"bytes_4000": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--golden", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    manifest = json.loads(Path(args.manifest).read_text())
    golden = json.loads(Path(args.golden).read_text())
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = {
        "MULTI_KB_MAX_CALLS_PER_TURN": int(settings.MULTI_KB_MAX_CALLS_PER_TURN),
        "MULTI_KB_RETRIEVAL_DEADLINE_SECONDS": float(settings.MULTI_KB_RETRIEVAL_DEADLINE_SECONDS),
        "MULTI_KB_FUSION_MAX_BYTES": int(settings.MULTI_KB_FUSION_MAX_BYTES),
    }
    report = {"baseline": baseline, "scope": "deterministic source anchors only", "experiments": []}
    try:
        for name, overrides in EXPERIMENTS:
            for key, value in {**baseline, **overrides}.items():
                setattr(settings, key, value)
            with tempfile.TemporaryDirectory(prefix=f"hdb-sweep-{name}-") as temp_dir:
                result = _execute_deterministic(manifest, golden, Path(temp_dir))
            expected_pass = EXPECTED_RESULTS.get(name, True)
            outcome_met = result["passed"] is expected_pass
            report["experiments"].append({
                "name": name, "overrides": overrides,
                "passed_count": result["passed_count"], "cases": len(result["cases"]),
                "passed": result["passed"],
                "expected_pass": expected_pass,
                "expected_outcome_met": outcome_met,
                "cases_detail": [
                    {"id": case["id"], "passed": case["passed"], "calls_used": case["calls_used"]}
                    for case in result["cases"]
                ],
            })
            print(
                "SWEEP", name, result["passed_count"], "/", len(result["cases"]),
                "expected_pass=" + str(expected_pass),
                "outcome_met=" + str(outcome_met),
                flush=True,
            )
    finally:
        for key, value in baseline.items():
            setattr(settings, key, value)
    report["passed"] = all(item["expected_outcome_met"] for item in report["experiments"])
    path = output_dir / "parameter-sweep.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(path)
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
