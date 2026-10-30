"""Prior-turn evidence is reused as non-citable candidate pointers."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.session_recall import preload_session_context
from src.agents.tools.runtime import ToolRuntime


class _ConversationService:
    def list_recent_turns(self, user_id, session_id, limit=3):
        assert user_id == 4
        assert session_id == 94
        return [
            SimpleNamespace(
                summary={
                    "verification": {
                        "answer_gaps": ["过压门限数值未给出"],
                        "gaps": ["Q1600 内部拓扑缺失"],
                    },
                    "evidence": [
                        {
                            "id": "document:old-1",
                            "content": "上一轮已经读过的 TC377 最小系统正文。",
                            "source_name": "600608964_ADAS_HSI.docx",
                            "content_kind": "document_text",
                            "processor_kind": "ragflow",
                            "score": 0.9,
                            "locator": {"page": 3},
                            "metadata": {"kb_id": 4, "citation_number": 7},
                        }
                    ]
                }
            )
        ]


def test_prior_turn_evidence_is_preloaded_as_candidates(monkeypatch):
    import src.core.conversation as conversation

    monkeypatch.setattr(conversation, "ConversationService", _ConversationService)
    runtime = ToolRuntime(kb_name="AAA", ctx=None)

    preload_session_context(runtime, 94, 4)

    assert runtime.evidence == []
    assert "document:old-1" in runtime.candidate_evidence
    assert runtime.prior_gaps == ["过压门限数值未给出", "Q1600 内部拓扑缺失"]
    prompt = runtime.prior_gap_prompt()
    assert "上一轮遗留缺口" in prompt
    assert "过压门限数值未给出" in prompt
    row = runtime.stash_candidates([runtime.candidate_evidence["document:old-1"]])
    assert row[0]["id"] == "document:old-1"


def test_prior_turn_evidence_is_renumbered_when_promoted(monkeypatch):
    import src.core.conversation as conversation

    from src.agents.tools.evidence_tools import make_evidence_read

    monkeypatch.setattr(conversation, "ConversationService", _ConversationService)
    runtime = ToolRuntime(kb_name="AAA", ctx=None)

    preload_session_context(runtime, 94, 4)

    assert "document:old-1" in runtime.prior_evidence_prompt()
    rendered = make_evidence_read(runtime)(["document:old-1"])

    assert "[1] 来源" in rendered
    assert runtime.evidence[0].id == "document:old-1"
    assert runtime.evidence[0].metadata["citation_number"] == 1
    assert "document:old-1" not in runtime.candidate_evidence


def test_answer_gap_lines_extracts_gap_sentences():
    from src.agents.turn_meta import answer_gap_lines

    lines = answer_gap_lines(
        "门限为 3.3V [1]。过压门限数值未给出。Q1600 拓扑证据不足。没有查到电气参数。"
    )

    assert lines == [
        "过压门限数值未给出",
        "Q1600 拓扑证据不足",
        "没有查到电气参数",
    ]
