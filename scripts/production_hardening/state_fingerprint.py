"""Emit row-count and file-hash fingerprints for migration validation.

Used before and after every state migration step.  Databases are opened
read-only; no write, VACUUM or checkpoint is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE = REPO_ROOT / "storage"

DEFAULT_DATABASES: tuple[str, ...] = (
    "auth.db",
    "agent_checkpoints.db",
    "memory.db",
    "chat_attachments.db",
    "pipeline_documents.db",
)

DEFAULT_DIRECTORIES: tuple[str, ...] = (
    "chat_attachments",
    "pipeline_archives",
    "exports",
    "circuits",
    "table_indexes",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_counts(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    uri = f"file:{path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (name,) in rows:
            quoted = '"' + str(name).replace('"', '""') + '"'
            try:
                value = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()
                counts[str(name)] = int(value[0]) if value else 0
            except sqlite3.DatabaseError:
                counts[str(name)] = -1
    finally:
        connection.close()
    return counts


def _directory_fingerprint(path: Path) -> dict[str, object]:
    files = 0
    total_bytes = 0
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        files += 1
        total_bytes += item.stat().st_size
        digest.update(str(item.relative_to(path)).encode("utf-8", "surrogateescape"))
        digest.update(_sha256(item).encode("ascii"))
    return {"files": files, "bytes": total_bytes, "digest": digest.hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="fingerprint label, e.g. p1-pre-migration")
    parser.add_argument("--storage", default=str(STORAGE))
    parser.add_argument("--database", action="append", dest="databases", help="override database list (repeatable)")
    parser.add_argument("--directory", action="append", dest="directories", help="override directory list (repeatable)")
    parser.add_argument("--output", default="", help="output JSON path; defaults to stdout")
    args = parser.parse_args(argv)

    storage = Path(args.storage)
    databases = args.databases or list(DEFAULT_DATABASES)
    directories = args.directories or list(DEFAULT_DIRECTORIES)

    payload: dict[str, object] = {
        "label": args.label,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "storage": str(storage),
        "databases": {},
        "directories": {},
    }
    for name in databases:
        path = storage / name
        if not path.is_file():
            payload["databases"][name] = {"present": False}
            continue
        payload["databases"][name] = {
            "present": True,
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
            "tables": _table_counts(path),
        }
    for name in directories:
        path = storage / name
        payload["directories"][name] = (
            {"present": True, **_directory_fingerprint(path)} if path.is_dir() else {"present": False}
        )

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"fingerprint={output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
