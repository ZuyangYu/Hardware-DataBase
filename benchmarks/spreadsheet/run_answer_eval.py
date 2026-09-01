"""端到端答案层 A/B 评测: Agent(含/不含 SQL 工具) 回答 golden QA, 确定性打分.

用法(仓库根目录):
    .venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm a --limit 10
    .venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm b --limit 10
    .venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --report

arm a = 禁用 SQL 工具(近似优化前); arm b = 完整工具(优化后)。
断点续跑: 结果按 arm 追加写入 JSON, 已答题目自动跳过。
temperature 由环境变量强制为 0(load_dotenv 不覆盖已有 env), 消除采样方差。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 必须在 import src.settings 之前设置
import os

os.environ.setdefault("AGENT_TEMPERATURE", "0")

RESULTS_DIR = ROOT / "storage" / "eval" / "spreadsheet" / "aaa_eval"
KB_NAME = "aaa_eval"
DEPARTMENT_ID = "eval"

# 每题答案判定: must_include 全部命中才算 pass(确定性, 不用 LLM judge)
ANSWER_MUST_INCLUDE = {
    "PL01": ["1318382-8"], "PL02": ["E6S20A-40MT5-C"], "PL03": ["I_S_WKUP"],
    "PL04": ["系统供电9~16V"], "PL05": ["Y900"], "PL06": ["ASIL B"],
    "PL07": ["CAN0"], "PL08": ["ID_SYS_RE_006"],
    "FL01": ["CAN1H", "CAN2H", "CAN3H"],
    "FL02": ["R1921", "R1917"],
    "FL03": ["C1621", "C1633"],
    "FL04": ["L1200", "L1300"],
    "FL05": ["RAM", "DDR"],
    "FL06": ["QFN", "SOD"],
    "FL07": ["HW-REQ-2", "HW-REQ-7"],
    "CP01": ["AXI"], "CP02": ["EOL"], "CP03": ["AXI"],
    "CP04": ["C1618"], "CP05": ["完全复用"],
    "AG01": ["23"], "AG02": ["8"], "AG03": ["3"],
    "AG04": ["R538", "R1000"], "AG05": ["HW-SOL-009"],
    "MC01": ["L1200", "L1301"], "MC02": ["C1617"],
    "MC03": ["车身CAN0"], "MC04": ["HW-REQ-2", "HW-REQ-7"], "MC05": ["HW-SOL-005"],
}


def build_ctx():
    from src.pipelines.document_rag.schemas import RequestContext

    return RequestContext(
        user_id="eval",
        roles=["dept_admin"],
        metadata={"department_id": DEPARTMENT_ID},
    )


def ingest_via_service() -> None:
    from src.services.spreadsheet_index_service import SpreadsheetIndexService
    from src.pipelines.spreadsheet.pipeline import SpreadsheetIndexRequest

    service = SpreadsheetIndexService()
    db_path = service.db_path(DEPARTMENT_ID, KB_NAME)
    need = not Path(db_path).exists()
    files = sorted((ROOT / "AAA").glob("*.xlsx"))
    assert files, "AAA 目录缺失"
    for i, path in enumerate(files, start=1):
        if need:
            result = service.parse_and_index(
                SpreadsheetIndexRequest(
                    record_id=i,
                    kb_name=KB_NAME,
                    department_id=DEPARTMENT_ID,
                    document_name=path.name,
                    source_group="AAA",
                    file_path=str(path),
                    local_path=str(path),
                    content_hash="answer-eval",
                )
            )
            print(f"  ingest {path.name[:40]}: ok={result.ok}")


def make_runner():
    from src.agents.runner import MultiSourceAgentRunner

    class _StubBackend:
        def list_documents(self, *a, **k):
            return []

    class _StubConversation:
        pass

    return MultiSourceAgentRunner(
        rag_backend=_StubBackend(),
        document_store=None,
        spreadsheet_service=None,  # 用默认 SpreadsheetIndexService
        circuit_service=None,
        conversation_service=_StubConversation(),
    )


def disable_sql_tools():
    import src.agents.runner as runner_module

    runner_module.make_spreadsheet_sql_tools = lambda rt, service: ()
    print("  [arm a] SQL 工具已禁用")


def disable_row_search():
    import src.agents.runner as runner_module
    from src.agents.tools.spreadsheet_tools import make_spreadsheet_tools as _orig

    def filtered(rt, service):
        return [t for t in _orig(rt, service)
                if getattr(t, "__name__", "") != "spreadsheet_row_search"]

    runner_module.make_spreadsheet_tools = filtered
    print("  [arm c] row_search 已隐藏, 强制 SQL 路径")


TOOL_EVENTS = ("tool_started", "tool_result")


def run_one(runner, q, arm) -> dict:
    events = []

    def on_event(evt):
        if evt.get("type") in TOOL_EVENTS:
            events.append(evt)

    started = time.monotonic()
    parts = []
    try:
        for text in runner.stream(
            query=q["question"],
            kb_name=KB_NAME,
            history=[],
            ctx=build_ctx(),
            thread_id=f"eval-{arm}-{q['id']}",
            event_callback=on_event,
            query_mode="deep",
        ):
            parts.append(text)
    except Exception as exc:
        return {
            "id": q["id"], "answer": "", "error": f"{type(exc).__name__}: {exc}"[:300],
            "latency_s": round(time.monotonic() - started, 1), "tools": [],
        }
    answer = "".join(parts)
    tools = [e.get("payload", {}).get("tool_name") for e in events if e.get("type") == "tool_started"]
    return {
        "id": q["id"], "answer": answer, "error": None,
        "latency_s": round(time.monotonic() - started, 1),
        "tools": tools,
    }


def score_item(item: dict, q: dict) -> dict:
    answer = item.get("answer") or ""
    must = ANSWER_MUST_INCLUDE.get(q["id"], [])
    missing = [token for token in must if token.casefold() not in answer.casefold()]
    # 锚点事实覆盖率(所有组)
    groups = q["anchor_groups"]
    hit = 0
    for group in groups:
        ok = True
        for anchor in group:
            if "(?!" in anchor or anchor.endswith(r"\b"):
                if not re.search(anchor, answer, flags=re.IGNORECASE):
                    ok = False
                    break
            elif anchor.casefold() not in answer.casefold():
                ok = False
                break
        if ok:
            hit += 1
    return {
        "pass": not missing and not item.get("error"),
        "must_missing": missing,
        "anchor_hit": f"{hit}/{len(groups)}",
        "anchor_ratio": round(hit / len(groups), 3) if groups else 1.0,
    }


def load_qa():
    return json.load(open(ROOT / "evals" / "spreadsheet" / "golden_qa.json", encoding="utf-8"))["questions"]


def cmd_run(arm: str, limit: int | None, workers: int = 4, ids: str | None = None):
    qa = load_qa()
    out_path = RESULTS_DIR / f"answer_eval_{arm}.json"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    done = {}
    if out_path.exists():
        done = {d["id"]: d for d in json.load(open(out_path, encoding="utf-8"))["results"]}
    if arm == "a":
        disable_sql_tools()
    elif arm == "c":
        disable_row_search()
    runner = make_runner()
    results = list(done.values())
    pending = [q for q in qa if q["id"] not in done]
    if ids:
        wanted = {s.strip() for s in ids.split(",") if s.strip()}
        pending = [q for q in pending if q["id"] in wanted]
    if limit:
        pending = pending[:limit]
    print(f"arm={arm} 待跑 {len(pending)} 题(已完成 {len(done)}), {workers} 并发", flush=True)

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    lock = threading.Lock()

    def flush():
        json.dump({"arm": arm, "results": results}, open(out_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

    def worker(q):
        item = None
        for attempt in range(3):
            item = run_one(runner, q, arm)
            if not item.get("error"):
                break
            time.sleep(15 * (attempt + 1))
        item.update(score_item(item, q))
        with lock:
            results.append(item)
            flush()
        return q, item

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, q) for q in pending]
        for future in as_completed(futures):
            q, item = future.result()
            flag = "PASS" if item.get("pass") else ("ERR " if item.get("error") else "MISS")
            print(f"  [{flag}] {q['id']:5s} {item.get('anchor_hit')} {item['latency_s']:>5.1f}s "
                  f"tools={','.join(dict.fromkeys(item['tools'])) or '-'}", flush=True)
    print(f"-> {out_path}")


def cmd_report():
    qa = {q["id"]: q for q in load_qa()}
    arms = {}
    for arm in ("a", "b", "c"):
        path = RESULTS_DIR / f"answer_eval_{arm}.json"
        if path.exists():
            arms[arm] = json.load(open(path, encoding="utf-8"))["results"]
    if not arms:
        print("尚无结果")
        return
    cats = sorted({q["category"] for q in qa.values()})
    header = f"{'category':16s}" + "".join(f"{a+' pass':>10s} {a+' fact':>10s}" for a in arms)
    print(header)
    for cat in cats + ["overall"]:
        ids = [qid for qid, q in qa.items() if cat == "overall" or q["category"] == cat]
        row = f"{cat:16s}"
        for arm, results in arms.items():
            sel = [r for r in results if r["id"] in ids]
            if not sel:
                row += f"{'-':>10s} {'-':>10s}"
                continue
            pr = sum(1 for r in sel if r.get("pass")) / len(sel)
            fr = sum(float(r.get("anchor_ratio") or 0) for r in sel) / len(sel)
            row += f"{pr:>10.1%} {fr:>10.1%}"
        print(row)
    # 工具使用统计
    for arm, results in arms.items():
        sql_used = sum(1 for r in results if any("sql" in t for t in r.get("tools", [])))
        print(f"arm {arm}: 使用 SQL 工具的题数 {sql_used}/{len(results)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["a", "b", "c", "report"], required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ids", type=str, default=None, help="逗号分隔的题目ID, 如 AG01,AG02")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.arm == "report":
        cmd_report()
    else:
        ingest_via_service()
        cmd_run(args.arm, args.limit, args.workers, args.ids)


if __name__ == "__main__":
    main()
