#!/usr/bin/env python
"""Minimal External API client example (P0).

The key is read from the environment and is never printed.  This file is a
reference for an internal consumer (WCCA is optional); it is deliberately not
a general-purpose SDK.  Employees enable access by creating a Key in personal
settings; revoking the Key closes that access.

Usage:
    export HDB_BASE_URL=https://hdb.example.com
    export HDB_API_KEY=hdbk_...
    export HDB_KB_REF=kbref_...
    uv run python scripts/manual/external_api_client.py
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx


class ExternalApiError(RuntimeError):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload
        error = payload.get("error") if isinstance(payload, dict) else None
        code = str((error or {}).get("code") or "HTTP_ERROR")
        message = str((error or {}).get("message") or "External API request failed")
        super().__init__(f"{status_code} {code}: {message}")


class HDBClient:
    """Small synchronous client for the P0 External surface."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-HDB-API-Key": api_key},
            timeout=timeout,
        )

    def __enter__(self) -> "HDBClient":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {
                "error": {
                    "code": "INVALID_RESPONSE",
                    "message": response.text[:200],
                }
            }
        if response.is_error:
            raise ExternalApiError(response.status_code, payload)
        return payload

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/external/capabilities")

    def list_kbs(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/external/kbs")

    def list_files(
        self,
        kb_ref: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/external/kbs/{kb_ref}/files",
            params={"offset": offset, "limit": limit},
        )

    def get_file(self, kb_ref: str, file_ref: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/external/kbs/{kb_ref}/files/{file_ref}"
        )

    def retrieve(
        self,
        kb_ref: str,
        *,
        query: str,
        file_refs: list[str],
        top_k: int = 10,
        expected_content_hashes: dict[str, str] | None = None,
        source_scope_ref: str | None = None,
    ) -> dict[str, Any]:
        scope: dict[str, Any] = {"mode": "files", "file_refs": file_refs}
        if expected_content_hashes:
            scope["expected_content_hashes"] = expected_content_hashes
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "scope": scope,
            "capabilities": ["document_claim_lookup"],
        }
        if source_scope_ref:
            payload["source_scope_ref"] = source_scope_ref
        return self._request(
            "POST",
            f"/api/v1/external/kbs/{kb_ref}/retrieve",
            json=payload,
        )

    def list_chunks(
        self,
        kb_ref: str,
        file_ref: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/external/kbs/{kb_ref}/files/{file_ref}/chunks",
            params={"offset": offset, "limit": limit},
        )

    def download_file(self, kb_ref: str, file_ref: str) -> bytes:
        response = self._client.get(
            f"/api/v1/external/kbs/{kb_ref}/files/{file_ref}/download"
        )
        if response.is_error:
            try:
                payload = response.json()
            except ValueError:
                payload = {
                    "error": {
                        "code": "INVALID_RESPONSE",
                        "message": response.text[:200],
                    }
                }
            raise ExternalApiError(response.status_code, payload)
        return response.content

    def upload_files(
        self,
        kb_ref: str,
        files: list[tuple[str, bytes]],
        *,
        source_group: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        multipart = [
            ("files", (name, content, "application/octet-stream"))
            for name, content in files
        ]
        data = {"source_group": source_group} if source_group else {}
        return self._request(
            "POST",
            f"/api/v1/external/kbs/{kb_ref}/files",
            headers={"Idempotency-Key": idempotency_key or uuid.uuid4().hex},
            files=multipart,
            data=data,
        )

    def get_ingestion_job(self, job_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/external/ingestion-jobs/{job_id}"
        )

    def list_assets(
        self,
        kb_ref: str,
        *,
        query: str = "",
        category: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/external/kbs/{kb_ref}/assets",
            params={
                "query": query,
                "category": category,
                "offset": offset,
                "limit": limit,
            },
        )

    def get_asset(self, kb_ref: str, asset_ref: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/external/kbs/{kb_ref}/assets/{asset_ref}"
        )

    def add_asset_version(
        self,
        kb_ref: str,
        asset_ref: str,
        *,
        file_ref: str,
        note: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/external/kbs/{kb_ref}/assets/{asset_ref}/versions",
            json={"file_ref": file_ref, "note": note},
        )

    def list_table_schema(
        self, kb_ref: str, *, file_ref: str = ""
    ) -> dict[str, Any]:
        params = {"file_ref": file_ref} if file_ref else {}
        return self._request(
            "GET",
            f"/api/v1/external/kbs/{kb_ref}/tables/schema",
            params=params,
        )

    def query_tables(
        self, kb_ref: str, sql: str, *, max_rows: int = 200
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/external/kbs/{kb_ref}/tables/query",
            json={"sql": sql, "max_rows": max_rows},
        )

    def query_circuits(
        self,
        kb_ref: str,
        *,
        kind: str = "modules",
        query: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/external/kbs/{kb_ref}/circuits/query",
            json={"kind": kind, "query": query, "limit": limit},
        )

    def batch_retrieve(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/external/evidence/batch-retrieve",
            json={"items": items},
        )

    def create_source_scope(
        self,
        kb_ref: str,
        file_refs: list[str],
        *,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kb_ref": kb_ref,
            "file_refs": file_refs,
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        return self._request(
            "POST", "/api/v1/external/source-scopes", json=payload
        )

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/external/evidence/{evidence_id}"
        )

    def validate_evidence(
        self,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/external/evidence/validate",
            json={"items": items},
        )


def main() -> None:
    base_url = os.environ["HDB_BASE_URL"]
    api_key = os.environ["HDB_API_KEY"]
    kb_ref = os.environ["HDB_KB_REF"]
    with HDBClient(base_url, api_key) as client:
        print(json.dumps(client.capabilities(), ensure_ascii=False, indent=2))
        files = client.list_files(kb_ref)
        print(json.dumps(files, ensure_ascii=False, indent=2))
        if files.get("items"):
            first = files["items"][0]
            result = client.retrieve(
                kb_ref,
                query="absolute maximum rating",
                file_refs=[first["file_ref"]],
                expected_content_hashes=(
                    {first["file_ref"]: first["content_hash"]}
                    if first.get("content_hash")
                    else None
                ),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
