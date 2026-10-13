"""Code-calculated paired differences; unsupported pairs remain missing."""
from __future__ import annotations

import math

from src.costs.store import CostStore


def paired_statistics(baseline, candidate, viewer, *, costs=None):
    def valid(value):
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)

    left, right = ({r.sample_id: r for r in history.results} for history in (baseline, candidate))
    pairs, metrics, latency = [], {}, []
    for sample_id in sorted(set(left) & set(right)):
        old, new = left[sample_id], right[sample_id]
        before = {m.metric_name: m.score for m in old.metrics if m.status == "success" and valid(m.score)}
        after = {m.metric_name: m.score for m in new.metrics if m.status == "success" and valid(m.score)}
        deltas = {name: after[name] - before[name] for name in before.keys() & after.keys()}
        for name, delta in deltas.items():
            metrics.setdefault(name, []).append(delta)
        old_latency, new_latency = old.metadata.get("latency_ms"), new.metadata.get("latency_ms")
        latency_delta = new_latency - old_latency if valid(old_latency) and valid(new_latency) else None
        if latency_delta is not None:
            latency.append(latency_delta)
        pairs.append({"sample_id": sample_id, "critical": old.critical, "metric_deltas": deltas,
                      "latency_delta_ms": latency_delta})
    costs = costs or CostStore()
    return {"schema_version": 1, "cohort_size": len(left), "paired_samples": pairs,
            "metric_delta_means": {name: {"mean": sum(values)/len(values), "pairs": len(values), "coverage": len(values)/len(left)} for name, values in metrics.items()},
            "latency_delta_ms": {"mean": sum(latency)/len(latency) if latency else None, "pairs": len(latency)},
            "baseline_run_costs": costs.summary(viewer, task_type="evaluation", task_id=baseline.run_id),
            "candidate_run_costs": costs.summary(viewer, task_type="evaluation", task_id=candidate.run_id),
            "cost_scope": "evaluation_calls_including_judge;original_production_answer_cost_excluded",
            "significance": "descriptive_only", "validation": "fixed_inputs_and_candidate_gate_required"}
