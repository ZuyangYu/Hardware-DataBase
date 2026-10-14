"""Phoenix 19.13 REST projection: stable identifier and synchronous ACK.

Only controlled labels/scores and correlation IDs cross this boundary. Raw
feedback, answers, source titles, reference answers and review notes stay in
the authorized quality store.
"""
from __future__ import annotations

import re
from urllib.parse import quote

import httpx

import src.settings as settings


class PhoenixRetry(RuntimeError):
    pass


class PhoenixRejected(RuntimeError):
    pass


class PhoenixAnnotationClient:
    def __init__(self, *, endpoint: str | None = None, api_key: str | None = None, project: str | None = None, transport=None):
        self.endpoint = (endpoint if endpoint is not None else settings.QUALITY_PHOENIX_ENDPOINT).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.QUALITY_PHOENIX_API_KEY
        self.project = project if project is not None else settings.OBS_PHOENIX_PROJECT
        self.transport = transport

    def sync(self, span_id: str, payload: dict) -> None:
        if not re.fullmatch(r"[a-f0-9]{16}", span_id) or int(span_id, 16) == 0:
            raise PhoenixRejected("invalid result span id")
        result = {"label": payload["label"]}
        if payload.get("score") is not None:
            result["score"] = payload["score"]
        allowed_metadata = {k: v for k, v in payload.get("metadata", {}).items() if k in {"case_id", "event_id", "review_id", "revision", "reason_codes", "root_cause", "judgment"}}
        annotation = {"span_id": span_id, "name": payload["name"], "annotator_kind": payload["annotator_kind"],
                      "identifier": payload["identifier"], "result": result, "metadata": allowed_metadata}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        with httpx.Client(base_url=self.endpoint, headers=headers, timeout=5, transport=self.transport) as client:
            try:
                response = client.post("/v1/span_annotations", params={"sync": "true"}, json={"data": [annotation]})
                self._check(response)
                # A synchronous write is additionally verified by identifier in
                # the configured project; HTTP 200 alone is not an applied ACK.
                response = client.get(f"/v1/projects/{quote(self.project, safe='')}/span_annotations",
                                      params={"span_ids": span_id, "identifier": payload["identifier"], "limit": 100})
                self._check(response)
                rows = response.json().get("data", [])
                matching = [row for row in rows if row.get("span_id") == span_id and row.get("identifier") == payload["identifier"] and row.get("name") == payload["name"] and (row.get("result") or {}).get("label") == result["label"] and (row.get("result") or {}).get("score") == result.get("score")]
                if len(matching) != 1:
                    raise PhoenixRetry("annotation acknowledgement not yet visible")
            except (httpx.TransportError, ValueError) as exc:
                raise PhoenixRetry(type(exc).__name__) from exc

    @staticmethod
    def _check(response):
        if response.status_code in (404, 408, 429) or response.status_code >= 500:
            raise PhoenixRetry(f"phoenix_http_{response.status_code}")
        if response.is_error:
            # Never persist a remote body that might contain credentials/content.
            raise PhoenixRejected(f"phoenix_http_{response.status_code}")
