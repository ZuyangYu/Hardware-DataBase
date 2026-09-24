"""SqliteStore index contract tests (LangMem V2 §21 CI gate).

The memory Store must expose an explicit semantic index over
``content.content`` / ``content.title`` / ``content.subject``.  Upgrading the
locked LangGraph checkpoint-sqlite dependency may silently change internal
tokenization paths; these tests fail loudly instead of falling back to the
default whole-object index.
"""

from __future__ import annotations

import sqlite3
import zlib

import pytest

from src.memory.store import MemoryStoreRuntime


DIMENSIONS = 4096
_VOCAB = [
    "redtitle",
    "bluecontent",
    "greenone",
    "purptitle",
    "cyancontent",
    "graytwo",
]


def _token_bucket(token: str) -> int:
    return zlib.crc32(token.encode("utf-8")) % DIMENSIONS


assert len({_token_bucket(word) for word in _VOCAB}) == len(_VOCAB), (
    "hash-bucket collision between vocabulary tokens would break the contract"
)


def _hash_embedding(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        vector = [0.0] * DIMENSIONS
        for word in str(text).split():
            vector[_token_bucket(word)] += 1.0
        vectors.append(vector)
    return vectors


@pytest.fixture()
def indexed_runtime(tmp_path):
    runtime = MemoryStoreRuntime(
        path=str(tmp_path / "memory.db"),
        index={
            "dims": DIMENSIONS,
            "embed": _hash_embedding,
            "fields": ["content.content", "content.title", "content.subject"],
        },
    )
    try:
        yield runtime
    finally:
        runtime.close()


def _value(title: str, content: str, subject: str) -> dict:
    return {
        "kind": "ProjectMemory",
        "content": {"title": title, "content": content, "subject": subject},
        "schema_version": "1",
    }


def test_index_fields_are_individually_searchable(indexed_runtime):
    namespace = ("hdb", "department", "1", "kb", "2", "candidate")
    indexed_runtime.put(namespace, "m-1", _value("redtitle", "bluecontent", "greenone"))
    indexed_runtime.put(namespace, "m-2", _value("purptitle", "cyancontent", "graytwo"))

    for query, expected_key in (
        ("bluecontent", "m-1"),
        ("redtitle", "m-1"),
        ("greenone", "m-1"),
        ("cyancontent", "m-2"),
        ("graytwo", "m-2"),
    ):
        items = indexed_runtime.search(namespace, query=query, limit=5)
        assert items, f"semantic search returned nothing for field query {query!r}"
        top = max(items, key=lambda item: item.score or 0.0)
        assert top.key == expected_key, f"query {query!r} did not hit its projection"


def test_store_setup_health_and_wal(indexed_runtime):
    assert indexed_runtime.semantic_index_ready is True
    health = indexed_runtime.health()
    assert health["ok"] is True and health["semantic_index"] is True

    connection = sqlite3.connect(indexed_runtime.path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert mode.lower() == "wal"


def test_missing_index_stays_healthy_but_marks_semantic_unready(tmp_path):
    runtime = MemoryStoreRuntime(path=str(tmp_path / "plain-memory.db"))
    try:
        assert runtime.semantic_index_ready is False
        assert runtime.health()["ok"] is True
        namespace = ("hdb", "user", "u1", "candidate")
        runtime.put(namespace, "k", {"kind": "UserMemory", "content": {"title": "t"}})
        items = runtime.search(namespace, query="t", limit=5)
        # Without a semantic index the raw Store still answers; MemoryService
        # is responsible for fail-closed Candidate skipping.
        assert isinstance(items, list)
    finally:
        runtime.close()
