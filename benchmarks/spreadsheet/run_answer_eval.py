"""端到端答案层 A/B 评测: Agent(含/不含 SQL 工具) 回答 golden QA, 确定性打分.

用法(仓库根目录):
    .venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm a --limit 10
    .venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm b --limit 10
    .venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm b --ids AG01 --force
    .venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm b --ids PL04,MC05 --repeat 3
    .venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm report

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
import os  # noqa: E402

os.environ.setdefault("AGENT_TEMPERATURE", "0")

RESULTS_DIR = ROOT / "storage" / "eval" / "spreadsheet" / "aaa_eval"
KB_NAME = "aaa_eval"
DEPARTMENT_ID = "eval"

# 每题答案判定: must_include 全部命中才算 pass(确定性, 不用 LLM judge)
# 支持 "REGEX:<pattern>" 前缀做正则匹配(如计数的量词、HW-REQ-2 防误配 HW-REQ-20);
# 纯子串会把文档名里的数字(845506811 -> "8")误当答案, 计数类必须带量词/边界。
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
    "FL07": ["REGEX:(?<![0-9])HW-REQ-2(?![0-9])", "REGEX:(?<![0-9])HW-REQ-7(?![0-9])"],
    "CP01": ["AXI"], "CP02": ["EOL"], "CP03": ["AXI"],
    "CP04": ["C1618"], "CP05": ["完全复用"],
    "AG01": ["REGEX:(?<!\\d)23(?!\\d)"], "AG02": ["REGEX:8\\s*[个项]"], "AG03": ["REGEX:3\\s*[路条]"],
    "AG04": ["R538", "R1000"], "AG05": ["HW-SOL-009"],
    "MC01": ["L1200", "L1301"], "MC02": ["C1617"],
    "MC03": ["车身CAN0"],
    "MC04": ["REGEX:(?<![0-9])HW-REQ-2(?![0-9])", "REGEX:(?<![0-9])HW-REQ-7(?![0-9])"],
    "MC05": ["HW-SOL-005"],
}


def _anchor_matches(anchor: str, text: str) -> bool:
    """REGEX: 前缀或含 regex 标记的锚点走正则, 其余走大小写不敏感子串。

    中文里夹英文编号/型号时模型常自行加空格(如「车身 CAN0 高」), 对含中文的
    锚点再做一次空白不敏感匹配, 避免可读性空格被误判为 MISS。
    """
    if anchor.startswith("REGEX:"):
        return bool(re.search(anchor[6:], text, flags=re.IGNORECASE))
    if "(?!" in anchor or anchor.endswith(r"\b"):
        return bool(re.search(anchor, text, flags=re.IGNORECASE))
    if anchor.casefold() in text.casefold():
        return True
    if re.search(r"[\u4e00-\u9fff]", anchor):
        def squeeze(value: str) -> str:
            return re.sub(r"\s+", "", value.casefold())

        return squeeze(anchor) in squeeze(text)
    return False


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
    from src.pipelines.document_store import PipelineDocumentStore
    from src.pipelines.registry import (
        CONTENT_KIND_SPREADSHEET,
        DATASET_TABLE,
        PROCESSOR_KIND_SPREADSHEET,
    )
    from src.services.document_routing import TABLE_STATUS_INDEXED

    service = SpreadsheetIndexService()
    db_path = service.db_path(DEPARTMENT_ID, KB_NAME)
    need = not Path(db_path).exists()
    files = sorted((ROOT / "AAA").glob("*.xlsx"))
    assert files, "AAA 目录缺失"
    store = PipelineDocumentStore()
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
        # 生产链路由 IngestionOrchestrator 写目录账本; eval 直连服务需补登记,
        # 否则 list_kb_sources 会把有表格的知识库误报成"文档 0、表格 0"。
        store.upsert_document(
            kb_name=KB_NAME,
            document_name=path.name,
            dataset_kind=DATASET_TABLE,
            dataset_id=KB_NAME,
            document_id=f"eval-{i}",
            source_group="AAA",
            department_id=DEPARTMENT_ID,
            uploaded_by="eval",
            status=TABLE_STATUS_INDEXED,
            original_file_name=path.name,
            local_path=str(path),
            file_size=path.stat().st_size,
            content_kind=CONTENT_KIND_SPREADSHEET,
            processor_kind=PROCESSOR_KIND_SPREADSHEET,
            parse_progress=100,
            parse_stage=TABLE_STATUS_INDEXED,
        )


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


def run_one(runner, q, arm, run_index: int = 0) -> dict:
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
            thread_id=f"eval-{arm}-{q['id']}-r{run_index}",
            event_callback=on_event,
            query_mode="deep",
        ):
            parts.append(text)
    except Exception as exc:
        return {
            "id": q["id"], "run": run_index, "answer": "",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "latency_s": round(time.monotonic() - started, 1), "tools": [],
        }
    answer = "".join(parts)
    tools = [e.get("payload", {}).get("tool_name") for e in events if e.get("type") == "tool_started"]
    try:
        summary = runner.get_last_retrieval_summary()
    except Exception:
        summary = {}
    return {
        "id": q["id"], "run": run_index, "answer": answer, "error": None,
        "latency_s": round(time.monotonic() - started, 1),
        "tools": tools,
        "retrieval_ledger": summary.get("retrieval_ledger") or [],
        "tool_diagnostics": summary.get("tool_diagnostics") or [],
    }


def score_item(item: dict, q: dict) -> dict:
    answer = item.get("answer") or ""
    must = ANSWER_MUST_INCLUDE.get(q["id"], [])
    missing = [token for token in must if not _anchor_matches(token, answer)]
    # 锚点事实覆盖率(所有组)
    groups = q["anchor_groups"]
    hit = 0
    for group in groups:
        if all(_anchor_matches(anchor, answer) for anchor in group):
            hit += 1
    return {
        "pass": not missing and not item.get("error"),
        "must_missing": missing,
        "anchor_hit": f"{hit}/{len(groups)}",
        "anchor_ratio": round(hit / len(groups), 3) if groups else 1.0,
    }


def load_qa():
    return json.load(
        open(ROOT / "benchmarks" / "spreadsheet" / "golden_qa.json", encoding="utf-8")
    )["questions"]


def cmd_run(arm: str, limit: int | None, workers: int = 4, ids: str | None = None,
            force: bool = False, repeat: int = 1):
    qa = load_qa()
    out_path = RESULTS_DIR / f"answer_eval_{arm}.json"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    repeat = max(1, int(repeat))
    results: list[dict] = []
    if out_path.exists():
        results = json.load(open(out_path, encoding="utf-8"))["results"]
    runs_by_id: dict[str, int] = {}
    for item in results:
        runs_by_id[item["id"]] = runs_by_id.get(item["id"], 0) + 1
    wanted: set[str] | None = None
    if ids:
        wanted = {s.strip() for s in ids.split(",") if s.strip()}
    if force:
        if not wanted:
            raise SystemExit("--force 需要与 --ids 一起使用(强制重跑指定题)")
        results = [item for item in results if item["id"] not in wanted]
        runs_by_id = {}
        for item in results:
            runs_by_id[item["id"]] = runs_by_id.get(item["id"], 0) + 1
        from src.agents.runner import forget_thread

        for qid in sorted(wanted):
            for run_index in range(repeat):
                forget_thread(f"eval-{arm}-{qid}-r{run_index}")
    if arm == "a":
        disable_sql_tools()
    elif arm == "c":
        disable_row_search()
    runner = make_runner()

    tasks: list[tuple[dict, int]] = []
    for q in qa:
        if wanted is not None and q["id"] not in wanted:
            continue
        have = runs_by_id.get(q["id"], 0)
        for run_index in range(have, repeat):
            tasks.append((q, run_index))
    if limit:
        tasks = tasks[:limit]
    question_count = len({q["id"] for q, _ in tasks})
    print(
        f"arm={arm} 待跑 {len(tasks)} 题次({question_count} 题 × 至多 {repeat} 次, "
        f"已归档 {len(results)} 条), {workers} 并发",
        flush=True,
    )

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    lock = threading.Lock()

    def flush():
        json.dump({"arm": arm, "results": results}, open(out_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

    def worker(q, run_index):
        item = None
        for attempt in range(3):
            item = run_one(runner, q, arm, run_index)
            if not item.get("error"):
                break
            time.sleep(15 * (attempt + 1))
        item.update(score_item(item, q))
        with lock:
            results.append(item)
            flush()
        return q, item

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, q, run_index) for q, run_index in tasks]
        for future in as_completed(futures):
            q, item = future.result()
            flag = "PASS" if item.get("pass") else ("ERR " if item.get("error") else "MISS")
            print(f"  [{flag}] {q['id']:5s} r{item.get('run', 0)} {item.get('anchor_hit')} "
                  f"{item['latency_s']:>5.1f}s tools={','.join(dict.fromkeys(item['tools'])) or '-'}",
                  flush=True)
    print(f"-> {out_path}")


def cmd_rescore():
    """按当前 ANSWER_MUST_INCLUDE/锚点规则重算已归档答案(不重跑模型)。"""
    qa = {q["id"]: q for q in load_qa()}
    for arm in ("a", "b", "c"):
        path = RESULTS_DIR / f"answer_eval_{arm}.json"
        if not path.exists():
            continue
        data = json.load(open(path, encoding="utf-8"))
        results = data["results"]
        before = sum(1 for r in results if r.get("pass"))
        for r in results:
            if r["id"] in qa:
                r.update(score_item(r, qa[r["id"]]))
        after = sum(1 for r in results if r.get("pass"))
        json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"arm {arm}: pass {before}/{len(results)} -> {after}/{len(results)}")


def _group_by_id(results: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in results:
        grouped.setdefault(item["id"], []).append(item)
    return grouped


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
    header = f"{'category':16s}" + "".join(f"{a+' pass@k':>10s} {a+' fact':>10s}" for a in arms)
    print(header)
    for cat in cats + ["overall"]:
        ids = [qid for qid, q in qa.items() if cat == "overall" or q["category"] == cat]
        row = f"{cat:16s}"
        for arm, results in arms.items():
            grouped = _group_by_id(results)
            sel_ids = [qid for qid in ids if qid in grouped]
            if not sel_ids:
                row += f"{'-':>10s} {'-':>10s}"
                continue
            pass_at_k = sum(
                1 for qid in sel_ids if any(r.get("pass") for r in grouped[qid])
            ) / len(sel_ids)
            runs = [r for qid in sel_ids for r in grouped[qid]]
            fr = sum(float(r.get("anchor_ratio") or 0) for r in runs) / len(runs)
            row += f"{pass_at_k:>10.1%} {fr:>10.1%}"
        print(row)
    # 重复次数 / 稳定性 / 工具使用统计
    for arm, results in arms.items():
        grouped = _group_by_id(results)
        total_ids = len(grouped)
        sql_used = sum(
            1 for runs in grouped.values()
            if any("sql" in t for r in runs for t in r.get("tools", []))
        )
        print(f"arm {arm}: 使用 SQL 工具的题数 {sql_used}/{total_ids} (题次 {len(results)})")
        multi = {qid: runs for qid, runs in grouped.items() if len(runs) > 1}
        if not multi:
            continue
        stable_pass = sum(1 for runs in multi.values() if all(r.get("pass") for r in runs))
        stable_fail = sum(1 for runs in multi.values() if not any(r.get("pass") for r in runs))
        print(
            f"  重复题 {len(multi)} | 稳定通过 {stable_pass} | 稳定失败 {stable_fail} | "
            f"翻转 {len(multi) - stable_pass - stable_fail}"
        )
        flaky = [
            (qid, sum(1 for r in runs if r.get("pass")), len(runs))
            for qid, runs in multi.items()
            if 0 < sum(1 for r in runs if r.get("pass")) < len(runs)
        ]
        if flaky:
            detail = " ".join(f"{qid}:{passed}/{runs}" for qid, passed, runs in sorted(flaky))
            print(f"  翻转题(通过/次数): {detail}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["a", "b", "c", "report", "rescore"], required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ids", type=str, default=None, help="逗号分隔的题目ID, 如 AG01,AG02")
    parser.add_argument("--force", action="store_true",
                        help="与 --ids 连用: 重跑已完成的题目(清除其 checkpoint 后重新作答)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=1,
                        help="每题重复次数(默认1); >1 时报告按 pass@k 与稳定性统计")
    args = parser.parse_args()
    if args.arm == "report":
        cmd_report()
    elif args.arm == "rescore":
        cmd_rescore()
    else:
        ingest_via_service()
        cmd_run(args.arm, args.limit, args.workers, args.ids, args.force, args.repeat)


if __name__ == "__main__":
    main()
