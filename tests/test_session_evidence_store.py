"""Session evidence catalogue: dedupe, recency, and lexical recall."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.session_evidence import SessionEvidenceStore, load_session_evidence


def _row(evidence_id: str, content: str, *, metadata=None):
    return {
        "id": evidence_id,
        "content": content,
        "source_name": f"{evidence_id}.docx",
        "content_kind": "document_text",
        "processor_kind": "ragflow",
        "score": 0.8,
        "locator": {"page": 1},
        "metadata": metadata or {},
    }


def _turn(
    turn_id: str,
    query: str,
    rows: list[dict],
    *,
    answer: str = "",
    gaps: list[str] | None = None,
):
    return SimpleNamespace(
        id=turn_id,
        query=query,
        answer=answer,
        created_at=f"2026-11-03T10:00:0{turn_id}z",
        summary={
            "verification": {"answer_gaps": gaps or []},
            "evidence": rows,
        },
    )


def test_store_keeps_newest_duplicate_and_strips_old_citation_numbers():
    turns = [
        _turn("1", "newest", [_row("doc:a", "fresh content", metadata={"citation_number": 9})]),
        _turn("2", "older", [_row("doc:a", "stale content"), _row("doc:b", "other")]),
    ]

    store = SessionEvidenceStore.from_turns(turns)

    assert [entry.evidence.id for entry in store.entries] == ["doc:a", "doc:b"]
    assert store.entries[0].evidence.content == "fresh content"
    assert "citation_number" not in store.entries[0].evidence.metadata
    assert store.entries[0].turn_id == "1"


def test_store_search_ranks_relevance_and_uses_recency_for_ties():
    turns = [
        _turn("1", "电源轨 P1", [_row("doc:new-power", "P1 电源正常")]),
        _turn("2", "KL30 电源管理", [_row("doc:kl30", "KL30 电源管理方案")]),
        _turn("3", "BOM", [_row("doc:bom", "BOM 物料清单")]),
    ]
    store = SessionEvidenceStore.from_turns(turns)

    assert [item.id for item in store.search("KL30")] == ["doc:kl30"]
    # Both power entries match; the newer one wins the tie.
    assert [item.id for item in store.search("电源")] == [
        "doc:new-power",
        "doc:kl30",
    ]
    assert [item.id for item in store.search("missing")] == []


def test_store_empty_query_returns_recent_first_and_is_bounded():
    turns = [
        _turn("1", "first", [_row(f"doc:{index}", f"content {index}")])
        for index in range(5)
    ]
    store = SessionEvidenceStore.from_turns(turns)

    assert [item.id for item in store.search("", limit=2)] == ["doc:0", "doc:1"]


def test_store_collects_gaps_newest_first_without_duplicates():
    turns = [
        _turn("1", "first", [], gaps=["过压门限未给出"]),
        _turn("2", "second", [], gaps=["过压门限未给出", "Q1600 拓扑缺失"]),
    ]

    store = SessionEvidenceStore.from_turns(turns)

    assert store.gaps == ["过压门限未给出", "Q1600 拓扑缺失"]


def test_load_session_evidence_is_fail_open():
    class _Boom:
        def list_recent_turns(self, *args, **kwargs):
            raise RuntimeError("db down")

    store = load_session_evidence(4, 94, service=_Boom())

    assert store.entries == []
    assert store.history == []


def test_gaps_stay_on_the_short_horizon_while_evidence_reaches_further():
    turns = [
        _turn(str(index), f"turn {index}", [], gaps=[f"gap-{index}"])
        for index in range(6)
    ]

    store = SessionEvidenceStore.from_turns(turns)

    assert store.gaps == ["gap-0", "gap-1", "gap-2", "gap-3"]
    assert len(store.history) == 6
