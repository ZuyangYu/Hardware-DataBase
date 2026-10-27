#!/usr/bin/env python3
"""Preflight and report scaffold for the multi-KB real-project regression.

The live six-question/model gate is intentionally not faked here. This entry
verifies the original manifest, source hashes, code/dirty fingerprints and
service reachability, then records ``not_executed`` when a dependency is
missing. It never invents answers or copies an older report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _reachable(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # Any HTTP answer proves the endpoint is listening; auth/route errors
        # remain visible in the report.
        return True, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - dependency state is data
        return False, f"{type(exc).__name__}: {exc}"


def _resolve_source(source_path: str) -> Path:
    path = Path(source_path)
    return path if path.is_absolute() else REPO_ROOT / path


def verify_manifest(manifest: dict) -> list[dict]:
    results = []
    for project in manifest.get("projects", []):
        for item in project.get("files", []):
            path = _resolve_source(str(item.get("source_path") or ""))
            row = {
                "project": project.get("project"),
                "filename": item.get("filename"),
                "source_path": str(path),
                "exists": path.is_file(),
                "expected_sha256": item.get("sha256"),
            }
            if path.is_file():
                row["actual_sha256"] = _sha256(path)
                row["sha256_match"] = row["actual_sha256"] == row["expected_sha256"]
            else:
                row["actual_sha256"] = None
                row["sha256_match"] = False
            results.append(row)
    return results


def _execute_deterministic(manifest: dict, golden: list[dict], output_dir: Path) -> dict:
    """Run the original six retrieval anchors through production adapters."""
    import re
    import time

    import src.settings as settings
    from src.agents.tools.retrieval_executor import initialize_retrieval_executor
    from src.core.auth import AuthService
    from src.pipelines.document_rag.factory import create_rag_backend
    from src.services.evidence_fusion import fuse_evidence
    from src.services.knowledge_scope import KnowledgeScopeResolver
    from src.services.knowledge_scope_models import KnowledgeSelectionInput
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget
    from src.services.retrieval_targets import compile_work_items, resolve_turn_targets
    from src.services.spreadsheet_index_service import SpreadsheetIndexService
    from src.services.structured_retrieval import build_structured_retrieval_adapters

    projects = {project["source"]: project for project in manifest.get("projects", [])}
    auth = AuthService()
    actor = auth.get_user_by_username(manifest.get("username") or "test_dept")
    if actor is None:
        raise RuntimeError("acceptance_user_missing")
    resolver = KnowledgeScopeResolver(auth)
    backend = create_rag_backend()
    initialize_retrieval_executor()
    adapters = build_structured_retrieval_adapters(
        document_store=backend.store, spreadsheet_service=SpreadsheetIndexService(),
    )
    service = MultiKnowledgeRetrievalService(resolver, backend, adapters=adapters)
    report = {"scope": "deterministic production adapters; no model", "cases": []}
    for case in golden:
        sources = [projects[key] for key in case["sources"]]
        selection = KnowledgeSelectionInput.model_validate({
            "version": 1,
            "purpose": "answer" if len(sources) == 1 else "compare",
            "sources": [
                {"kb_id": project["kb_id"], "role": "primary" if len(sources) == 1 else "peer"}
                for project in sources
            ],
        })
        snapshot = resolver.resolve(actor, sources[0]["kb_name"], selection)
        started = time.monotonic()
        targets = resolve_turn_targets(
            case["question"], sources=snapshot.sources, document_store=backend.store,
            turn_id="t8-deterministic", scope_fingerprint=snapshot.selection_hash,
        )
        work_items = compile_work_items(targets.targets) if targets.resolution == "resolved" else ()
        budget = RetrievalBudget(
            max_calls=int(settings.MULTI_KB_MAX_CALLS_PER_TURN),
            timeout_seconds=float(settings.MULTI_KB_RETRIEVAL_DEADLINE_SECONDS),
        )
        kind = case.get("retriever_kind", "document")
        if work_items:
            from dataclasses import replace

            expanded = []
            for item in work_items:
                seen_queries = set()
                for query in case.get("queries") or [item.normalized_query]:
                    value = str(query or "").strip()
                    if not value or value in seen_queries:
                        continue
                    seen_queries.add(value)
                    expanded.append(replace(item, normalized_query=value))
            outcome = service.search(
                snapshot=snapshot, queries=[], budget=budget, per_source_top_k=8,
                work_items=expanded,
            )
        else:
            outcome = service.search(
                snapshot=snapshot, queries=case["queries"], retriever_kind=kind,
                budget=budget, per_source_top_k=8,
            )
        fusion_budget = int(getattr(settings, "MULTI_KB_FUSION_MAX_BYTES",
                                    settings.MULTI_KB_CONTEXT_MAX_TOKENS))
        fusion = fuse_evidence(
            outcome, purpose=snapshot.purpose,
            max_items=settings.MULTI_KB_EVIDENCE_MAX_ITEMS, token_budget=fusion_budget,
        )
        evidence = [
            {"id": item.id, "source_name": item.source_name, "content": item.content,
             "metadata": dict(item.metadata), "score": item.score}
            for item in fusion.evidence
        ]
        checks = []
        for expected in case["expected"]:
            project = projects[expected["source"]]
            own = [item for item in evidence
                   if int(item["metadata"].get("kb_id") or 0) == project["kb_id"]
                   and item["source_name"] == expected["filename"]]
            locator = expected.get("locator") or {}
            if locator.get("sheet"):
                own = [item for item in own
                       if item["metadata"].get("locator", {}).get("sheet_name") == locator["sheet"]
                       and item["metadata"].get("locator", {}).get("row_index") == locator.get("row")]
            for pattern in expected["patterns"]:
                checks.append({
                    "source": expected["source"], "filename": expected["filename"],
                    "pattern": pattern,
                    "evidence_ids": [item["id"] for item in own if re.search(pattern, item["content"], re.I)],
                })
        allowed = {project["kb_id"] for project in sources}
        actual = {int(item["metadata"].get("kb_id") or 0) for item in evidence}
        provenance_errors = []
        for item in evidence:
            metadata = item["metadata"]
            kb_id = int(metadata.get("kb_id") or 0)
            project = next((value for value in projects.values() if value["kb_id"] == kb_id), None)
            if project is None or not all(metadata.get(key) for key in
                                          ("local_record_id", "source_content_hash", "evidence_text_hash")):
                provenance_errors.append({"evidence_id": item["id"], "reason": "incomplete_or_out_of_scope"})
                continue
            record = backend.store.get_document_by_id_scoped(
                int(metadata["local_record_id"]), str(manifest.get("department_id") or "47"),
            )
            if record is None or record.kb_name != project["kb_name"] or record.content_hash != metadata["source_content_hash"]:
                provenance_errors.append({"evidence_id": item["id"], "reason": "source_stamp_mismatch"})
        report["cases"].append({
            "id": case["id"], "sources": case["sources"], "retriever_kind": kind,
            "target_resolution": targets.resolution,
            "work_items": [
                {"work_id": item.work_id, "kb_id": item.kb_id, "retriever_kind": item.retriever_kind,
                 "target_ids": list(item.target_ids), "allowed_record_ids": list(item.contract.allowed_record_ids or ())}
                for item in work_items
            ],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "calls_used": budget.calls_used,
            "partial": outcome.partial,
            "fusion_complete": fusion.complete,
            "evidence": evidence, "checks": checks,
            "provenance_errors": provenance_errors,
            "source_outcomes": [
                {"kb_id": row.kb_id, "query": row.query, "retriever_kind": row.retriever_kind,
                 "status": row.status, "error_code": row.error_code, "evidence_count": len(row.evidence),
                 "work_id": row.work_id, "target_ids": list(row.target_ids),
                 "diagnostics": dict(row.diagnostics)}
                for row in outcome.source_outcomes
            ],
            "passed": bool(checks) and all(row["evidence_ids"] for row in checks)
                      and actual == allowed and not provenance_errors and not outcome.partial,
        })
    report["passed_count"] = sum(1 for case in report["cases"] if case["passed"])
    report["passed"] = bool(report["cases"]) and report["passed_count"] == len(report["cases"])
    report_path = output_dir / "deterministic-retrieval-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    os.chmod(report_path, 0o600)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--golden", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--backend-url", default="")
    parser.add_argument("--auth-file", default="")
    parser.add_argument("--run-spec", default="")
    parser.add_argument("--execute-deterministic", action="store_true")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text())
    sources = verify_manifest(manifest)
    golden = []
    if args.golden:
        golden_path = Path(args.golden).resolve()
        if golden_path.is_file():
            golden = json.loads(golden_path.read_text())

    preflight = {
        "manifest_readable": manifest_path.is_file(),
        "all_sources_match": bool(sources) and all(item["sha256_match"] for item in sources),
        "golden_loaded": bool(golden),
        "base_url": args.base_url or None,
        "backend_url": args.backend_url or None,
        "auth_file_present": bool(args.auth_file and Path(args.auth_file).is_file()),
    }
    if args.base_url:
        preflight["frontend_reachable"], preflight["frontend_detail"] = _reachable(args.base_url)
    if args.backend_url:
        preflight["backend_reachable"], preflight["backend_detail"] = _reachable(args.backend_url)

    missing = [key for key, value in preflight.items() if key.endswith("_reachable") and not value]
    not_executed = (
        not preflight["all_sources_match"]
        or not preflight["golden_loaded"]
        or not preflight.get("frontend_reachable", False)
        or not preflight.get("backend_reachable", False)
        or not preflight["auth_file_present"]
    )
    report = {
        "batch_id": dt.datetime.now(dt.timezone.utc).strftime("multi-kb-%Y%m%dT%H%M%SZ"),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "not_executed" if not_executed else "preflight_only",
        "not_executed_reason": (
            "Missing dependencies: " + ", ".join(missing) if missing
            else "Live six-question/model gate is not implemented in this preflight entry."
        ) if not_executed else None,
        "code": {
            "head": _git("rev-parse", "HEAD"),
            "branch": _git("branch", "--show-current"),
            "dirty_diff_sha256": hashlib.sha256(
                subprocess.check_output(["git", "diff", "--binary"], cwd=REPO_ROOT)
            ).hexdigest() if _git("rev-parse", "--is-inside-work-tree") else None,
        },
        "model_config": {
            "provider": os.getenv("AGENT_LLM_PROVIDER", ""),
            "model": os.getenv("AGENT_CUSTOM_MODEL") or os.getenv("AGENT_OLLAMA_MODEL", ""),
            "max_calls": os.getenv("MULTI_KB_MAX_CALLS_PER_TURN", ""),
            "deadline_seconds": os.getenv("MULTI_KB_RETRIEVAL_DEADLINE_SECONDS", ""),
            "fusion_max_bytes": os.getenv("MULTI_KB_FUSION_MAX_BYTES", ""),
            "turn_evidence_max_bytes": os.getenv("MULTI_KB_TURN_EVIDENCE_MAX_BYTES", ""),
        },
        "run_spec": args.run_spec or None,
        "preflight": preflight,
        "sources": sources,
        "golden_case_ids": [case.get("id") for case in golden],
        "attempts": [],
        "note": "Credentials/tokens are never written. Existing acceptance reports are not overwritten.",
    }
    report_path = output_dir / "regression-preflight.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    os.chmod(report_path, 0o600)
    print(report_path)
    if args.execute_deterministic and preflight["all_sources_match"] and preflight["golden_loaded"]:
        deterministic = _execute_deterministic(manifest, golden, output_dir)
        print(json.dumps({
            "status": "passed" if deterministic["passed"] else "failed",
            "passed_count": deterministic["passed_count"],
            "cases": len(deterministic["cases"]),
        }, ensure_ascii=False))
        if not deterministic["passed"]:
            return 3
    return 0 if not not_executed else 2


if __name__ == "__main__":
    sys.exit(main())
