"""Evaluate real multi-KB retrieval against source-bound textual anchors.

Run in an IsolatedDeployment environment, for example::

    HDB_ENV_PATH=/tmp/<run>/active.env uv run python -m \
      scripts.production_hardening.multi_kb_live_acceptance \
      --deployment /tmp/<run> --samples 4 --concurrency 1 2 4

selected-sources.json records only the chosen documents, stable KB IDs, source
hashes and upload responses. The evaluator calls production authorization,
retrieval, fusion and RAGFlow services. Capacity timings exclude model answers.
The anchor dataset is authored by the coding agent from local source excerpts;
it is not an independent human relevance annotation.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "evaluation/datasets/multi_kb_r3_live_anchors.jsonl"


def score_case(case, evidence, source_ids):
    """Require every anchor in its own expected KB and complete provenance."""
    matches, wanted, covered = [], 0, set()
    for expectation in case["expected"]:
        source = expectation["source"]
        own = [item for item in evidence if item["metadata"].get("kb_id") == source_ids[source]]
        for pattern in expectation["patterns"]:
            wanted += 1
            matching = [item["id"] for item in own if re.search(pattern, item["content"], re.I)]
            matches.append({"source": source, "pattern": pattern, "evidence_ids": matching})
        if own:
            covered.add(source)
    provenance_failures = [item["id"] for item in evidence if not all((
        item.get("id"), item.get("metadata", {}).get("kb_id"),
        item.get("metadata", {}).get("local_record_id"),
    ))]
    hit = sum(bool(match["evidence_ids"]) for match in matches)
    expected_sources = {item["source"] for item in case["expected"]}
    return {"passed": hit == wanted and not provenance_failures,
            "anchor_recall": hit / wanted if wanted else 1.0,
            "source_recall": len(covered & expected_sources) / len(expected_sources) if expected_sources else 1.0,
            "anchor_matches": matches, "provenance_failures": provenance_failures}


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    offset = (len(values) - 1) * fraction
    lower = math.floor(offset)
    upper = math.ceil(offset)
    return values[lower] + (values[upper] - values[lower]) * (offset - lower)


def summarize_capacity(results):
    times = [item["elapsed_seconds"] for item in results]
    return {"request_count": len(results),
            "failure_count": sum(item["partial"] or item["status"] != "ok" for item in results),
            "median_seconds": statistics.median(times) if times else None,
            "p95_seconds": percentile(times, .95), "max_seconds": max(times) if times else None,
            "calls_used": sum(item["calls_used"] for item in results)}


def prepare(deployment: Path):
    """Upload three selected originals plus one explicitly derived control."""
    import httpx
    from docx import Document

    metadata = json.loads((deployment / "deployment.json").read_text())
    client = httpx.Client(base_url=metadata["base_url"] + "/api/v1", timeout=180)
    login = client.post("/login", json={"username": metadata["username"], "password": metadata["test_password"]})
    login.raise_for_status()
    client.headers["Authorization"] = "Bearer " + login.json()["token"]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    selected = [
        ("eps60", "EPS_AP230060", "docs/test_chat/配置审计文档/600605919_EPS_HWREQM.docx"),
        ("eps62", "EPS_AP230062", "docs/test_chat/三环配置审计文档/600605918_EPS_HWSolution.docx"),
        ("adas", "ADAS_AA250004", "docs/test_chat/AA250004_ 奇瑞 T1G 高法规 ADAS项目/600608964_ADAS_HSI_0506_1952_shoulin.wang.docx"),
    ]
    control = deployment / "EPS_AP230060_control_candidate.docx"
    document = Document()
    document.add_heading("R3 验收派生控制样本 — EPS AP230060", 0)
    document.add_paragraph("本文件是自动生成的验收派生未审批候选，仅用于测试冲突，不是正式基线文档，不得作为工程发布依据。")
    document.add_paragraph("项目：AP230060；对象：EPS ECU；要求标识：REQ-KL30-OPERATION。")
    document.add_paragraph("KL30电压范围20-30V时，ECU所有模块正常工作。")
    document.add_paragraph("静态电流小于300 uA@25deg，测试电压24V。")
    document.add_paragraph("验收标记：R3-CONTROL-AP230060-20261016。")
    document.save(control)
    selected.append(("control", "EPS_AP230060", str(control)))
    manifest = []
    try:
        for source, project, relative in selected:
            path = Path(relative) if Path(relative).is_absolute() else REPO_ROOT / relative
            name = f"R3_{stamp}_{source.upper()}"
            response = client.post("/kbs", json={"name": name})
            response.raise_for_status()
            item = {"source": source, "project": project, "kb_name": name,
                    "relative_path": relative, "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "derived_control": source == "control", "source_group": "文档资料"}
            manifest.append(item)
            (deployment / "selected-sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            with path.open("rb") as stream:
                response = client.post("/kbs/" + name + "/files", data={"source_group": "文档资料"},
                                       files={"files": (path.name, stream)})
            response.raise_for_status()
            item["upload"] = response.json()
            item["kb_id"] = next(kb["kb_id"] for kb in client.get("/kbs").json() if kb["name"] == name)
            (deployment / "selected-sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        client.close()
    return manifest


def wait_parsed(deployment: Path, *, timeout_seconds=600):
    import httpx

    metadata = json.loads((deployment / "deployment.json").read_text())
    manifest = json.loads((deployment / "selected-sources.json").read_text())
    with httpx.Client(base_url=metadata["base_url"] + "/api/v1", timeout=60) as client:
        login = client.post("/login", json={"username": metadata["username"], "password": metadata["test_password"]})
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        deadline = time.monotonic() + timeout_seconds
        while True:
            pending = []
            for source in manifest:
                response = client.get("/kbs/" + source["kb_name"] + "/files")
                response.raise_for_status()
                source["files"] = response.json()
                original = next((f for f in source["files"] if f["name"] == Path(source["relative_path"]).name), None)
                if original is None or original["status"] not in {"parsed", "indexed"}:
                    pending.append(source["kb_name"])
            (deployment / "selected-sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            if not pending:
                return manifest
            if time.monotonic() >= deadline:
                raise TimeoutError("selected documents not parsed: " + ", ".join(pending))
            print("waiting_selected_sources=" + ",".join(pending), flush=True)
            time.sleep(5)


def evaluate(deployment: Path, *, samples: int, concurrencies: list[int],
             capacity_sources=(1, 2, 4), output_name="multi-kb-live-report.json"):
    from src.agents.tools.retrieval_executor import initialize_retrieval_executor, reset_retrieval_executor
    from src.core.auth import AuthService
    from src.pipelines.document_rag.factory import create_rag_backend
    from src.services.knowledge_scope import KnowledgeScopeDenied, KnowledgeScopeResolver
    from src.services.knowledge_scope_models import KnowledgeSelectionInput, KnowledgeSourceInput
    from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget
    from src.services.evidence_fusion import fuse_evidence
    import src.settings as settings

    manifest = wait_parsed(deployment)
    metadata = json.loads((deployment / "deployment.json").read_text())
    # Older exploratory manifests are accepted but reports always write keys.
    for item in manifest:
        item.setdefault("source", "control" if item.get("derived_control") else
                        "adas" if "ADAS" in item["kb_name"] else "eps60" if "EPS60" in item["kb_name"] else "eps62")
    identities = {item["source"]: item["kb_id"] for item in manifest}
    names = {item["source"]: item["kb_name"] for item in manifest}
    auth = AuthService()
    actor = auth.get_user_by_username(metadata["username"])
    resolver = KnowledgeScopeResolver(auth)
    backend = create_rag_backend()
    service = MultiKnowledgeRetrievalService(resolver, backend)
    initialize_retrieval_executor()

    def retrieve(sources, queries, *, user=actor):
        start = time.monotonic()
        budget = RetrievalBudget(max_calls=settings.MULTI_KB_MAX_CALLS_PER_TURN,
                                 timeout_seconds=settings.MULTI_KB_RETRIEVAL_DEADLINE_SECONDS)
        purpose = "answer" if len(sources) == 1 else "compare"
        selection = KnowledgeSelectionInput(purpose=purpose, sources=tuple(
            KnowledgeSourceInput(kb_id=identities[key], role="primary" if len(sources) == 1 else "peer") for key in sources))
        snapshot = resolver.resolve(user, names[sources[0]], selection)
        outcome = service.search(snapshot=snapshot, queries=queries, budget=budget,
                                 per_source_top_k=settings.MULTI_KB_PER_SOURCE_TOP_K)
        fusion = fuse_evidence(outcome, purpose=purpose,
                               max_items=settings.MULTI_KB_EVIDENCE_MAX_ITEMS,
                               token_budget=settings.MULTI_KB_CONTEXT_MAX_TOKENS)
        return {"elapsed_seconds": time.monotonic() - start, "calls_used": budget.calls_used,
                "status": "ok" if not outcome.partial else "partial_failure", "partial": outcome.partial,
                "selection_hash": snapshot.selection_hash, "snapshot": snapshot.model_dump(mode="json"),
                "coverage": outcome.coverage,
                "fusion_coverage": fusion.coverage, "fusion_complete": fusion.complete,
                "fusion_token_estimate": fusion.token_count, "fusion_discarded": fusion.discarded,
                "query_coverage": fusion.query_coverage,
                "source_representatives": fusion.source_representatives,
                "raw_evidence_count": len(outcome.evidence),
                "source_outcomes": [{"kb_id": item.kb_id, "status": item.status, "error_code": item.error_code,
                                     "evidence_count": len(item.evidence), "diagnostics": item.diagnostics} for item in outcome.source_outcomes],
                "evidence": [{"id": item.id, "content": item.content, "source_name": item.source_name,
                              "metadata": item.metadata, "score": item.score} for item in fusion.evidence]}

    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "model_used_for_scoring": False,
              "annotation_author": "coding_agent_source_excerpt_review; not independent human labels",
              "scope": "real scoped RAGFlow retrieval; excludes answer-model latency",
              "source_manifest": [{key: item.get(key) for key in ("source", "project", "kb_name", "kb_id", "sha256", "bytes", "derived_control")} for item in manifest],
              "cases": [], "capacity": [], "boundaries": {}}
    output = deployment / output_name
    try:
        for line in DATASET.read_text().splitlines():
            case = json.loads(line)
            result = retrieve(case["sources"], case["queries"])
            result["case_id"] = case["id"]
            result["question"] = case["question"]
            result["score"] = score_case(case, result["evidence"], identities)
            report["cases"].append(result)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(f"case={case['id']} passed={result['score']['passed']} anchors={result['score']['anchor_recall']:.3f}", flush=True)
        for count in capacity_sources:
            sources = ["eps60", "eps62", "adas", "control"][:count]
            for concurrency in concurrencies:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    results = list(pool.map(lambda _: retrieve(sources, ["KL30 电源 电压 范围 静态电流 LP87702"]), range(max(samples, concurrency))))
                summary = summarize_capacity(results)
                summary.update({"source_count": count, "concurrency": concurrency, "samples": results})
                report["capacity"].append(summary)
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(f"capacity sources={count} concurrency={concurrency} p95={summary['p95_seconds']:.3f} failures={summary['failure_count']}", flush=True)
        # Exact owner-managed sharing is reversible and scoped to this run.
        shared_name = names["control"]
        auth.set_knowledge_base_sharing(shared_name, actor, True, department_id=actor.department_id, kb_id=identities["control"])
        browser = auth.get_user_by_username(metadata["browser_username"])
        accessible = retrieve(["control"], ["R3-CONTROL-AP230060-20261016"], user=browser)
        snapshot_payload = accessible["snapshot"]
        auth.set_knowledge_base_sharing(shared_name, actor, False, department_id=actor.department_id, kb_id=identities["control"])
        from src.services.knowledge_scope_models import KnowledgeScopeSnapshot
        try:
            resolver.revalidate(browser.id, KnowledgeScopeSnapshot.model_validate(snapshot_payload))
        except KnowledgeScopeDenied:
            report["boundaries"]["revoked_snapshot_denied"] = True
        else:
            report["boundaries"]["revoked_snapshot_denied"] = False
        report["boundaries"]["shared_source_read_evidence_count"] = len(accessible["evidence"])
        previous = settings.MULTI_KB_CHAT_ENABLED
        settings.MULTI_KB_CHAT_ENABLED = False
        try:
            from src.agents.tools.retrieval_executor import get_multi_kb_retrieval_executor, MultiKnowledgeRetrievalUnavailable
            try:
                get_multi_kb_retrieval_executor()
            except MultiKnowledgeRetrievalUnavailable:
                report["boundaries"]["flag_off_fail_closed"] = True
            else:
                report["boundaries"]["flag_off_fail_closed"] = False
        finally:
            settings.MULTI_KB_CHAT_ENABLED = previous
        report["summary"] = {"case_count": len(report["cases"]),
                             "passed_cases": sum(item["score"]["passed"] for item in report["cases"]),
                             "boundary_passed": all(value for key, value in report["boundaries"].items() if key.endswith("denied") or key.endswith("closed"))}
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        reset_retrieval_executor(deadline_seconds=5)
        close = getattr(backend, "close", None)
        if callable(close):
            close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True, type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--samples", type=int, default=4)
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4])
    parser.add_argument("--capacity-sources", nargs="*", type=int, choices=[1, 2, 4], default=[1, 2, 4])
    parser.add_argument("--output-name", default="multi-kb-live-report.json")
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 100 or any(not 1 <= value <= 16 for value in args.concurrency):
        parser.error("samples must be 1..100 and concurrency 1..16")
    if Path(args.output_name).name != args.output_name:
        parser.error("output-name must be a filename inside this deployment")
    config = args.deployment / "active.env"
    if not config.is_file():
        parser.error("isolated deployment active.env is missing")
    if os.environ.get("HDB_ENV_PATH") != str(config):
        parser.error("set HDB_ENV_PATH to this isolated deployment's active.env")
    if args.prepare:
        if (args.deployment / "selected-sources.json").exists():
            parser.error("refusing to replace an existing source manifest")
        prepare(args.deployment)
    report = evaluate(args.deployment, samples=args.samples, concurrencies=args.concurrency,
                      capacity_sources=args.capacity_sources, output_name=args.output_name)
    print(json.dumps(report["summary"]))
    return 0 if (
        report["summary"]["passed_cases"] == report["summary"]["case_count"]
        and report["summary"]["boundary_passed"]
        and all(item["failure_count"] == 0 for item in report["capacity"])
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
