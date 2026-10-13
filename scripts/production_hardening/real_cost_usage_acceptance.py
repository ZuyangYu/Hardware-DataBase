"""Probe configured real provider usage into an isolated, unpriced ledger.

Uses a synthetic short prompt. The evidence retains accounting metadata only;
no endpoint, credential, prompt, response content or invented price is saved.
"""
from __future__ import annotations

import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import src.settings as settings
from src.core.auth import AuthUser
from src.core.chat_model_runtime import invoke_text, stream_text
from src.core.model_factory import _build_cached_chat_model, resolve_chat_model_config
from src.costs.context import call_context
from src.costs.sink import get_sink, shutdown_sinks
from src.costs.store import CostStore
from src.quality.store import sha256


def main():
    config = resolve_chat_model_config(priority="batch")
    artifact = Path(__file__).resolve().parents[2] / "storage/evaluations/production-feedback-p2-20261008"
    artifact.mkdir(parents=True, exist_ok=True)
    result = {"fixture_only": False, "content": "synthetic_non_private_prompt", "configured_provider": config.provider,
              "requested_model": config.model, "sdk_max_retries_for_probe": 0, "price_source": "none", "settlement_evidence": False}
    with tempfile.TemporaryDirectory(prefix="hdb-real-usage-", dir="/tmp/opencode") as tmp:
        settings.STORAGE_DIR = tmp
        settings.AUTH_DB_PATH = str(Path(tmp) / "auth.db")
        settings.LLM_USAGE_LEDGER_DB = settings.AUTH_DB_PATH
        settings.CONTROL_PLANE_BACKEND = "sqlite"
        settings.CONTROL_PLANE_DATABASE_URL = ""
        settings.COST_LEDGER_ENABLED = True
        settings.OBS_ENABLED = False
        model = _build_cached_chat_model(9000, replace(config, max_tokens=128, max_retries=0, timeout=60))
        provider = "ollama" if config.provider == "ollama" else "openai-compatible"
        endpoint = str(getattr(model, "openai_api_base", "") or getattr(model, "base_url", ""))
        settings.COST_MODEL_REGISTRY = {sha256([provider, endpoint, config.model]): {"endpoint_alias": "configured-acceptance", "model_alias": "configured-model"}}
        outcomes = []
        for mode in ("sync", "stream"):
            started = time.monotonic()
            try:
                with call_context(task_type="acceptance", task_id="real-usage-" + mode, purpose="quality_analysis", execution_attempt_id="probe-1"):
                    messages = [{"role": "user", "content": "Reply with the single word OK."}]
                    content = invoke_text(model, messages, operation="quality_analysis", profile="default").text if mode == "sync" else "".join(stream_text(model, messages, operation="quality_analysis", profile="default"))
                outcomes.append({"mode": mode, "status": "success", "response_nonempty": bool(content.strip()), "duration_seconds": round(time.monotonic()-started, 3)})
            except Exception as exc:
                outcomes.append({"mode": mode, "status": "error", "exception_type": type(exc).__name__, "duration_seconds": round(time.monotonic()-started, 3)})
        get_sink().queue.join()
        shutdown_sinks()
        store = CostStore()
        admin = AuthUser(0, "acceptance", "system_admin", True)
        calls = store.invocations(admin)
        result.update(outcomes=outcomes, invocations=calls, summary=store.summary(admin), raw_endpoint_retained=False, raw_content_retained=False)
        (artifact / "real-provider-usage.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"outcomes": outcomes, "calls": len(calls), "usage_statuses": [c["usage_status"] for c in calls],
                          "response_models": [c["response_model"] for c in calls], "cost_statuses": [c["cost_status"] for c in calls]}))


if __name__ == "__main__":
    main()
