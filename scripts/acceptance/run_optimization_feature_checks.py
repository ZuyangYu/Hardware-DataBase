#!/usr/bin/env python3
"""Live feature checks for candidate retrieval and the quiet chat UI.

The script exercises the running API end-to-end instead of mocking tools:

1. a real KB turn completes with citations;
2. broad retrieval leaves non-citable candidates and the model reads some of
   them through ``evidence_read``;
3. typed tool events and the persisted trace carry ``candidate_count``;
4. the frontend source no longer renders budget/partial/verification cards;
5. a persisted ``context_compacted`` event, when available, keeps its typed
   envelope and never becomes a chat card.

Credentials are read from ``--username/--password`` or
``HDB_E2E_USER/HDB_E2E_PASSWORD`` and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"completed", "failed", "cancelled"}
FORBIDDEN_UI_STRINGS = (
    "BudgetPanel",
    "VerificationPanel",
    "系统校验",
    "缩小范围",
    "本轮为部分交付",
    "本轮执行预算已用完",
)


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


def find_compaction(client: httpx.Client, session_limit: int = 8, turn_limit: int = 3):
    sessions = client.get("/api/v1/conversations").json()
    checked = 0
    for session in sessions[:session_limit]:
        turns = client.get(
            f"/api/v1/conversations/{session['id']}/turns",
            params={"include_terminal": "true"},
        ).json()
        for turn in turns[:turn_limit]:
            if turn.get("status") not in TERMINAL:
                continue
            checked += 1
            events = parse_sse(client.get(f"/api/v1/turns/{turn['id']}/events").text)
            compaction = next(
                (event for event in events if event["type"] == "context_compacted"),
                None,
            )
            if compaction is not None:
                return {"session_id": session["id"], "turn_id": turn["id"], "event": compaction}
    return {"checked_turns": checked, "event": None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8002")
    parser.add_argument("--front-url", default="http://127.0.0.1:5174")
    parser.add_argument("--username", default=os.getenv("HDB_E2E_USER", ""))
    parser.add_argument("--password", default=os.getenv("HDB_E2E_PASSWORD", ""))
    parser.add_argument("--kb", default="AAA")
    parser.add_argument(
        "--query",
        default="ADAS 产品的硬件架构包含哪些主要模块？请给出依据。",
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--turn-id", default="", help="reuse a completed turn instead of running a new one")
    parser.add_argument("--compaction-turn", default="", help="turn id expected to contain context_compacted")
    parser.add_argument("--parallel-turn", default="", help="turn id expected to contain overlapping parallel searches")
    parser.add_argument("--ui-build", default="", help="expected frontend build marker (optional)")
    parser.add_argument("--output", default="/tmp/hdb-optimization-feature-checks.json")
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        parser.error("set HDB_E2E_USER/HDB_E2E_PASSWORD or pass --username/--password")
    args.api_url = args.api_url.rstrip("/")
    args.front_url = args.front_url.rstrip("/")

    checks: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {
        "api_url": args.api_url,
        "front_url": args.front_url,
        "kb": args.kb,
        "query": args.query,
        "checks": checks,
    }

    def check(name: str, passed: bool, **detail: Any) -> None:
        checks[name] = {"passed": bool(passed), **detail}
        print(("PASS " if passed else "FAIL ") + name, flush=True)

    with httpx.Client(base_url=args.api_url, timeout=60) as client:
        login = client.post(
            "/api/v1/login",
            json={"username": args.username, "password": args.password},
        )
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["token"]

        kbs = client.get("/api/v1/kbs").json()
        kb_names = {row.get("name") for row in kbs}
        check("kb_visible", args.kb in kb_names, available=sorted(kb_names))
        if args.kb not in kb_names:
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            return 2

        if args.turn_id:
            turn_id = args.turn_id
            turn_data = client.get(f"/api/v1/turns/{turn_id}").json()
            session_id = int(turn_data["session_id"])
            report.update({"session_id": session_id, "turn_id": turn_id})
        else:
            session = client.post(
                "/api/v1/conversations",
                json={"kb_name": args.kb, "title": "optimization feature checks"},
            )
            session.raise_for_status()
            session_id = session.json()["id"]
            turn = client.post(
                f"/api/v1/conversations/{session_id}/turns",
                json={"query": args.query, "source_scope": "knowledge_base_only"},
            )
            turn.raise_for_status()
            turn_id = turn.json()["turn"]["id"]
            client.post(f"/api/v1/turns/{turn_id}/start").raise_for_status()
            report.update({"session_id": session_id, "turn_id": turn_id})

            deadline = time.monotonic() + args.timeout_seconds
            turn_data = {}
            while time.monotonic() < deadline:
                response = client.get(f"/api/v1/turns/{turn_id}")
                if response.status_code == 200:
                    turn_data = response.json()
                    if turn_data.get("status") in TERMINAL:
                        break
                time.sleep(3)
        report["turn_status"] = turn_data.get("status")
        check("turn_completed", turn_data.get("status") == "completed")

        events = parse_sse(client.get(f"/api/v1/turns/{turn_id}/events").text)
        tool_results = [
            event["payload"]
            for event in events
            if event["type"] == "tool_result"
        ]
        tool_starts = [
            event["payload"]
            for event in events
            if event["type"] == "tool_started"
        ]
        candidate_total = sum(int(item.get("candidate_count") or 0) for item in tool_results)
        evidence_read_calls = [
            item for item in tool_results if item.get("tool_name") == "evidence_read"
        ]
        report["tool_results"] = [
            {
                "tool_name": item.get("tool_name"),
                "hit_count": item.get("hit_count"),
                "evidence_count": item.get("evidence_count"),
                "candidate_count": item.get("candidate_count"),
                "status": item.get("status"),
            }
            for item in tool_results
        ]
        check("candidate_mode_active", candidate_total > 0, candidates=candidate_total)
        check("evidence_read_used", bool(evidence_read_calls), calls=len(evidence_read_calls))
        check(
            "tool_events_typed",
            all(
                item.get("schema_version") == 1
                and item.get("item_id")
                and item.get("call_id")
                for item in tool_starts + tool_results
            ),
        )
        check(
            "candidate_count_on_every_tool_result",
            all("candidate_count" in item for item in tool_results),
        )

        answer = str(turn_data.get("answer") or "")
        check("answer_has_citations", bool(re.search(r"\[\d+\]", answer)), answer_chars=len(answer))
        evidence = (turn_data.get("summary") or {}).get("evidence") or []
        report["formal_evidence_count"] = len(evidence)
        check("formal_evidence_within_budget", len(evidence) <= 30, count=len(evidence))
        check(
            "context_manifest_present",
            "context_manifest" in (turn_data.get("summary") or {}),
        )

        messages = client.get(f"/api/v1/conversations/{session_id}/messages").json()
        assistant = next(
            (message for message in messages if message.get("role") == "assistant" and message.get("content")),
            None,
        )
        steps = (assistant or {}).get("trace_steps") or []
        report["persisted_candidate_steps"] = sum(
            1 for step in steps if int(step.get("candidate_count") or 0) > 0
        )
        check(
            "candidate_count_replayed_after_refresh",
            report["persisted_candidate_steps"] > 0,
            steps=len(steps),
        )
        if args.parallel_turn:
            parallel_events = parse_sse(
                client.get(f"/api/v1/turns/{args.parallel_turn}/events").text
            )
            parallel_starts = [
                event["payload"]
                for event in parallel_events
                if event["type"] == "tool_started"
                and event["payload"].get("tool_name") == "document_search"
            ]
            parallel_results = [
                event["payload"]
                for event in parallel_events
                if event["type"] == "tool_result"
                and event["payload"].get("tool_name") == "document_search"
            ]
            start_ids = [item.get("call_id") for item in parallel_starts]
            result_ids = [item.get("call_id") for item in parallel_results]
            start_indexes = [
                index for index, event in enumerate(parallel_events)
                if event["type"] == "tool_started"
                and event["payload"].get("tool_name") == "document_search"
            ]
            result_indexes = [
                index for index, event in enumerate(parallel_events)
                if event["type"] == "tool_result"
                and event["payload"].get("tool_name") == "document_search"
            ]
            report["parallel"] = {
                "start_ids": start_ids,
                "result_ids": result_ids,
                "starts": len(start_ids),
                "results": len(result_ids),
            }
            check(
                "parallel_tool_ids_unique_and_paired",
                len(start_ids) >= 2
                and len(start_ids) == len(set(start_ids))
                and set(start_ids) == set(result_ids),
                start_ids=start_ids,
                result_ids=result_ids,
            )
            check(
                "parallel_tool_calls_overlap",
                bool(start_indexes)
                and bool(result_indexes)
                and max(start_indexes) < min(result_indexes),
                last_start=max(start_indexes) if start_indexes else None,
                first_result=min(result_indexes) if result_indexes else None,
            )
            check(
                "parallel_tool_results_typed",
                all(
                    item.get("schema_version") == 1
                    and item.get("item_id") == f"tool:{item.get('call_id')}"
                    for item in parallel_starts + parallel_results
                )
                and all("candidate_count" in item for item in parallel_results),
            )
        if args.compaction_turn:
            compaction_events = parse_sse(
                client.get(f"/api/v1/turns/{args.compaction_turn}/events").text
            )
            compaction = {
                "turn_id": args.compaction_turn,
                "event": next(
                    (event for event in compaction_events if event["type"] == "context_compacted"),
                    None,
                ),
            }
        else:
            compaction = find_compaction(client)

    report["compaction"] = {
        "session_id": compaction.get("session_id"),
        "turn_id": compaction.get("turn_id"),
        "checked_turns": compaction.get("checked_turns"),
    }
    compaction_event = compaction.get("event")
    if compaction_event is not None:
        payload = compaction_event["payload"]
        report["compaction"]["payload"] = {
            key: payload.get(key)
            for key in ("schema_version", "item_id", "stage", "trigger", "dropped_messages", "before_tokens", "after_tokens")
        }
        check(
            "compaction_event_typed",
            payload.get("schema_version") == 1
            and bool(payload.get("item_id"))
            and "dropped_messages" in payload,
            stage=payload.get("stage"),
        )
    else:
        checks["compaction_event_typed"] = {
            "passed": not bool(args.compaction_turn),
            "skipped": not bool(args.compaction_turn),
            "reason": "no persisted compaction event in the recent sessions"
            if not args.compaction_turn
            else f"no context_compacted event in turn {args.compaction_turn}",
        }
        print("SKIP compaction_event_typed (no persisted event)", flush=True)

    with httpx.Client(base_url=args.front_url, timeout=30) as front:
        bubble = front.get("/src/pages/chat/components/MessageBubble.tsx")
        bubble.raise_for_status()
        source = bubble.text
        present = [text for text in FORBIDDEN_UI_STRINGS if text in source]
        check("frontend_meta_cards_removed", not present, found=present)
        main_tsx = front.get("/src/main.tsx")
        main_tsx.raise_for_status()
        marker = re.search(r"const UI_BUILD\s*=\s*['\"]([^'\"]+)['\"]", main_tsx.text)
        report["ui_build"] = marker.group(1) if marker else ""
        check(
            "frontend_build_marker_updated",
            bool(report["ui_build"]) and (not args.ui_build or report["ui_build"] == args.ui_build),
            marker=report["ui_build"],
            expected=args.ui_build or "any non-empty marker",
        )

    passed = all(item.get("passed") for item in checks.values())
    report["passed"] = passed
    report["passed_count"] = sum(1 for item in checks.values() if item.get("passed"))
    report["check_count"] = len(checks)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        f"SUMMARY {report['passed_count']}/{report['check_count']} "
        f"candidates={candidate_total} evidence={report['formal_evidence_count']} "
        f"answer_chars={len(answer)} report={args.output}",
        flush=True,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
