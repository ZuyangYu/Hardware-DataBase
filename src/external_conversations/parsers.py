"""Best-effort txt/markdown conversation parser.

Detects role-marked dialogue turns (用户:/assistant:/Q:/A:/...); falls back to
markdown-heading topic blocks; falls back further to a single whole-file block.
Every turn records character offsets into the raw text so evidence can cite
the original file precisely.
"""

from __future__ import annotations

import hashlib
import os
import re

from src.external_conversations.models import ConversationTurn, ExternalConversation

_USER_MARK_RE = re.compile(r"^\s*(?:\[[^\]]+\]\s*)?(?:用户|user|human|提问|问|q)\s*[:：]", re.IGNORECASE)
_ASSISTANT_MARK_RE = re.compile(r"^\s*(?:\[[^\]]+\]\s*)?(?:助手|assistant|ai|回答|答|a)\s*[:：]", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(r"^\s*\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\]")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+")


def make_conversation_id(filename: str, content_hash: str) -> str:
    """Filename stem + content-hash prefix keeps same-name files distinct.

    Pure-CJK (or otherwise unsanitizable) stems collapse to ``conv``; the
    content-hash suffix guarantees uniqueness and the result always matches
    ``[A-Za-z0-9][A-Za-z0-9_-]*`` so it is a safe path component.
    """
    stem = os.path.splitext(os.path.basename(filename))[0].strip()
    safe_stem = re.sub(r"[^A-Za-z0-9]+", "_", stem)[:96].strip("_") or "conv"
    hash_part = (content_hash or hashlib.sha256(b"").hexdigest())[:12]
    return f"{safe_stem}_{hash_part}"


def content_hash_of(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def _read_text(file_path: str) -> tuple[str, bytes]:
    with open(file_path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8-sig"), raw
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), raw


def _split_ts(line: str) -> tuple[str, str]:
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return "", line
    return match.group(1), line[match.end() :].lstrip()


def _classify_line(line: str) -> tuple[str, str, str]:
    """Return (role, ts, remainder) for a candidate marker line."""
    ts, body = _split_ts(line)
    if _USER_MARK_RE.match(body):
        return "user", ts, _USER_MARK_RE.sub("", body, count=1).lstrip()
    if _ASSISTANT_MARK_RE.match(body):
        return "assistant", ts, _ASSISTANT_MARK_RE.sub("", body, count=1).lstrip()
    return "", "", line


def _parse_marked_conversation(text: str) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    current: dict | None = None
    offset = 0

    def close_current(end: int):
        nonlocal current
        if current is not None:
            content = "\n".join(current["lines"]).rstrip()
            if content:
                current["turn"].content = content
                current["turn"].end_offset = end
                turns.append(current["turn"])
            current = None

    for line in text.splitlines(keepends=True):
        line_end = offset + len(line)
        stripped = line.rstrip("\r\n")
        role, ts, remainder = _classify_line(stripped)
        if role:
            close_current(offset)
            current = {
                "turn": ConversationTurn(
                    role=role,
                    content=remainder,
                    ts=ts,
                    start_offset=line_end - len(stripped),
                    end_offset=line_end,
                ),
                "lines": [remainder] if remainder else [],
            }
        elif current is not None:
            if stripped or current["lines"]:
                current["lines"].append(stripped)
        # lines outside any marked block are ignored when the file is a
        # conversation; if no markers exist at all the caller falls back.
        offset = line_end

    close_current(offset)
    return turns


def _parse_markdown_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    buffer: list[str] = []
    for line in text.splitlines():
        if _MD_HEADING_RE.match(line) and buffer:
            block = "\n".join(buffer).strip()
            if block:
                blocks.append(block)
            buffer = [line]
        else:
            buffer.append(line)
    block = "\n".join(buffer).strip()
    if block:
        blocks.append(block)
    return blocks


def parse_external_conversation(
    file_path: str,
    filename: str,
    kb_name: str,
    department_id: str,
    kb_id: int | None = None,
    origin: str = "upload",
    source_group: str = "",
) -> ExternalConversation:
    text, raw = _read_text(file_path)
    digest = content_hash_of(raw)

    turns = _parse_marked_conversation(text) if text.strip() else []
    blocks: list[str] = []
    if not turns and text.strip():
        blocks = _parse_markdown_blocks(text) or [text.strip()]

    title = os.path.splitext(os.path.basename(filename))[0]
    return ExternalConversation(
        conversation_id=make_conversation_id(filename, digest),
        kb_name=kb_name,
        department_id=department_id,
        title=title,
        source_file=filename,
        content_hash=digest,
        origin=origin,
        source_group=source_group,
        kb_id=int(kb_id or 0),
        turns=turns,
        blocks=blocks,
    )
