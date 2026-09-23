#!/usr/bin/env python3
"""Fixed slow bundle used to validate timeout/cancel/reconcile behaviour."""

from __future__ import annotations

import json
import os
import time

PARAMETERS = "/work/in/parameters.json"
OUTPUT_DIR = "/work/out"


def main() -> None:
    with open(PARAMETERS, encoding="utf-8") as handle:
        parameters = json.load(handle)
    seconds = max(1, min(int(parameters.get("seconds", 30)), 120))
    time.sleep(seconds)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "done.txt"), "w", encoding="utf-8") as handle:
        handle.write("done\n")


if __name__ == "__main__":
    main()
