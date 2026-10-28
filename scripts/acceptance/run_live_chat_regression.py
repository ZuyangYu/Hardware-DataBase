#!/usr/bin/env python3
"""Run the original six real-project questions through the live chat API.

The caller supplies a short-lived auth file created for test_dept. The script
never prints the token, never changes the account password, and writes each
pre-registered round to its own report so a failed first round cannot be
overwritten by a later retry.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path

import httpx


def score_case(case: dict, projects: dict, turn: dict) -> dict:
    summary = turn.get("summary") or {}
    evidence = summary.get("evidence") or []
    allowed = {projects[key]["kb_id"] for key in case["sources"]}
    checks = []
    for expected in case["expected"]:
        project = projects[expected["source"]]
        own = [item for item in evidence
               if int((item.get("metadata") or {}).get("kb_id") or item.get("kb_id") or 0) == project["kb_id"]
               and item.get("source_name") == expected["filename"]]
        for pattern in expected["patterns"]:
            checks.append({
                "source": expected["source"], "filename": expected["filename"], "pattern": pattern,
                "evidence_ids": [item.get("id") for item in own
                                 if re.search(pattern, item.get("content") or "", re.I)],
                "answer_contains": bool(re.search(pattern, turn.get("answer") or "", re.I)),
            })
    ids = {int((item.get("metadata") or {}).get("kb_id") or item.get("kb_id") or 0)
           for item in evidence if ((item.get("metadata") or {}).get("kb_id") or item.get("kb_id"))}
    provenance_errors = [
        {"id": item.get("id"), "reason": "missing_provenance"}
        for item in evidence
        if not ((item.get("metadata") or {}).get("kb_id")
                and (item.get("metadata") or {}).get("local_record_id")
                and (item.get("metadata") or {}).get("source_content_hash"))
    ]
    return {
        "passed": turn.get("status") == "completed"
                  and all(check["evidence_ids"] and check["answer_contains"] for check in checks)
                  and ids == allowed and not provenance_errors,
        "evidence_kb_ids": sorted(ids),
        "checks": checks,
        "provenance_errors": provenance_errors,
        "retrieval_status": summary.get("status"),
        "target_coverage": summary.get("turn_evidence_coverage"),
        "retrieval_targets": summary.get("retrieval_targets"),
        "invocations": summary.get("invocations"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--golden", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5176")
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(Path(args.manifest).read_text())
    cases = json.loads(Path(args.golden).read_text())
    auth = json.loads(Path(args.auth_file).read_text())
    projects = {project["source"]: project for project in manifest["projects"]}
    report = {
        "round": args.round,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "username": manifest.get("username"),
        "base_url": args.base_url,
        "model_config": {
            "provider": "custom",
            "max_calls": 24,
            "deadline_seconds": 30,
            "fusion_max_bytes": "inherit(8000)",
            "turn_evidence_max_bytes": "inherit(8000)",
        },
        "cases": [],
    }
    report_path = output_dir / f"chat-round-{args.round}.json"
    headers = {"Authorization": f"Bearer {auth['token']}"}

    def save() -> None:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    with httpx.Client(base_url=args.base_url, headers=headers, timeout=120) as client:
        def call(method: str, path: str, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.is_error:
                print("API_ERROR", response.status_code, path, response.text[:500], flush=True)
            response.raise_for_status()
            return response.json()

        # Revalidate the original KB identities before using the old IDs.
        kb_rows = call("GET", "/api/v1/kbs")
        by_name = {row["name"]: row for row in kb_rows}
        for project in projects.values():
            row = by_name.get(project["kb_name"])
            if row is None or int(row["kb_id"]) != int(project["kb_id"]):
                report["preflight_error"] = f"kb_identity_changed:{project['kb_name']}"
                report["status"] = "not_executed"
                save()
                return 2

        parsing = {}
        for key, project in projects.items():
            rows = call("GET", f"/api/v1/kbs/{project['kb_name']}/files")
            parsing[key] = {row["name"]: row["status"] for row in rows}
        pending = [
            (case["id"], expected["source"], expected["filename"])
            for case in cases for expected in case["expected"]
            if parsing[expected["source"]].get(expected["filename"]) not in ("parsed", "indexed", "completed")
        ]
        if pending:
            report["status"] = "not_executed"
            report["not_executed_reason"] = {"parsing": pending}
            save()
            return 2

        pending_cases = list(cases)
        running = []
        deadline = time.monotonic() + args.timeout_seconds
        while (pending_cases or running) and time.monotonic() < deadline:
            for case in pending_cases[:]:
                selection = None
                if len(case["sources"]) > 1:
                    selection = {
                        "version": 1, "purpose": "compare",
                        "sources": [{"kb_id": projects[key]["kb_id"], "role": "peer"}
                                    for key in case["sources"]],
                    }
                session_body = {"kb_name": projects[case["sources"][0]]["kb_name"], "title": case["title"]}
                if selection:
                    session_body["knowledge_scope"] = selection
                session = call("POST", "/api/v1/conversations", json=session_body)
                request_body = {
                    "query": case["question"],
                    "client_request_id": f"optimization-{args.round}-{case['id']}",
                    "source_scope": "knowledge_base_only",
                }
                if selection:
                    request_body["knowledge_scope"] = selection
                turn = call("POST", f"/api/v1/conversations/{session['id']}/turns", json=request_body)["turn"]
                item = {**case, "session_id": session["id"], "turn_id": turn["id"], "selection": selection}
                report["cases"].append(item)
                running.append(item)
                pending_cases.remove(case)
                save()
                print("QUEUED", case["id"], session["id"], turn["id"], flush=True)

            for item in running[:]:
                turn = call("GET", "/api/v1/turns/" + item["turn_id"])
                item["turn"] = turn
                if turn["status"] in ("completed", "failed", "cancelled"):
                    item["messages"] = call("GET", f"/api/v1/conversations/{item['session_id']}/messages")
                    item["score"] = score_case(item, projects, turn)
                    running.remove(item)
                    save()
                    print("RESULT", item["id"], turn["status"],
                          "passed", item["score"]["passed"],
                          "coverage", item["score"]["evidence_kb_ids"], flush=True)
            save()
            if pending_cases or running:
                time.sleep(10)

        if pending_cases or running:
            report["status"] = "timeout"
            report["pending"] = [case["id"] for case in pending_cases]
            report["running"] = [case["id"] for case in running]
            save()
            return 4

    report["passed"] = all(item["score"]["passed"] for item in report["cases"])
    report["passed_count"] = sum(item["score"]["passed"] for item in report["cases"])
    report["status"] = "passed" if report["passed"] else "failed"
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save()
    print("CHAT_SUMMARY", report["passed_count"], "/", len(report["cases"]), flush=True)
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
