"""Record native async judge/re-ask calls while preserving AsyncOpenAI identity."""
from __future__ import annotations

from types import SimpleNamespace

from .capture import leaf_call
from .context import call_context


def instrument_async_openai(client):
    for resource, operation in ((client.chat.completions, "generate"), (client.embeddings, "embedding")):
        original = resource.create
        if getattr(original, "_hdb_cost_observed", False):
            continue

        async def create(*args, _original=original, _operation=operation, **kwargs):
            model = SimpleNamespace(hdb_provider="openai-compatible", hdb_model=kwargs.get("model", "unknown"),
                                    openai_api_base=str(client.base_url).rstrip("/"))
            with call_context(purpose="evaluation_judge", operation=_operation), leaf_call(model) as call:
                result = await _original(*args, **kwargs)
                if call:
                    call.response = result
                return result

        create._hdb_cost_observed = True
        resource.create = create
    return client
