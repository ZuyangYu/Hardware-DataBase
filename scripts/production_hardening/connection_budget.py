"""Report the control-plane connection budget against the live PostgreSQL.

Single-host capacity work (v3 §9.5): sums the worst-case connections of every
API/worker process class plus unpooled callers, then compares it with
``max_connections`` and the current ``pg_stat_activity`` count.

Example::

    python scripts/production_hardening/connection_budget.py \
        --dsn postgresql://user:pass@127.0.0.1:5432/hdb \
        --api-replicas 1 --worker-replicas 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.settings as settings  # noqa: E402
from src.storage.control_plane.budget import (  # noqa: E402
    budget_fits,
    settings_connection_budget,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default="", help="control-plane PostgreSQL connection URL")
    parser.add_argument("--api-replicas", type=int, default=1)
    parser.add_argument("--worker-replicas", type=int, default=1)
    parser.add_argument("--reserve", type=int, default=20, help="migration/ops headroom")
    args = parser.parse_args(argv)

    budget = settings_connection_budget(
        settings,
        api_replicas=max(0, args.api_replicas),
        worker_replicas=max(0, args.worker_replicas),
        reserve=max(0, args.reserve),
    )
    payload: dict[str, object] = {"budget": budget.to_dict()}

    if args.dsn:
        import psycopg

        with psycopg.connect(args.dsn, autocommit=True) as conn:
            max_connections = int(
                conn.execute("SHOW max_connections").fetchone()[0]
            )
            current = int(
                conn.execute("SELECT count(*) FROM pg_stat_activity").fetchone()[0]
            )
        fits, message = budget_fits(budget, max_connections=max_connections)
        payload.update(
            {
                "max_connections": max_connections,
                "current_connections": current,
                "fits": fits,
                "message": message,
            }
        )

    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if args.dsn and not payload["fits"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
