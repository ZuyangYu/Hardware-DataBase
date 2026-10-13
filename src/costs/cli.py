from __future__ import annotations

import argparse
import json
from pathlib import Path

import src.settings as settings
from .pricing import PriceVersion
from .store import CostStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Register immutable model prices or replay retained cost spool")
    parser.add_argument("--price-file", type=Path)
    parser.add_argument("--replay-spool", action="store_true", help="Run with application consumers stopped; preserves bad files")
    parser.add_argument("--spool-directory", type=Path, help="Explicit source directory for legacy spool or a changed storage destination")
    parser.add_argument("--registry-key", nargs=3, metavar=("PROVIDER", "ENDPOINT", "MODEL"), help="Print a configuration lookup key; endpoint is not stored")
    args = parser.parse_args(argv)
    if args.registry_key:
        from src.quality.store import sha256

        print(sha256(args.registry_key))
        return 0
    store = CostStore()
    if args.price_file:
        store.add_price(PriceVersion.model_validate_json(args.price_file.read_text()))
    if args.replay_spool:
        from .sink import spool_root

        count = 0
        root = args.spool_directory or spool_root(settings.CONTROL_PLANE_BACKEND, settings.CONTROL_PLANE_DATABASE_URL, settings.STORAGE_DIR)
        for path in sorted(root.rglob("*.json")):
            store.ingest(json.loads(path.read_text()))
            path.unlink()
            count += 1
        print(json.dumps({"replayed": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
