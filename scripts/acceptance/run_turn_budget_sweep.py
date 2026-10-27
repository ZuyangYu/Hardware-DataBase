#!/usr/bin/env python3
"""Replay admitted evidence through runtime turn-byte limits.

This isolates the whole-turn evidence budget from the per-call fusion budget.
It uses the already accepted deterministic evidence and server-resolved targets;
it does not call the model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import src.settings as settings  # noqa: E402
from src.agents.schemas import Evidence  # noqa: E402
from src.agents.tools.runtime import ToolRuntime, _evidence_context_cost  # noqa: E402
from src.core.auth import AuthService  # noqa: E402
from src.pipelines.document_rag.factory import create_rag_backend  # noqa: E402
from src.services.knowledge_scope import KnowledgeScopeResolver  # noqa: E402
from src.services.knowledge_scope_models import KnowledgeSelectionInput  # noqa: E402
from src.services.retrieval_targets import resolve_turn_targets  # noqa: E402
from src.services.turn_evidence_plan import TurnEvidencePlan  # noqa: E402

BUDGETS = (4000, 8000, 16000, 24000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--golden", required=True)
    parser.add_argument("--evidence-report", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    manifest = json.loads(Path(args.manifest).read_text())
    report = json.loads(Path(args.evidence_report).read_text())
    golden = {case["id"]: case for case in json.loads(Path(args.golden).read_text())}
    projects = {project["source"]: project for project in manifest["projects"]}
    auth = AuthService()
    actor = auth.get_user_by_username(manifest["username"])
    resolver = KnowledgeScopeResolver(auth)
    backend = create_rag_backend()
    output = {"budgets": list(BUDGETS), "cases": []}
    for case in report["cases"]:
        sources = [projects[key] for key in case["sources"]]
        selection = KnowledgeSelectionInput.model_validate({
            "version": 1,
            "purpose": "answer" if len(sources) == 1 else "compare",
            "sources": [{"kb_id": project["kb_id"], "role": "primary" if len(sources) == 1 else "peer"}
                        for project in sources],
        })
        snapshot = resolver.resolve(actor, sources[0]["kb_name"], selection)
        question = golden.get(case["id"], {}).get("question", "")
        targets = resolve_turn_targets(
            question, sources=snapshot.sources, document_store=backend.store,
            turn_id="turn-budget-sweep", scope_fingerprint=snapshot.selection_hash,
        )
        row = {"id": case["id"], "requirements": len(targets.targets), "budgets": {}}
        evidence_items = [
            Evidence(id=item["id"], content=item["content"], source_name=item["source_name"],
                     content_kind=str((item.get("metadata") or {}).get("content_kind") or "document_text"),
                     processor_kind=str((item.get("metadata") or {}).get("processor_kind") or "ragflow"),
                     score=float(item.get("score") or 0.0),
                     locator=dict((item.get("metadata") or {}).get("locator") or {}),
                     metadata=dict(item.get("metadata") or {}))
            for item in case.get("evidence", [])
        ]
        for budget in BUDGETS:
            old = settings.MULTI_KB_TURN_EVIDENCE_MAX_BYTES
            settings.MULTI_KB_TURN_EVIDENCE_MAX_BYTES = budget
            try:
                plan = TurnEvidencePlan.from_targets(
                    targets.targets, turn_id="turn-budget-sweep",
                    selected_kb_ids=[source.kb_id for source in snapshot.sources],
                )
                rt = ToolRuntime(
                    kb_name=sources[0]["kb_name"], ctx=resolver.context_for_source(actor.id, snapshot.sources[0]),
                    knowledge_scope=snapshot, question=question, evidence_budget=24,
                    turn_evidence_plan=plan,
                )
                rt.add_evidence(evidence_items)
                states = {state.state for state in plan.states.values()}
                row["budgets"][str(budget)] = {
                    "admitted": sum(state.state == "admitted" for state in plan.states.values()),
                    "required": len(plan.states),
                    "complete": bool(plan.states) and states == {"admitted"},
                    "turn_context_used": sum(_evidence_context_cost(item) for item in rt.evidence),
                }
            finally:
                settings.MULTI_KB_TURN_EVIDENCE_MAX_BYTES = old
        output["cases"].append(row)
    output["passed"] = all(
        row["budgets"]["8000"]["complete"] for row in output["cases"]
    )
    path = Path(args.output_dir).resolve() / "turn-budget-sweep.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(path)
    return 0 if output["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
