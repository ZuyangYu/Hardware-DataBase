#!/usr/bin/env python3
"""Resolve the acceptance evidence through the live citation API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def _locator(evidence: dict) -> dict:
    metadata = evidence.get("metadata") or {}
    locator = metadata.get("locator") or metadata.get("chunk_or_structured_locator")
    if isinstance(locator, str):
        return {"chunk_id": locator}
    return dict(locator or {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-report", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5176")
    args = parser.parse_args(argv)

    report = json.loads(Path(args.evidence_report).read_text())
    auth = json.loads(Path(args.auth_file).read_text())
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checks = []
    seen = set()
    with httpx.Client(base_url=args.base_url, headers={"Authorization": f"Bearer {auth['token']}"}, timeout=120) as client:
        for case in report.get("cases", []):
            for evidence in case.get("evidence", []):
                metadata = evidence.get("metadata") or {}
                locator = _locator(evidence)
                source_name = str(evidence.get("source_name") or "")
                is_doc = source_name.endswith(".docx")
                is_row = source_name.endswith(".xlsx") and locator.get("sheet_name") and locator.get("row_index") is not None
                if not (is_doc or is_row):
                    continue
                key = (metadata.get("kb_id"), source_name, json.dumps(locator, sort_keys=True, ensure_ascii=False))
                if key in seen:
                    continue
                seen.add(key)
                body = {
                    "kb_id": metadata["kb_id"],
                    "local_record_id": metadata["local_record_id"],
                    "source_content_hash": metadata["source_content_hash"],
                    "index_revision": metadata.get("index_revision"),
                    "processing_artifact_id": metadata.get("processing_artifact_id"),
                    "processor_version": metadata.get("processor_version"),
                    "evidence_text_hash": metadata.get("evidence_text_hash"),
                    "locator": locator,
                }
                response = client.post("/api/v1/citations/resolve", json=body)
                payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                matches = response.is_success and payload.get("content") == evidence.get("content")
                checks.append({
                    "case_id": case.get("id"), "kb_id": metadata.get("kb_id"),
                    "source_name": source_name, "locator": locator,
                    "status_code": response.status_code,
                    "matches_retrieved_content": matches,
                    **({"error": payload} if not response.is_success else {}),
                })
                print("READBACK", case.get("id"), source_name, response.status_code, matches, flush=True)

    doc_count = sum(1 for check in checks if check["source_name"].endswith(".docx"))
    row_count = sum(1 for check in checks if check["source_name"].endswith(".xlsx"))
    passed = all(check["matches_retrieved_content"] for check in checks) and doc_count >= 5 and row_count >= 3
    result = {"checks": checks, "document_count": doc_count, "icd_row_count": row_count, "passed": passed}
    path = output_dir / "citation-readback-report.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print("CITATION_SUMMARY", doc_count, "docs", row_count, "rows", "passed", passed, flush=True)
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
