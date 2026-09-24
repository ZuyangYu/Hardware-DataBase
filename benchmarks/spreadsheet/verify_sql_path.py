"""SQL 路径可答性验证: 对 golden QA 中代表性问题手写参考 SQL, 走真实校验+执行链路."""
import sqlite3
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from src.agents.tools.spreadsheet_tools import (
    _execute_readonly_sql, _load_sql_registry, _validate_readonly_sql,
)

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
DB = str(ROOT / "storage" / "eval" / "spreadsheet" / "aaa_eval" / "table_indexes.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 动态找列: 架构表的 HW-REQ 追溯列 / 需求表的编号与网络安全列
def col_of(table, keyword):
    r = conn.execute("SELECT schema_json FROM sql_table_registry WHERE table_name=?", (table,)).fetchone()
    for c in __import__("json").loads(r["schema_json"])["columns"]:
        if keyword in c["header"]:
            return c["column"]
    raise KeyError((table, keyword))

ARCH_REQ = col_of("t_1_4", "硬件需求编号")
REQ_ID = col_of("t_5_6", "编号\nID")
REQ_CYBER = col_of("t_5_6", "网络安全")
print(f"动态列: 架构追溯={ARCH_REQ} 需求编号={REQ_ID} 需求网络安全={REQ_CYBER}\n")

# (问题ID, 描述, SQL, 校验函数)
CASES = [
    ("AG01", "GND-PGND 并联连接数", "SELECT COUNT(*) AS n, COUNT(DISTINCT col_2) AS parts FROM t_4_5 WHERE col_4='GND' AND col_5='PGND'",
     lambda rows, err: not err and rows[0]["n"] == 23 and rows[0]["parts"] == 9),
    ("FL02", "GND-PGND 电阻清单", "SELECT GROUP_CONCAT(col_2) AS parts FROM t_4_5 WHERE col_4='GND' AND col_5='PGND'",
     lambda rows, err: not err and len(rows[0]["parts"].split(",")) == 23 and set(rows[0]["parts"].split(",")) == {"R1921","R1917","R1912","R1914","R1920","R1919","R1918","R1907","R1906"}),
    ("FL03", "VCC3V3 100nF 电容", "SELECT GROUP_CONCAT(col_2) AS parts FROM t_4_5 WHERE col_4='VCC3V3' AND col_5='GND' AND col_6='100nF'",
     lambda rows, err: not err and len(rows[0]["parts"].split(",")) == 10),
    ("MC02", "VCC3V3 容值>=10uF", "SELECT GROUP_CONCAT(col_2) AS parts FROM t_4_5 WHERE col_4='VCC3V3' AND col_6 IN ('22uF','47uF','10uF')",
     lambda rows, err: not err and len(rows[0]["parts"].split(",")) == 10),
    ("FL04", "标准件2 拆除项", "SELECT GROUP_CONCAT(col_2) AS parts FROM t_7_5 WHERE col_5='标准件2'",
     lambda rows, err: not err and "L1200" in rows[0]["parts"] and "FPC" in rows[0]["parts"] and rows[0]["parts"].count(",") == 5),
    ("AG04", "标准件4/5 拆除项", "SELECT col_5, GROUP_CONCAT(col_2) AS parts FROM t_7_5 WHERE col_5 IN ('标准件4','标准件5') GROUP BY col_5",
     lambda rows, err: not err and {r["col_5"]: r["parts"] for r in rows} == {"标准件4": "拆除R538", "标准件5": "拆除R1000"}),
    ("MC01", "CAN 无法发送报文", "SELECT GROUP_CONCAT(col_2) AS parts FROM t_7_5 WHERE col_3 LIKE '%CAN无法发送报文%'",
     lambda rows, err: not err and rows[0]["parts"].count(",") == 3 and "拆除L1200" in rows[0]["parts"]),
    ("FL06", "Polarity=1 器件类型", "SELECT GROUP_CONCAT(col_1) AS types FROM t_4_4 WHERE col_5='1'",
     lambda rows, err: not err and len(rows[0]["types"].split(",")) == 8 and "QFN" in rows[0]["types"]),
    ("CP01", "AOI vs AXI Missing", "SELECT col_1 AS tester, col_2 AS missing FROM t_4_6 WHERE col_1 IN ('AOI Coverage','AXI Coverage')",
     lambda rows, err: not err and all("100.00" in r["missing"] for r in rows if "AXI" in r["tester"]) and any("99.82" in r["missing"] for r in rows if "AOI" in r["tester"])),
    ("CP02", "ICT vs EOL Polarity", "SELECT col_1 AS tester, col_5 AS polarity FROM t_4_6 WHERE col_1 IN ('ICT Coverage','EOL Coverage')",
     lambda rows, err: not err and len(rows) == 2),
    ("CP03", "Wrong 覆盖率最低", "SELECT col_1 AS tester, col_6 AS wrong FROM t_4_6 WHERE col_1 LIKE '%Coverage'",
     lambda rows, err: not err and min(rows, key=lambda r: float(r["wrong"].split("(")[1].rstrip(")%"))) ["tester"] == "AXI Coverage"),
    ("CP04", "C1618 vs C1700", "SELECT col_2 AS part, col_6 AS val FROM t_4_5 WHERE col_2 IN ('C1618','C1700')",
     lambda rows, err: not err and {r["part"]: r["val"] for r in rows} == {"C1618": "22uF", "C1700": "10uF"}),
    ("PL06", "HW-SOL-004 ASIL", f"SELECT {col_of('t_1_4', 'ASIL')} AS asil FROM t_1_4 WHERE {col_of('t_1_4', '方案编号')}='HW-SOL-004'",
     lambda rows, err: not err and rows and "ASIL B" in rows[0]["asil"]),
    ("AG05", "HW-REQ-2 方案编号", f"SELECT {col_of('t_1_4', '方案编号')} AS sol FROM t_1_4 WHERE '|' || REPLACE({ARCH_REQ}, char(10), '|') || '|' LIKE '%|HW-REQ-2|%'",
     lambda rows, err: not err and rows and any("HW-SOL-009" in r["sol"] for r in rows)),
    ("FL07", "HW-REQ-2..7 网络安全", f"SELECT GROUP_CONCAT(DISTINCT {REQ_ID}) AS ids FROM t_5_6 WHERE {REQ_CYBER} LIKE '%是%' AND CAST(REPLACE({REQ_ID},'HW-REQ-','') AS INTEGER) BETWEEN 2 AND 7",
     lambda rows, err: not err and sorted(rows[0]["ids"].split(",")) == [f"HW-REQ-{i}" for i in range(2, 8)]),
    ("FL05", "Memory 类别子项(层级)", "SELECT COUNT(*) AS n FROM t_6_5 WHERE row_index > (SELECT row_index FROM t_6_5 WHERE col_2='Memory' LIMIT 1) AND row_index < (SELECT row_index FROM t_6_5 WHERE col_2='eSE' LIMIT 1)",
     lambda rows, err: not err and rows[0]["n"] == 7),
    ("AG02", "power 子项个数(层级)", "SELECT COUNT(*) AS n FROM t_6_5 WHERE row_index > (SELECT row_index FROM t_6_5 WHERE col_2='power' LIMIT 1) AND row_index < (SELECT row_index FROM t_6_5 WHERE col_2 LIKE '电性能%' LIMIT 1)",
     lambda rows, err: not err and rows[0]["n"] == 8),
    ("PL05", "MCU 20MHz 测试点", "SELECT col_4 AS tp FROM t_6_12 WHERE col_3='MCU 20MHz'",
     lambda rows, err: not err and rows and rows[0]["tp"] == "Y900"),
]

passed = 0
for qid, desc, sql, check in CASES:
    registry = _load_sql_registry(DB)
    allowed = {e["table_name"] for e in registry}
    ast, verror = _validate_readonly_sql(sql, allowed)
    if verror:
        print(f"[FAIL] {qid} {desc}: 校验失败 {verror}")
        continue
    rows, eerror = _execute_readonly_sql(DB, ast.sql(dialect="sqlite"))
    try:
        ok = check(rows, eerror)
    except Exception as e:
        ok = False
        eerror = f"check 异常: {e}, rows={rows}"
    if ok:
        passed += 1
        print(f"[PASS] {qid} {desc}")
    else:
        print(f"[FAIL] {qid} {desc}: err={eerror} rows={rows}")
print(f"\n{passed}/{len(CASES)} 通过")
