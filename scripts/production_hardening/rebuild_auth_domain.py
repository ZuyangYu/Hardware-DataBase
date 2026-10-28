#!/usr/bin/env python
"""把认证域（账号 + 审计）的表在 PostgreSQL 上按机械形状重建并灌入数据。

背景：``users`` / ``departments`` / ``audit_events`` 等在早期切换控制面时
按"语义类型"建过（TIMESTAMPTZ / BOOLEAN），而 ``AuthService`` /
``AppLogService`` 说的是 SQLite 方言（文本时间戳、0/1 标志）。直接切
``AUTH_STORE_BACKEND=postgres`` 会登不进去，必须先用本脚本重建。

步骤：DROP ... CASCADE → 机械建表（含索引/外键）→ 从 ``storage/auth.db``
复制行 → 重新挂回引用这些表的控制面外键（如 ``fk_chat_sessions_user``）。

**先停写**（``bash scripts/hdb.sh stop``），否则源库在复制过程中变化会漏行。
本脚本只读 SQLite、只写 PostgreSQL；不动源文件。

用法：
    bash scripts/hdb.sh stop
    uv run python scripts/production_hardening/rebuild_auth_domain.py \
        --dsn "$CONTROL_PLANE_DATABASE_URL"
    # 在 .env 设 AUTH_STORE_BACKEND=postgres 后：bash scripts/hdb.sh start
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.storage import domain_pg  # noqa: E402
from src.storage.auth_domain import TABLES, domain_classes  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="目标 PostgreSQL 连接串")
    parser.add_argument(
        "--domain",
        choices=("auth",),
        default="auth",
        help="重建哪一组（默认 auth：账号 + 审计 + 治理杂项）",
    )
    parser.add_argument(
        "--source",
        default="",
        help="源 SQLite（默认按 domain 取 storage/ 下的对应文件）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告将要做什么")
    args = parser.parse_args(argv)

    tables = TABLES
    classes = domain_classes()
    default_source = REPO_ROOT / "storage" / "auth.db"
    source = Path(args.source) if args.source else default_source
    if not source.is_file():
        print(f"[rebuild-auth] 找不到源库: {source}", file=sys.stderr)
        return 1

    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        available = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        present = [table for table in tables if table in available]
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in present
        }
    missing = [table for table in tables if table not in available]
    if missing:
        print(f"[rebuild-auth] 源库缺少表（跳过，不阻断）: {missing}", file=sys.stderr)
    tables = tuple(present)

    print(f"[rebuild-auth] 域={args.domain} 将重建以下表（源行数）：")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    if args.dry_run:
        print("[rebuild-auth] --dry-run：未做任何修改")
        return 0

    copied = domain_pg.rebuild_domain_group(
        args.dsn,
        classes,
        source_sqlite_path=str(source),
        tables=tables,
    )
    print("[rebuild-auth] 重建完成（PG 行数）：")
    ok = True
    for table, count in copied.items():
        expected = counts.get(table, 0)
        flag = "✓" if count == expected else "⚠ 行数不一致"
        if count != expected:
            ok = False
        print(f"  {table}: {count} {flag}")
    print(
        "[rebuild-auth] 下一步：在 .env 里设 AUTH_STORE_BACKEND=postgres，"
        "然后 bash scripts/hdb.sh start"
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
