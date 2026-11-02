#!/usr/bin/env python3
"""Run many real knowledge-base questions against the live API.

The test creates one isolated session per question, submits turns with bounded
concurrency, polls to a terminal state, and reports retrieval/candidate/context
metrics plus latency percentiles.  It is intentionally a stress harness, not a
correctness oracle: answer quality is reported (citations, evidence counts),
while failures and timeouts are surfaced explicitly.

Credentials come from ``--username/--password`` or
``HDB_E2E_USER/HDB_E2E_PASSWORD`` and are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"completed", "failed", "cancelled"}
CITATION_RE = re.compile(r"\[(\d+)\]")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 1)


def parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        event_type = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        if not event_type or not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        events.append({"type": event_type, "payload": payload})
    return events


def load_questions(path: str, limit: int) -> list[dict[str, str]]:
    raw = json.loads(Path(path).read_text())
    questions: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            questions.append({"id": f"q{index + 1}", "question": item})
        elif isinstance(item, dict):
            question = str(item.get("question") or item.get("query") or "").strip()
            if question:
                questions.append({"id": str(item.get("id") or f"q{index + 1}"), "question": question})
    return questions[: max(1, limit)]


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


async def run_question(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    question: dict[str, str],
    index: int,
    total: int,
    args: argparse.Namespace,
    progress: dict[str, Any],
) -> dict[str, Any]:
    async with semaphore:
        started = time.monotonic()
        result: dict[str, Any] = {
            "id": question["id"],
            "question": question["question"],
            "status": "error",
            "error": "",
        }
        try:
            session = await request_json(
                client,
                "POST",
                "/api/v1/conversations",
                json={"kb_name": args.kb, "title": f"stress {args.run_id} #{index + 1}"},
            )
            session_id = int(session["id"])
            turn = await request_json(
                client,
                "POST",
                f"/api/v1/conversations/{session_id}/turns",
                json={
                    "query": question["question"],
                    "source_scope": "knowledge_base_only",
                    "client_request_id": f"stress-{args.run_id}-{question['id']}",
                },
            )
            turn_id = str(turn["turn"]["id"])
            await request_json(client, "POST", f"/api/v1/turns/{turn_id}/start")
            result.update({"session_id": session_id, "turn_id": turn_id})

            deadline = time.monotonic() + args.timeout_seconds
            detail: dict[str, Any] = {}
            while time.monotonic() < deadline:
                detail = await request_json(client, "GET", f"/api/v1/turns/{turn_id}")
                if detail.get("status") in TERMINAL:
                    break
                await asyncio.sleep(args.poll_interval)
            else:
                result["status"] = "timeout"
                result["error"] = f"turn did not finish in {args.timeout_seconds}s"
                return result

            result["status"] = str(detail.get("status") or "unknown")
            result["error"] = str(detail.get("error_message") or detail.get("error_stage") or "")
            summary = detail.get("summary") or {}
            metrics = detail.get("metrics") or {}
            events = parse_sse(
                (await client.get(f"/api/v1/turns/{turn_id}/events")).text
            )
            tool_results = [
                event["payload"]
                for event in events
                if event["type"] == "tool_result"
            ]
            candidate_total = sum(int(item.get("candidate_count") or 0) for item in tool_results)
            evidence_read_calls = sum(
                1 for item in tool_results if item.get("tool_name") == "evidence_read"
            )
            compactions = sum(1 for event in events if event["type"] == "context_compacted")
            pressures = sum(1 for event in events if event["type"] == "context_pressure")
            answer = str(detail.get("answer") or "")
            budget = summary.get("budget") or detail.get("budget") or {}
            result.update(
                {
                    "answer_chars": len(answer),
                    "citation_count": len(set(CITATION_RE.findall(answer))),
                    "evidence_count": len(summary.get("evidence") or []),
                    "candidate_count": candidate_total,
                    "evidence_read_calls": evidence_read_calls,
                    "tool_result_count": len(tool_results),
                    "context_compacted": compactions,
                    "context_pressure": pressures,
                    "budget_status": budget.get("status") or "",
                    "budget_exhausted": bool(budget.get("exhausted")),
                    "stopped_reason": budget.get("stopped_reason") or "",
                    "recursion_limit": budget.get("recursion_limit"),
                    "partial_answer": bool(budget.get("partial_answer")),
                    "queue_ms": int(metrics.get("queue_ms") or 0),
                    "total_ms": int(metrics.get("total_ms") or 0),
                    "e2e_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
        except Exception as exc:  # noqa: BLE001 - stress harness records failures
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            async with progress["lock"]:
                progress["completed"] += 1
                print(
                    f"[{progress['completed']}/{total}] {question['id']} "
                    f"{result['status']} evidence={result.get('evidence_count', 0)} "
                    f"candidates={result.get('candidate_count', 0)} "
                    f"e2e={result.get('e2e_ms', result['elapsed_ms'])}ms "
                    f"{result.get('error', '')[:120]}",
                    flush=True,
                )
        return result


async def main_async(args: argparse.Namespace) -> int:
    questions = load_questions(args.questions, args.limit)
    total = len(questions)
    args.run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(
        f"stress start run={args.run_id} kb={args.kb} questions={total} "
        f"concurrency={args.concurrency}",
        flush=True,
    )
    async with httpx.AsyncClient(
        base_url=args.api_url.rstrip("/"),
        timeout=httpx.Timeout(args.request_timeout_seconds),
        limits=httpx.Limits(
            max_connections=max(4, args.concurrency * 2),
            max_keepalive_connections=max(4, args.concurrency),
        ),
    ) as client:
        login = await request_json(
            client,
            "POST",
            "/api/v1/login",
            json={"username": args.username, "password": args.password},
        )
        client.headers["Authorization"] = "Bearer " + login["token"]
        kbs = await request_json(client, "GET", "/api/v1/kbs")
        available = {row.get("name") for row in kbs}
        if args.kb not in available:
            print(f"KB_NOT_VISIBLE {args.kb} available={sorted(available)}", flush=True)
            return 2

        semaphore = asyncio.Semaphore(args.concurrency)
        progress = {"completed": 0, "lock": asyncio.Lock()}
        tasks = [
            asyncio.create_task(
                run_question(client, semaphore, question, index, total, args, progress)
            )
            for index, question in enumerate(questions)
        ]
        results = await asyncio.gather(*tasks)

    completed = [item for item in results if item["status"] == "completed"]
    failed = [item for item in results if item["status"] != "completed"]
    latencies = [item["e2e_ms"] for item in completed]
    queues = [item["queue_ms"] for item in completed]
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "api_url": args.api_url,
        "kb": args.kb,
        "concurrency": args.concurrency,
        "question_count": total,
        "completed": len(completed),
        "failed": len(failed),
        "success_rate": round(len(completed) / total, 3) if total else 0,
        "e2e_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 1) if latencies else None,
            "mean": round(statistics.fmean(latencies), 1) if latencies else None,
        },
        "queue_ms": {
            "p50": percentile(queues, 0.50),
            "p95": percentile(queues, 0.95),
            "max": max(queues) if queues else None,
        },
        "totals": {
            "evidence": sum(int(item.get("evidence_count") or 0) for item in completed),
            "candidates": sum(int(item.get("candidate_count") or 0) for item in completed),
            "evidence_read_calls": sum(int(item.get("evidence_read_calls") or 0) for item in completed),
            "tool_results": sum(int(item.get("tool_result_count") or 0) for item in completed),
            "context_compacted": sum(int(item.get("context_compacted") or 0) for item in completed),
            "context_pressure": sum(int(item.get("context_pressure") or 0) for item in completed),
            "budget_exhausted": sum(1 for item in completed if item.get("budget_exhausted")),
            "recursion_limited": sum(1 for item in completed if item.get("stopped_reason") == "recursion_limit"),
            "partial_answers": sum(1 for item in completed if item.get("partial_answer")),
            "answers_with_citations": sum(1 for item in completed if item.get("citation_count")),
        },
        "failures": [
            {"id": item["id"], "question": item["question"], "status": item["status"], "error": item["error"]}
            for item in failed
        ],
        "results": results,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        "SUMMARY "
        f"completed={report['completed']}/{total} "
        f"success_rate={report['success_rate']:.1%} "
        f"p50={report['e2e_ms']['p50']}ms p95={report['e2e_ms']['p95']}ms "
        f"evidence={report['totals']['evidence']} candidates={report['totals']['candidates']} "
        f"evidence_read={report['totals']['evidence_read_calls']} "
        f"compactions={report['totals']['context_compacted']} "
        f"recursion_limited={report['totals']['recursion_limited']} "
        f"budget_exhausted={report['totals']['budget_exhausted']} "
        f"report={args.output}",
        flush=True,
    )
    if args.fail_on_error and failed:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8002")
    parser.add_argument("--username", default=os.getenv("HDB_E2E_USER", ""))
    parser.add_argument("--password", default=os.getenv("HDB_E2E_PASSWORD", ""))
    parser.add_argument("--kb", default="AAA")
    parser.add_argument("--questions", default=str(Path(__file__).with_name("adas_stress_questions.json")))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--request-timeout-seconds", type=int, default=120)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output", default="/tmp/hdb-question-stress.json")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        parser.error("set HDB_E2E_USER/HDB_E2E_PASSWORD or pass --username/--password")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
