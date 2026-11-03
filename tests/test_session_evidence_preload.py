"""Prior-turn evidence/gaps are dormant until the model calls session_recall."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.session_recall import load_session_context
from src.agents.tools.evidence_tools import make_evidence_read
from src.agents.tools.runtime import ToolRuntime
from src.agents.tools.session_tools import make_session_recall


class _ConversationService:
    def list_recent_turns(self, user_id, session_id, limit=4):
        assert user_id == 4
        assert session_id == 94
        return [
            SimpleNamespace(
                query="VCC3V3_EQ 的过压门限是多少？",
                answer="上一轮结论：门限是 2.884V / 3.782V。",
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
                    ],
                },
            )
        ]


def _loaded(monkeypatch, question: str = "") -> ToolRuntime:
    import src.core.conversation as conversation

    monkeypatch.setattr(conversation, "ConversationService", _ConversationService)
    runtime = ToolRuntime(kb_name="AAA", ctx=None, question=question)
    load_session_context(runtime, 94, 4)
    return runtime


def test_load_session_context_is_dormant(monkeypatch):
    runtime = _loaded(monkeypatch)

    assert [item.id for item in runtime.session_recall_items] == ["document:old-1"]
    assert runtime.session_recall_gaps == ["过压门限数值未给出", "Q1600 内部拓扑缺失"]
    assert runtime.candidate_evidence == {}
    assert runtime.evidence == []
    assert runtime.prior_gaps == []
    assert "<session_state>" in runtime.session_state_text
    assert "门限是 2.884V" in runtime.session_state_text


def test_session_recall_exposes_pointers_only_when_called(monkeypatch):
    runtime = _loaded(monkeypatch)

    rendered = make_session_recall(runtime)()

    assert "id=document:old-1" in rendered
    assert "上一轮未解决缺口" in rendered
    assert "document:old-1" in runtime.candidate_evidence
    assert runtime.prior_gaps == ["过压门限数值未给出", "Q1600 内部拓扑缺失"]
    assert runtime.evidence == []


def test_session_recall_candidate_can_be_promoted(monkeypatch):
    runtime = _loaded(monkeypatch)

    make_session_recall(runtime)()
    rendered = make_evidence_read(runtime)(["document:old-1"])

    assert "[1] 来源" in rendered
    assert runtime.evidence[0].id == "document:old-1"
    assert runtime.evidence[0].metadata["citation_number"] == 1
    assert "document:old-1" not in runtime.candidate_evidence


def test_unrelated_question_gets_no_automatic_context(monkeypatch):
    runtime = _loaded(monkeypatch, question="KL30 电源管理如何工作？")

    assert runtime.candidate_evidence == {}
    assert runtime.prior_gaps == []
    assert runtime.session_recall_items


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
