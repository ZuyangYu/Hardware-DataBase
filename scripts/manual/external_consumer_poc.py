#!/usr/bin/env python
"""Run a generic External Consumer POC against the External API.

This is intentionally consumer-oriented: it does not import HDB internals.  It
is the required acceptance path when no WCCA workflow is available; a future
WCCA adapter can reuse the same request sequence.

Required environment:
    HDB_BASE_URL   e.g. http://127.0.0.1:8001
    HDB_API_KEY    employee API key
    HDB_KB_REF     kb_ref returned by /me/api-keys/available-kbs

Optional:
    HDB_FILE_REF   pin a specific file; otherwise the first discovered file is used
    HDB_QUERY      retrieval query (default: "absolute maximum rating")

The API key is never printed.  Output is a compact JSON report and a non-zero
exit code only for required checks that fail.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from external_api_client import ExternalApiError, HDBClient


def _result(name: str, *, ok: bool, required: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "required": required, "detail": detail}


def _find_file(files: dict[str, Any], wanted: str) -> dict[str, Any] | None:
    items = [item for item in files.get("items", []) if isinstance(item, dict)]
    if wanted:
        return next((item for item in items if item.get("file_ref") == wanted), None)
    return next((item for item in items if item.get("file_ref")), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="只输出 JSON 报告")
    args = parser.parse_args(argv)

    base_url = os.environ["HDB_BASE_URL"]
    api_key = os.environ["HDB_API_KEY"]
    kb_ref = os.environ["HDB_KB_REF"]
    wanted_file = os.environ.get("HDB_FILE_REF", "")
    query = os.environ.get("HDB_QUERY", "absolute maximum rating")

    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {"consumer": "external-consumer-poc", "kb_ref": kb_ref}
    with HDBClient(base_url, api_key) as client:
        try:
            capabilities = client.capabilities()
            names = set(capabilities.get("capabilities") or [])
            checks.append(
                _result(
                    "capabilities",
                    ok="document_claim_lookup" in names,
                    required=True,
                    detail=",".join(sorted(names)),
                )
            )

            kbs = client.list_kbs()
            checks.append(
                _result("list_kbs", ok=isinstance(kbs, list), required=True, detail=f"{len(kbs)} kbs")
            )

            files = client.list_files(kb_ref)
            selected = _find_file(files, wanted_file)
            checks.append(
                _result(
                    "list_files",
                    ok=selected is not None,
                    required=True,
                    detail=f"{len(files.get('items', []))} files",
                )
            )
            if selected is None:
                raise RuntimeError("no file_ref available in the selected KB")

            file_ref = str(selected["file_ref"])
            content_hash = str(selected.get("content_hash") or "")
            report["file_ref"] = file_ref

            try:
                chunks = client.list_chunks(kb_ref, file_ref, limit=5)
                checks.append(
                    _result(
                        "chunks",
                        ok=isinstance(chunks.get("items"), list),
                        required=False,
                        detail=f"{len(chunks.get('items', []))} chunks",
                    )
                )
            except ExternalApiError as exc:
                checks.append(
                    _result("chunks", ok=False, required=False, detail=exc.payload.get("error", {}).get("code", str(exc)))
                )

            if selected.get("download_available"):
                try:
                    content = client.download_file(kb_ref, file_ref)
                    checks.append(
                        _result(
                            "download",
                            ok=bool(content),
                            required=False,
                            detail=f"{len(content)} bytes",
                        )
                    )
                except ExternalApiError as exc:
                    checks.append(
                        _result(
                            "download",
                            ok=False,
                            required=False,
                            detail=exc.payload.get("error", {}).get("code", str(exc)),
                        )
                    )

            scope: dict[str, Any] = {"mode": "files", "file_refs": [file_ref]}
            if content_hash:
                scope["expected_content_hashes"] = {file_ref: content_hash}
            retrieved = client.retrieve(
                kb_ref,
                query=query,
                file_refs=[file_ref],
                expected_content_hashes=scope["expected_content_hashes"],
            )
            checks.append(
                _result(
                    "retrieve",
                    ok=retrieved.get("status") in {"completed", "completed_no_hits"},
                    required=True,
                    detail=f"status={retrieved.get('status')} hits={len(retrieved.get('results', []))}",
                )
            )
            if retrieved.get("results"):
                first = retrieved["results"][0]
                checks.append(
                    _result(
                        "evidence_has_source",
                        ok=bool(
                            first.get("evidence_id")
                            and first.get("source", {}).get("file_ref") == file_ref
                        ),
                        required=True,
                        detail="evidence_id/source file_ref present",
                    )
                )
                if "source_scope" in names:
                    try:
                        source_scope = client.create_source_scope(
                            kb_ref, [file_ref], ttl_seconds=300
                        )
                        scoped = client.retrieve(
                            kb_ref,
                            query=query,
                            file_refs=[file_ref],
                            expected_content_hashes=scope.get(
                                "expected_content_hashes"
                            ),
                            source_scope_ref=source_scope["scope_ref"],
                        )
                        scoped_evidence = (scoped.get("results") or [{}])[0]
                        evidence_id = str(scoped_evidence.get("evidence_id") or "")
                        stored = client.get_evidence(evidence_id) if evidence_id else {}
                        checks.append(
                            _result(
                                "source_scope_evidence",
                                ok=bool(
                                    stored.get("evidence_id") == evidence_id
                                    and stored.get("source", {}).get("file_ref")
                                    == file_ref
                                ),
                                required=False,
                                detail=evidence_id,
                            )
                        )
                        if evidence_id and "evidence_validation" in names:
                            validation = client.validate_evidence(
                                [{"evidence_id": evidence_id}]
                            )
                            status = (validation.get("items") or [{}])[0].get(
                                "status"
                            )
                            checks.append(
                                _result(
                                    "evidence_validation",
                                    ok=status == "supported",
                                    required=False,
                                    detail=str(status),
                                )
                            )
                    except ExternalApiError as exc:
                        checks.append(
                            _result(
                                "source_scope_evidence",
                                ok=False,
                                required=False,
                                detail=exc.payload.get("error", {}).get(
                                    "code", str(exc)
                                ),
                            )
                        )

            batch = client.batch_retrieve(
                [
                    {
                        "kb_ref": kb_ref,
                        "query": query,
                        "scope": scope,
                    },
                    {
                        "kb_ref": kb_ref,
                        "query": query,
                        "scope": {"mode": "files", "file_refs": ["fileref_missing"]},
                    },
                ]
            )
            batch_items = batch.get("items") or []
            checks.append(
                _result(
                    "batch_isolates_failures",
                    ok=(
                        len(batch_items) == 2
                        and batch_items[0].get("status") in {"completed", "completed_no_hits"}
                        and batch_items[1].get("status") == "failed"
                    ),
                    required=True,
                    detail=f"status={batch.get('status')}",
                )
            )

            if "table_schema" in names:
                try:
                    tables = client.list_table_schema(kb_ref)
                    table_items = tables.get("tables") or []
                    checks.append(
                        _result(
                            "table_schema_optional",
                            ok=True,
                            required=False,
                            detail=f"{len(table_items)} tables",
                        )
                    )
                    if table_items:
                        table = table_items[0]
                        columns = table.get("columns") or []
                        if columns:
                            sql = f"SELECT {columns[0]['key']} FROM {table['table_ref']} LIMIT 1"
                            queried = client.query_tables(kb_ref, sql)
                            checks.append(
                                _result(
                                    "table_query_optional",
                                    ok=queried.get("row_count", 0) <= 1,
                                    required=False,
                                    detail=f"{queried.get('row_count', 0)} rows",
                                )
                            )
                except ExternalApiError as exc:
                    checks.append(
                        _result(
                            "table_schema_optional",
                            ok=False,
                            required=False,
                            detail=exc.payload.get("error", {}).get("code", str(exc)),
                        )
                    )

            if "circuit_query" in names:
                try:
                    circuits = client.query_circuits(kb_ref, kind="modules", query="", limit=3)
                    checks.append(
                        _result(
                            "circuit_query_optional",
                            ok=isinstance(circuits.get("rows"), list),
                            required=False,
                            detail=f"{circuits.get('row_count', 0)} rows",
                        )
                    )
                except ExternalApiError as exc:
                    checks.append(
                        _result(
                            "circuit_query_optional",
                            ok=False,
                            required=False,
                            detail=exc.payload.get("error", {}).get("code", str(exc)),
                        )
                    )
        except (ExternalApiError, RuntimeError) as exc:
            checks.append(_result("run", ok=False, required=True, detail=str(exc)))

    report["checks"] = checks
    report["ok"] = all(item["ok"] for item in checks if item["required"])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            marker = "PASS" if item["ok"] else ("FAIL" if item["required"] else "SKIP")
            print(f"[{marker}] {item['name']}: {item['detail']}")
        print(f"\nRequired checks: {'PASS' if report['ok'] else 'FAIL'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
