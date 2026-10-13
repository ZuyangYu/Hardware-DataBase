"""One event pair per provider leaf, with nested adapter calls suppressed."""
from __future__ import annotations

import time
import asyncio
import uuid
from contextlib import contextmanager

import src.settings as settings
from src.quality.store import now_iso, sha256
from .context import _ACTIVE, current_context
from .sink import get_sink
from .usage import Usage, normalize, response_identity


class LeafCall:
    def __init__(self, model, *, visibility="logical_only"):
        self.sink = get_sink() if getattr(settings, "COST_LEDGER_ENABLED", True) else None
        requested = str(getattr(model, "hdb_model", "") or getattr(model, "model_name", "") or getattr(model, "model", "unknown"))
        provider = str(getattr(model, "hdb_provider", "unknown"))
        endpoint = str(getattr(model, "openai_api_base", "") or getattr(model, "base_url", ""))
        # Explicit registry controls aliases; URLs/credentials never leave this
        # lookup. Unregistered endpoints cannot accidentally use another price.
        registry = getattr(settings, "COST_MODEL_REGISTRY", {})
        identity = registry.get(sha256([provider, endpoint, requested]), {})
        invocation = "call-" + uuid.uuid4().hex
        self.event = {**current_context(), "invocation_id": invocation, "logical_call_id": invocation,
                      "provider_attempt_no": None, "attempt_visibility": visibility,
                      "endpoint_alias": identity.get("endpoint_alias", "unregistered"), "provider": provider,
                      "model_alias": identity.get("model_alias", "unregistered"), "requested_model": requested,
                      "response_model": "", "provider_request_id": "", "started_at": now_iso(), "finished_at": "",
                      "duration_ms": None, "ttft_ms": None, "status": "started", "metadata_version": 1,
                      **Usage().to_dict()}
        from src.observability import current_trace_identity

        trace = current_trace_identity()
        self.event.update(trace_id=trace.trace_id, span_id=trace.span_id)
        self.started = time.monotonic()
        self.aggregate = Usage()
        self.invalid_chunk_metadata = False
        self.first = None
        self.response = None
        self.publish(0)

    def publish(self, sequence):
        if self.sink is not None:
            self.sink.emit({**self.event, "sequence": sequence, "event_id": f"{self.event['invocation_id']}:{sequence}"})

    def chunk(self, response):
        try:
            self._chunk(response)
        except Exception:
            # A provider-specific malformed metadata object must not break the
            # business stream. Retain an explicit incomplete usage record.
            self.aggregate = Usage(usage_status="partial")
            self.invalid_chunk_metadata = True

    def _chunk(self, response):
        self.response = response
        self.event.update({k: v for k, v in response_identity(response).items() if v})
        raw = getattr(response, "message", response)
        if self.first is None and getattr(raw, "content", None):
            self.first = time.monotonic()
        usage = normalize(response)
        if usage.usage_status != "missing":
            self.invalid_chunk_metadata |= usage.usage_status == "partial" and usage.input_tokens is not None and usage.output_tokens is not None
            # LangChain chunks are additive (AIMessageChunk.__add__). SDK raw
            # cumulative streams must use a provider-specific adapter instead.
            old = self.aggregate
            fields = {}
            for k in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens", "reasoning_tokens"):
                a, b = getattr(old, k), getattr(usage, k)
                fields[k] = None if a is None and b is None else (a or 0) + (b or 0)
            i, o, cached, reasoning = (fields[k] for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"))
            valid = i is not None and o is not None and (cached is None or cached <= i) and (reasoning is None or reasoning <= o)
            fields["usage_status"] = "reported" if valid and usage.usage_status != "partial" and not self.invalid_chunk_metadata else "partial"
            self.aggregate = Usage(**fields)

    def finish(self, status):
        usage = self.aggregate if self.aggregate.usage_status != "missing" else normalize(self.response)
        self.event.update(**usage.to_dict(), **{k: v for k, v in response_identity(self.response).items() if v}, status=status,
                          finished_at=now_iso(), duration_ms=(time.monotonic()-self.started)*1000,
                          ttft_ms=(self.first-self.started)*1000 if self.first else None)
        self.publish(1)


@contextmanager
def leaf_call(model):
    if _ACTIVE.get():
        yield None
        return
    token = _ACTIVE.set(True)
    call = None
    status = "success"
    observation = None
    try:
        try:
            from src.observability import observe

            observation = observe.llm("hdb.llm.provider", model=str(getattr(model, "hdb_model", "") or getattr(model, "model", "unknown")),
                                      provider=str(getattr(model, "hdb_provider", "unknown")))
            observation.start()
            observation.set("hdb.gen_ai.mapping_version", "hdb-genai-20261008-v1")
            observation.set("gen_ai.operation.name", "embeddings" if current_context()["operation"] == "embedding" else "chat")
            observation.set("gen_ai.provider.name", str(getattr(model, "hdb_provider", "unknown")))
            observation.set("gen_ai.request.model", str(getattr(model, "hdb_model", "") or getattr(model, "model", "unknown")))
            call = LeafCall(model)
            observation.set("hdb.invocation.id", call.event["invocation_id"])
            observation.set("hdb.attempt_visibility", call.event["attempt_visibility"])
        except Exception:
            pass  # Accounting never changes the provider response contract.
        yield call
    except GeneratorExit:
        status = "cancelled"
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except BaseException:
        status = "error"
        raise
    finally:
        if call:
            try:
                call.finish(status)
                if observation:
                    observation.tokens(input_tokens=call.event["input_tokens"], output_tokens=call.event["output_tokens"])
                    if call.event["response_model"]:
                        observation.set("gen_ai.response.model", call.event["response_model"])
            except Exception:
                pass
        if observation:
            if status == "cancelled":
                observation.set("hdb.status", "cancelled")
            else:
                observation.outcome(status)
            observation.end()
        _ACTIVE.reset(token)
