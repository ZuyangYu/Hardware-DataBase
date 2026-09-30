#!/usr/bin/env python3
"""Fixed, reviewed report bundle (standard library only).

Contract:
- reads ``/work/in/parameters.json`` written by the Sandbox Runner;
- writes ``report.txt`` and ``report.json`` into ``/work/out``;
- deterministic for the same parameters; no network, no environment secrets.

The Runner enforces the reviewed digest of this file; changing it requires a
new bundle registration and a new digest on the HDB side.
"""

from __future__ import annotations

import json
import os

PARAMETERS = "/work/in/parameters.json"
OUTPUT_DIR = "/work/out"


def main() -> None:
    with open(PARAMETERS, encoding="utf-8") as handle:
        parameters = json.load(handle)
    title = str(parameters.get("title", "report"))[:80]
    rows = max(1, min(int(parameters.get("rows", 3)), 20))

    lines = [f"# {title}", "", f"rows={rows}", ""]
    for index in range(rows):
        lines.append(f"- row {index + 1}: value={index * 7}")
    text = "\n".join(lines) + "\n"

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "report.txt"), "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(os.path.join(OUTPUT_DIR, "report.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {"title": title, "rows": rows, "lines": len(lines)},
            handle,
            ensure_ascii=False,
            sort_keys=True,
        )
    print("bundle ok")


if __name__ == "__main__":
    main()
