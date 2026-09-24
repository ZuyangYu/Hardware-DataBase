"""P3-001 offline evaluation for the deterministic Reviewer layer.

Runs the flag-gated ReviewerAgent in deterministic mode over a labeled fixture
(``evaluation/fixtures/reviewer_cases.jsonl``) and reports per-code precision/
recall plus status accuracy.  The model-based semantic pass (unsupported
claims / conflicts) is only exercised with ``--model``; without a model budget
the report explicitly marks that dimension as not evaluated.

Usage:
    uv run python scripts/production_hardening/reviewer_eval.py \
        --fixture evaluation/fixtures/reviewer_cases.jsonl \
        --output docs/superpowers/implementation/production-hardening/reviewer-eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.agents.subagents.reviewer_agent import (  # noqa: E402
    REVIEWER_VERSION,
    ReviewerAgent,
)

DEFAULT_FIXTURE = REPO_ROOT / "evaluation" / "fixtures" / "reviewer_cases.jsonl"

# Semantic-pass codes are reported separately; the deterministic fixture scores
# only the deterministic layer so a model smoke run never changes its gate.
SEMANTIC_ISSUE_CODES = frozenset({"unsupported_claim", "evidence_conflict"})


def _deterministic_view(result: dict) -> tuple[set[str], str]:
    issues = [
        issue
        for issue in result.get("issues", [])
        if str(issue.get("code")) not in SEMANTIC_ISSUE_CODES
    ]
    codes = {str(issue.get("code")) for issue in issues}
    if any(issue.get("severity") == "fail" for issue in issues):
        status = "fail"
    elif issues:
        status = "warn"
    else:
        status = "pass"
    return codes, status


def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def review_case(case: dict, *, model=None) -> dict:
    reviewer = ReviewerAgent(model, enabled=True)
    result = reviewer.review(
        document_text=str(case.get("document_text") or ""),
        evidence=list(case.get("evidence") or []),
        required_facts=list(case.get("required_facts") or []),
        forbidden_claims=list(case.get("forbidden_claims") or []),
    )
    return result.to_dict()


def _load_semantic_model():
    """Build the configured governed chat model for the semantic smoke pass."""
    from src.core.model_factory import create_chat_model

    return create_chat_model()


def score_cases(cases: list[dict], results: dict[str, dict]) -> dict:
    """Deterministic per-code TP/FP/FN and status accuracy."""
    codes = sorted(
        {code for case in cases for code in case.get("expected_codes", [])}
        | {
            code
            for result in results.values()
            for code in _deterministic_view(result)[0]
        }
    )
    per_code = {
        code: {"tp": 0, "fp": 0, "fn": 0, "precision": 0.0, "recall": 0.0}
        for code in codes
    }
    status_hits = 0
    for case in cases:
        case_id = str(case["case_id"])
        expected = set(case.get("expected_codes", []))
        actual, actual_status = _deterministic_view(results[case_id])
        for code in sorted(expected | (actual - SEMANTIC_ISSUE_CODES)):
            if code in expected and code in actual:
                per_code[code]["tp"] += 1
            elif code in actual:
                per_code[code]["fp"] += 1
            else:
                per_code[code]["fn"] += 1
        if actual_status == str(case.get("expected_status", "pass")):
            status_hits += 1
    for metrics in per_code.values():
        tp, fp, fn = metrics["tp"], metrics["fp"], metrics["fn"]
        metrics["precision"] = round(tp / (tp + fp), 4) if (tp + fp) else 1.0
        metrics["recall"] = round(tp / (tp + fn), 4) if (tp + fn) else 1.0
    return {
        "cases": len(cases),
        "status_accuracy": round(status_hits / len(cases), 4) if cases else 0.0,
        "per_code": per_code,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--model",
        action="store_true",
        help="also run the semantic pass (requires model budget; not scored here)",
    )
    args = parser.parse_args(argv)

    fixture = Path(args.fixture)
    if not fixture.is_file():
        print(f"fixture not found: {fixture}", file=sys.stderr)
        return 2
    cases = load_cases(fixture)
    if not cases:
        print("fixture is empty", file=sys.stderr)
        return 2

    semantic_model = None
    semantic_error = ""
    if args.model:
        try:
            semantic_model = _load_semantic_model()
        except Exception as exc:  # pragma: no cover - depends on deployment config
            semantic_error = f"{type(exc).__name__}: {exc}"

    results: dict[str, dict] = {}
    for case in cases:
        results[str(case["case_id"])] = review_case(case, model=semantic_model)

    if args.model and semantic_model is not None:
        semantic_pass = {
            "evaluated": True,
            "kind": "smoke",
            "note": (
                "semantic pass smoke only: adversarial precision/recall requires a "
                "labeled semantic corpus and a frozen model budget"
            ),
            "cases_with_unsupported_claims": sum(
                1 for result in results.values() if result.get("unsupported_claims")
            ),
            "cases_with_conflicts": sum(
                1 for result in results.values() if result.get("evidence_conflicts")
            ),
        }
    else:
        semantic_pass = {
            "evaluated": False,
            "reason": semantic_error or "no model budget in this environment",
        }

    report = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "fixture": str(fixture),
        "reviewer_version": REVIEWER_VERSION,
        "deterministic": score_cases(cases, results),
        "semantic_pass": semantic_pass,
        "results": results,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    ok = report["deterministic"]["status_accuracy"] == 1.0 and all(
        metrics["precision"] == 1.0 and metrics["recall"] == 1.0
        for metrics in report["deterministic"]["per_code"].values()
    )
    print(
        f"status_accuracy={report['deterministic']['status_accuracy']} "
        f"codes={len(report['deterministic']['per_code'])} ok={ok}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
