"""AAA Excel QA baseline 评测 harness.

用法(仓库根目录):
    .venv/bin/python benchmarks/spreadsheet/run_eval.py

输入: <repo>/AAA/*.xlsx (gitignore 的本地数据, 与 golden_qa.json 的 file_keys 对应)
输出: <repo>/storage/eval/spreadsheet/aaa_eval/latest_report.json (gitignore)
基线: benchmarks/spreadsheet/baseline_report.json (2026-08-31 存档, 优化对比的基准)

层1(解析保真): 每个 anchor_group 是否能被 table_semantic_rows 表示(按 file+sheet 定位)
层2(检索召回): 用问题原文/关键词两种 query, 走与 SpreadsheetSemanticTool/SpreadsheetCellTool
              完全一致的 SQL+重排路径, 统计 recall@k 与 MRR
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.pipelines.spreadsheet.table_store import TableIndexStore
from src.agents.tools.spreadsheet_tools import (
    _tokens, _like_clauses, _token_match_order, _rank_rows, _candidate_limit,
)

EVAL_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT / "storage" / "eval" / "spreadsheet" / "aaa_eval" / "table_indexes.db"
REPORT_PATH = DB_PATH.parent / "latest_report.json"

FILES = [
    "AAA/600606894_ADAS_产品硬件架构设计说明书ProductHardwareArchitectureDesignSpecification.xlsx",
    "AAA/600608964_ADAS_FPT_0506_1954_shoulin.wang.xlsx",
    "AAA/600608964_ADAS_ICD_0506_1957_shoulin.wang.xlsx",
    "AAA/600608964_ADAS_Testcoverage_0506_1958_shoulin.wang.xlsx",
    "AAA/600608964_ADAS_产品硬件需求规格说明书ProductHardwareRequirementSpecification.xlsx",
    "AAA/845506811_ADAS_HWDebug.xlsx",
    "AAA/845506811_ADAS_SpecificationForDefectParts_0506_1956_shoulin.wang.xlsx",
]


def ingest() -> dict:
    os.makedirs(DB_PATH.parent, exist_ok=True)
    store = TableIndexStore(db_path=str(DB_PATH))
    stats = {}
    for i, rel in enumerate(FILES, 1):
        path = ROOT / rel
        name = os.path.basename(rel)
        s = store.index_xlsx(
            record_id=i,
            kb_name="aaa_eval",
            department_id="eval",
            document_name=name,
            source_group="AAA",
            file_path=str(path),
            local_path=str(path),
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        stats[name] = {
            "sheet_count": s.sheet_count if hasattr(s, "sheet_count") else None,
            "cell_count": getattr(s, "cell_count", None),
            "semantic_row_count": getattr(s, "semantic_row_count", None),
            "warnings": getattr(s, "warnings", [])[:5],
        }
    return stats


def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def group_matches_row(group: list[str], text: str) -> bool:
    """一个 anchor_group 的全部 anchor(子串或 regex)须命中同一行文本."""
    for a in group:
        if a.startswith("REGEX:"):
            if not re.search(a[6:], text, flags=re.IGNORECASE):
                return False
        elif a.endswith(r"\b") or "\\b" in a or "(?!" in a:
            # 数据集里直接写了 regex 形式(如 HW-REQ-2(?![0-9]))
            if not re.search(a, text, flags=re.IGNORECASE):
                return False
        else:
            if a.casefold() not in text.casefold():
                return False
    return True


def row_text(row) -> str:
    return " ".join(
        str(row[k] or "")
        for k in ("semantic_text", "raw_text", "values_json", "raw_values_json")
    )


def key_of(rel_path: str) -> str:
    meta_key_map = [
        ("产品硬件架构设计说明书", "arch"),
        ("ADAS_FPT_0506_1954", "fpt"),
        ("ADAS_ICD_0506_1957", "icd"),
        ("ADAS_Testcoverage_0506_1958", "testcov"),
        ("产品硬件需求规格说明书", "req"),
        ("ADAS_HWDebug", "debug"),
        ("SpecificationForDefectParts", "defect"),
    ]
    for marker, key in meta_key_map:
        if marker in rel_path:
            return key
    raise KeyError(rel_path)


def rec_id_of(file_key: str) -> int:
    return FILES.index(next(f for f in FILES if key_of(f) == file_key)) + 1


# ---------- 层1: 解析保真 ----------

def parse_layer(qa_items: list[dict]) -> dict:
    detail = {}
    for q in qa_items:
        sheet = q["source"]["sheet"].strip()
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM table_semantic_rows WHERE record_id=?",
                (rec_id_of(q["source"]["file"]),),
            ).fetchall()
        texts = [row_text(r) for r in rows if (r["sheet_name"] or "").strip() == sheet]
        groups = q["anchor_groups"]
        hit = sum(1 for g in groups if any(group_matches_row(g, t) for t in texts))
        detail[q["id"]] = {"groups_total": len(groups), "groups_hit": hit}
    return detail


# ---------- 层2: 检索召回(复刻工具 SQL 路径) ----------

def semantic_search(conn, query: str, top_k: int, rec_id: int | None = None) -> list:
    tokens = _tokens(query)
    columns = ["r.semantic_text", "r.raw_text"]
    where, where_params = _like_clauses(columns, tokens)
    relevance_order, order_params = _token_match_order(columns, tokens)
    params = [*where_params]
    record_clause = " AND r.record_id = ?" if rec_id else ""
    if rec_id:
        params.append(rec_id)
    params.extend(order_params)
    params.append(_candidate_limit(top_k))
    sql = f"""
        SELECT r.*, d.document_name, d.source_group
        FROM table_semantic_rows r
        JOIN table_documents d ON d.record_id = r.record_id
        WHERE {where}{record_clause}
        ORDER BY {relevance_order} DESC, r.confidence_score DESC, r.id ASC
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    return _rank_rows(rows, tokens, lambda r: f"{r['semantic_text'] or ''}\n{r['raw_text'] or ''}")[:top_k]


def cell_search(conn, query: str, top_k: int, rec_id: int | None = None) -> list:
    tokens = _tokens(query)
    columns = ["c.value", "c.raw_value", "c.header"]
    where, where_params = _like_clauses(columns, tokens)
    relevance_order, order_params = _token_match_order(columns, tokens)
    params = [*where_params]
    record_clause = " AND c.record_id = ?" if rec_id else ""
    if rec_id:
        params.append(rec_id)
    params.extend(order_params)
    params.append(_candidate_limit(top_k))
    sql = f"""
        SELECT c.*, d.document_name, d.source_group
        FROM table_cells c
        JOIN table_documents d ON d.record_id = c.record_id
        WHERE {where}{record_clause}
        ORDER BY {relevance_order} DESC, c.id ASC
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    return _rank_rows(rows, tokens, lambda r: f"{r['value'] or ''}\n{r['raw_value'] or ''}\n{r['header'] or ''}")[:top_k]


def retrieve_layer(qa_items: list[dict], ks=(5, 10)) -> dict:
    detail = {}
    for q in qa_items:
        sheet = q["source"]["sheet"].strip()
        rec_id = rec_id_of(q["source"]["file"])
        groups = q["anchor_groups"]
        per_query = {}
        for qname, query in (("question", q["question"]), ("keywords", q["query_keywords"])):
            with closing(connect()) as conn:
                sem = semantic_search(conn, query, max(ks), rec_id)
                cel = cell_search(conn, query, max(ks), rec_id)
            # 证据行文本(带 sheet 过滤: 只有目标 sheet 的证据可命中)
            sem_hits = []  # 按序记录每个 rank 命中的 group 索引
            for rank, r in enumerate(sem, 1):
                if (r["sheet_name"] or "").strip() != sheet:
                    continue
                t = row_text(r)
                sem_hits.extend((rank, gi) for gi, g in enumerate(groups) if group_matches_row(g, t))
            cel_hits = []
            for rank, r in enumerate(cel, 1):
                if (r["sheet_name"] or "").strip() != sheet:
                    continue
                t = " ".join(str(r[k] or "") for k in ("value", "raw_value", "header"))
                cel_hits.extend((rank, gi) for gi, g in enumerate(groups) if group_matches_row(g, t))
            per_group_rank = {}
            for gi in range(len(groups)):
                best = None
                for rank, hi in sem_hits + cel_hits:
                    if hi == gi and (best is None or rank < best):
                        best = rank
                per_group_rank[gi] = best
            per_query[qname] = {
                "group_ranks": per_group_rank,
                **{
                    f"recall@{k}": round(sum(1 for r in per_group_rank.values() if r and r <= k) / len(groups), 4)
                    for k in ks
                },
                "mrr": round(
                    sum(1.0 / r for r in per_group_rank.values() if r) / len(groups), 4
                ),
            }
        detail[q["id"]] = per_query
    return detail


def summarize(detail: dict, qa_items: list[dict]) -> dict:
    cats = {}
    for q in qa_items:
        d = detail[q["id"]]
        c = q["category"]
        for qname in ("question", "keywords"):
            slot = cats.setdefault(c, {"question": {"r5": [], "r10": [], "mrr": []},
                                       "keywords": {"r5": [], "r10": [], "mrr": []}})
            slot[qname]["r5"].append(d[qname]["recall@5"])
            slot[qname]["r10"].append(d[qname]["recall@10"])
            slot[qname]["mrr"].append(d[qname]["mrr"])
    out = {}
    for c, slot in cats.items():
        out[c] = {
            qname: {
                "recall@5": round(sum(v["r5"]) / len(v["r5"]), 4),
                "recall@10": round(sum(v["r10"]) / len(v["r10"]), 4),
                "mrr": round(sum(v["mrr"]) / len(v["mrr"]), 4),
            }
            for qname, v in slot.items()
        }
    all_ids = [q["id"] for q in qa_items]
    out["overall"] = {
        qname: {
            "recall@5": round(sum(detail[i][qname]["recall@5"] for i in all_ids) / len(all_ids), 4),
            "recall@10": round(sum(detail[i][qname]["recall@10"] for i in all_ids) / len(all_ids), 4),
            "mrr": round(sum(detail[i][qname]["mrr"] for i in all_ids) / len(all_ids), 4),
        }
        for qname in ("question", "keywords")
    }
    return out


def main():
    qa = json.load(open(EVAL_DIR / "golden_qa.json", encoding="utf-8"))
    items = qa["questions"]
    print("== ingesting ==")
    stats = ingest()
    for name, s in stats.items():
        print(f"  {name[:44]:46s} sheets={s['sheet_count']} cells={s['cell_count']} sem_rows={s['semantic_row_count']}")

    print("\n== parse layer ==")
    pd = parse_layer(items)
    for q in items:
        d = pd[q["id"]]
        flag = "OK " if d["groups_hit"] == d["groups_total"] else "MISS"
        print(f"  [{flag}] {q['id']:5s} {d['groups_hit']}/{d['groups_total']}  {q['question'][:44]}")
    total_g = sum(d["groups_total"] for d in pd.values())
    hit_g = sum(d["groups_hit"] for d in pd.values())
    print(f"  parse anchor coverage: {hit_g}/{total_g} = {hit_g/total_g:.2%}")

    print("\n== retrieval layer ==")
    rd = retrieve_layer(items)
    summary = summarize(rd, items)
    print(f"  {'category':16s} {'q-recall@5':>10s} {'q-recall@10':>11s} {'q-MRR':>7s} {'k-recall@5':>10s} {'k-recall@10':>11s} {'k-MRR':>7s}")
    for c in ["point_lookup", "filter", "comparison", "aggregation", "multi_condition", "overall"]:
        s = summary.get(c)
        if not s:
            continue
        qs, ks_ = s["question"], s["keywords"]
        print(f"  {c:16s} {qs['recall@5']:>10.2%} {qs['recall@10']:>11.2%} {qs['mrr']:>7.3f} "
              f"{ks_['recall@5']:>10.2%} {ks_['recall@10']:>11.2%} {ks_['mrr']:>7.3f}")

    report = {
        "ingest_stats": stats,
        "parse_layer": pd,
        "parse_anchor_coverage": {"hit": hit_g, "total": total_g},
        "retrieval_detail": rd,
        "retrieval_summary": summary,
        "golden_meta": qa["meta"],
    }
    json.dump(report, open(REPORT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nreport -> {REPORT_PATH}")


if __name__ == "__main__":
    main()
