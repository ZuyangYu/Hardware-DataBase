"""HTTP client + SSE parsing for the Hardware DataBase API."""
from __future__ import annotations

import json
import os
from collections.abc import Iterator

import httpx


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _extract_detail(body: str, status_code: int) -> str:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.strip() or f"HTTP {status_code}"
    if isinstance(data, dict) and "detail" in data:
        return str(data["detail"])
    return str(data)


def _iter_sse(response: httpx.Response) -> Iterator[tuple[str, dict]]:
    event: str | None = None
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                try:
                    data = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    data = {"raw": "\n".join(data_lines)}
                yield event or "message", data
            event = None
            data_lines = []
        elif line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())


class ApiClient:
    """Synchronous HTTP client. ``client`` lets tests inject an ASGI transport."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _raise(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            raise ApiError(_extract_detail(r.text, r.status_code), r.status_code)

    def login(self, username: str, password: str) -> dict:
        r = self._client.post(
            f"{self.base_url}/login",
            json={"username": username, "password": password},
        )
        self._raise(r)
        return r.json()

    def whoami(self) -> dict:
        r = self._client.get(f"{self.base_url}/whoami", headers=self._headers())
        self._raise(r)
        return r.json()

    def logout(self) -> dict:
        r = self._client.post(f"{self.base_url}/logout", headers=self._headers())
        self._raise(r)
        return r.json()

    def list_kbs(self) -> list:
        r = self._client.get(f"{self.base_url}/kbs", headers=self._headers())
        self._raise(r)
        return r.json()

    def list_files(self, kb: str) -> list:
        r = self._client.get(f"{self.base_url}/kbs/{kb}/files", headers=self._headers())
        self._raise(r)
        return r.json()

    def create_kb(self, name: str) -> dict:
        r = self._client.post(f"{self.base_url}/kbs", json={"name": name}, headers=self._headers())
        self._raise(r)
        return r.json()

    def delete_file(self, kb: str, filename: str) -> dict:
        r = self._client.delete(
            f"{self.base_url}/kbs/{kb}/files/{filename}", headers=self._headers()
        )
        self._raise(r)
        return r.json()

    def upload(self, kb: str, paths: list[str], source_group: str | None = None) -> dict:
        opened = []
        files = []
        try:
            for p in paths:
                f = open(p, "rb")
                opened.append(f)
                files.append(("files", (os.path.basename(p), f)))
            data = {"source_group": source_group} if source_group else None
            r = self._client.post(
                f"{self.base_url}/kbs/{kb}/files",
                files=files,
                data=data,
                headers=self._headers(),
            )
            self._raise(r)
            return r.json()
        finally:
            for f in opened:
                f.close()

    def query(self, kb: str, query_text: str, history: list | None = None) -> Iterator[tuple[str, dict]]:
        payload = {"kb_name": kb, "query": query_text, "history": history or []}
        with self._client.stream(
            "POST",
            f"{self.base_url}/query",
            json=payload,
            headers=self._headers(),
            timeout=None,
        ) as r:
            if r.status_code != 200:
                body = r.read().decode("utf-8", "ignore")
                raise ApiError(_extract_detail(body, r.status_code), r.status_code)
            yield from _iter_sse(r)
