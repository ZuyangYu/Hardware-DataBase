"""Context-window accounting and real summarization-event extraction.

The agent framework already owns summarization.  This module does not replace
it; it turns the framework's private ``_summarization_event`` into the same
auditable item/event vocabulary as tools, and records what occupied the context
window without persisting prompt or evidence text.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from langchain_core.messages.utils import count_tokens_approximately

from src.core.model_context import model_context_window
from src.observability.metrics import record_context_compaction, record_context_pressure


def text_sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _message_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(content)


def _token_count(messages: Iterable[Any]) -> int:
    try:
        return int(count_tokens_approximately(list(messages)))
    except Exception:
        return 0


def append_fragment(
    fragments: list[tuple[str, str]],
    kind: str,
    text: str,
) -> None:
    value = str(text or "")
    if value:
        fragments.append((kind, value))


def read_agent_values(
    agent: Any,
    config: Mapping[str, Any],
    *,
    has_checkpointer: bool,
) -> Mapping[str, Any] | None:
    if not has_checkpointer or not hasattr(agent, "get_state"):
        return None
    try:
        values = getattr(agent.get_state(config), "values", None) or {}
    except Exception:
        return None
    return values if isinstance(values, Mapping) else None


def initial_context_state(
    agent: Any,
    config: Mapping[str, Any],
    *,
    has_checkpointer: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    """Read the pre-turn todo and summarization snapshots once."""

    values = read_agent_values(agent, config, has_checkpointer=has_checkpointer)
    if values is None:
        return None, None
    todos = json.dumps(
        values.get("todos"), ensure_ascii=False, sort_keys=True, default=str
    )
    return todos, summarization_snapshot(values)


def build_context_manifest(
    *,
    system_prompt: str,
    fragments: Iterable[tuple[str, str]],
    evidence: Sequence[Any],
    model_context_window: int,
    estimated_input_tokens: int,
    history_turns: int = 0,
) -> dict[str, Any]:
    """Build a content-free manifest of the model-visible context.

    Only lengths and hashes are persisted; evidence/citation text never leaves
    the turn through this structure.
    """

    fragment_rows: list[dict[str, Any]] = []
    total_bytes = 0
    total_chars = 0
    for kind, text in fragments:
        value = str(text or "")
        if not value:
            continue
        encoded = len(value.encode("utf-8"))
        row = {
            "kind": str(kind or "unknown"),
            "chars": len(value),
            "bytes": encoded,
            "sha256": text_sha256(value),
        }
        fragment_rows.append(row)
        total_bytes += encoded
        total_chars += len(value)

    evidence_bytes = 0
    evidence_chars = 0
    for item in evidence:
        content = str(getattr(item, "content", "") or "")
        evidence_bytes += len(content.encode("utf-8"))
        evidence_chars += len(content)
    fragment_rows.append(
        {
            "kind": "evidence",
            "chars": evidence_chars,
            "bytes": evidence_bytes,
            "count": len(evidence),
        }
    )

    return {
        "system_prompt_sha256": text_sha256(system_prompt),
        "system_prompt_chars": len(system_prompt),
        "system_prompt_bytes": len(system_prompt.encode("utf-8")),
        "fragments": fragment_rows,
        "fragment_bytes": total_bytes,
        "evidence_count": len(evidence),
        "evidence_bytes": evidence_bytes,
        "history_turns": max(0, int(history_turns or 0)),
        "model_context_window": max(0, int(model_context_window or 0)),
        "estimated_input_tokens": max(0, int(estimated_input_tokens or 0)),
    }


def summarization_snapshot(values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a serializable snapshot of deepagents' current compaction event."""

    if not isinstance(values, Mapping):
        return None
    event = values.get("_summarization_event")
    if not isinstance(event, Mapping):
        return None
    try:
        cutoff_index = int(event.get("cutoff_index"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    summary_message = event.get("summary_message")
    summary = _message_content(summary_message)
    file_path = str(event.get("file_path") or "")
    summary_sha = text_sha256(summary)
    fingerprint = text_sha256(f"{max(0, cutoff_index)}:{summary_sha}:{file_path}")
    return {
        "cutoff_index": max(0, cutoff_index),
        "summary": summary,
        "summary_chars": len(summary),
        "summary_sha256": summary_sha,
        "offload_path": file_path,
        "fingerprint": fingerprint,
        "_summary_message": summary_message,
    }


def build_compaction_event(
    *,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
    messages: Sequence[Any],
    window_tokens: int,
    turn_id: str,
) -> dict[str, Any] | None:
    """Turn a changed summarization snapshot into one real compaction event.

    Returns ``None`` when no new compaction happened.  ``before_tokens`` counts
    the raw persisted history; ``after_tokens`` counts the summary plus the
    retained tail, i.e. what the next model call actually receives.
    """

    if current is None:
        return None
    if previous is not None and previous.get("fingerprint") == current.get("fingerprint"):
        return None

    total_messages = len(messages)
    cutoff = min(max(0, int(current.get("cutoff_index") or 0)), total_messages)
    dropped = cutoff
    retained = max(0, total_messages - cutoff)
    summary_message = current.get("_summary_message")
    after_messages = ([summary_message] if summary_message is not None else []) + list(
        messages[cutoff:]
    )
    summary_sha = str(current.get("summary_sha256") or "")
    window_id = f"{str(turn_id or 'turn')[:36]}:{summary_sha[:12]}"
    return {
        "stage": "summarization",
        "trigger": "auto",
        "cutoff_index": cutoff,
        "dropped_messages": dropped,
        "retained_messages": retained,
        "summary_chars": int(current.get("summary_chars") or 0),
        "summary_sha256": summary_sha,
        "offload_path": str(current.get("offload_path") or ""),
        "before_tokens": _token_count(messages),
        "after_tokens": _token_count(after_messages),
        "window_tokens": max(0, int(window_tokens or 0)),
        "window_id": window_id,
        "note": "框架摘要已替换较早的对话窗口。",
    }


def publish_turn_context(
    *,
    model: object,
    system_prompt: str,
    fragments: Iterable[tuple[str, str]],
    evidence: Sequence[Any],
    max_prompt_tokens: int,
    history_turns: int,
    initial_summarization: Mapping[str, Any] | None,
    final_values: Mapping[str, Any] | None,
    turn_id: str,
    event_callback: Any,
    budget_state: dict[str, Any],
    fallback_window: int = 0,
) -> dict[str, Any]:
    """Emit real compaction/pressure events and return the turn manifest."""

    window = model_context_window(model, fallback=fallback_window)
    if window > 0:
        threshold = int(window * 0.85)
        current = summarization_snapshot(final_values)
        messages = (
            final_values.get("messages")
            if isinstance(final_values, Mapping)
            and isinstance(final_values.get("messages"), list)
            else []
        )
        compaction = build_compaction_event(
            previous=initial_summarization,
            current=current,
            messages=messages,
            window_tokens=window,
            turn_id=turn_id,
        )
        if compaction is not None:
            budget_state["context_compaction"] = compaction
            event_callback({"type": "context_compacted", "payload": compaction})
            try:
                record_context_compaction(
                    trigger=str(compaction.get("trigger") or "auto"),
                    outcome="ok",
                    dropped_messages=int(compaction.get("dropped_messages") or 0),
                    before_tokens=int(compaction.get("before_tokens") or 0),
                    after_tokens=int(compaction.get("after_tokens") or 0),
                    summary_chars=int(compaction.get("summary_chars") or 0),
                )
            except Exception:
                pass
        elif max_prompt_tokens >= threshold:
            pressure = {
                "max_prompt_tokens": max(0, int(max_prompt_tokens or 0)),
                "window_tokens": window,
                "threshold_tokens": threshold,
                "note": "上下文接近模型窗口；下一轮可能触发框架摘要。",
            }
            budget_state["context_window"] = pressure
            event_callback({"type": "context_pressure", "payload": pressure})
            try:
                record_context_pressure(
                    stage="warning",
                    max_prompt_tokens=max_prompt_tokens,
                    window_tokens=window,
                )
            except Exception:
                pass

    return build_context_manifest(
        system_prompt=system_prompt,
        fragments=fragments,
        evidence=evidence,
        model_context_window=window,
        estimated_input_tokens=max_prompt_tokens,
        history_turns=history_turns,
    )


__all__ = [
    "append_fragment",
    "build_compaction_event",
    "build_context_manifest",
    "initial_context_state",
    "publish_turn_context",
    "read_agent_values",
    "summarization_snapshot",
    "text_sha256",
]
