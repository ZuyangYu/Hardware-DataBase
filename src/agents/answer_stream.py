"""Streaming answer/narration classification and retraction helpers.

The runner consumes provider chunks; this module owns the provider-agnostic
split between provisional answer text and interim narration.  Keeping it out of
the runner makes the SSE/eval replay contract reusable by other transports.
"""

from __future__ import annotations

from typing import Any, Callable


class AnswerSplitter:
    """Incremental narration/answer classifier over the deepagents model stream.

    deepagents loop semantics: the loop continues only while the model calls
    tools, so the last model message is the final answer and every earlier
    model message ("让我再检索…") is interim narration. Tool-call metadata on
    streamed chunks is not uniform across providers, so classification is
    incremental with two signals:

    1. tool calls visible on the chunk (``tool_calls`` / ``tool_call_chunks``)
       → the segment is narration from that point on;
    2. a follow-up segment starting → the previous message must have called
       tools (the loop continued), so it is retroactively narration.

    Text of a not-yet-disproven segment streams out live as provisional
    answer deltas. Once a segment is classified as narration, its already-
    streamed text is announced through ``on_narration`` exactly once so
    consumers can retract it (``strip_narration_segments``); the ``done``
    payload always carries the authoritative answer. The final segment
    completes without tool calls — which is precisely the loop's stopping
    condition — so its streamed text is the answer.
    """

    def __init__(self, on_narration: Callable[[str], None]):
        self._on_narration = on_narration
        self._order: list[str] = []
        self._segments: dict[str, dict[str, Any]] = {}
        self._current = ""

    def _start_segment(self, key: str) -> None:
        if self._current:
            prev = self._segments[self._current]
            # 循环没有终止就开启了下一段 → 上一段必然触发过工具调用，是叙述。
            prev["narration"] = True
            self._announce(self._current)
        self._current = key
        self._order.append(key)
        self._segments[key] = {
            "text": "",
            "streamed": "",
            "narration": False,
            "announced": False,
        }

    def _announce(self, key: str) -> None:
        seg = self._segments[key]
        if seg["narration"] and not seg["announced"] and seg["streamed"]:
            seg["announced"] = True
            self._on_narration(seg["streamed"])

    def feed(self, chunk: Any, text: str) -> str:
        """Ingest one model-node chunk; return the text to stream as answer."""
        seg_id = str(getattr(chunk, "id", "") or "")
        has_tool_calls = bool(getattr(chunk, "tool_calls", None)) or bool(
            getattr(chunk, "tool_call_chunks", None)
        )
        current = self._segments.get(self._current)
        if seg_id and seg_id != self._current:
            self._start_segment(seg_id)
        elif current is not None and current["narration"] and text:
            # 工具调用之后到来的文本必然来自下一次模型调用（无 id 的伪流式
            # 路径，或跨调用复用同一 id 的提供方），从这里开新段。
            self._start_segment(f"seg-{len(self._order)}")
        elif not self._current:
            self._start_segment("seg-0")
        seg = self._segments[self._current]
        seg["text"] += text
        if has_tool_calls and not seg["narration"]:
            seg["narration"] = True
            self._announce(self._current)
        if seg["narration"]:
            return ""
        seg["streamed"] += text
        return text

    def finish(self) -> str:
        """Return the authoritative answer text (the last model message)."""
        for key in reversed(self._order):
            seg = self._segments[key]
            if not seg["narration"]:
                return seg["text"]
            self._announce(key)
        return ""


def strip_narration_segments(answer: str, narrations: list[str]) -> str:
    """Remove narration text that was optimistically streamed as answer deltas.

    ``AnswerSplitter`` streams provisional deltas live and reclassifies
    segments as narration once tool calls surface; each ``narration`` event
    carries the retracted text in stream order. Consumers that joined the raw
    delta stream (SSE answer accumulation, eval response collection) recover
    the authoritative answer with this helper: a narration segment is a
    contiguous, in-order substring of the joined deltas, so each entry is
    removed at or after the previous removal position.
    """
    result = str(answer or "")
    search_from = 0
    for narr in narrations:
        text = str(narr or "")
        if not text:
            continue
        idx = result.find(text, search_from)
        if idx < 0:
            idx = result.find(text)
            if idx < 0:
                continue
            search_from = 0
        result = result[:idx] + result[idx + len(text) :]
        search_from = idx
    return result


__all__ = ["AnswerSplitter", "strip_narration_segments"]
