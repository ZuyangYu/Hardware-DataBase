from __future__ import annotations

from src.document_authoring.generation_sessions import GenerationBrief
from src.document_authoring.requirement_clarifier import RequirementClarifier


def test_clarifier_asks_for_revision_before_generation():
    message = RequirementClarifier().next_message(
        {"format": "xlsx", "units": [{"label": "版本"}]},
        GenerationBrief(),
    )

    assert message.question_id == "scope.revision"
    assert message.options == ["当前发布版本", "最新上传版本", "指定其他版本"]


def test_clarifier_advances_one_question_at_a_time():
    clarifier = RequirementClarifier()
    brief = clarifier.apply_answer(
        GenerationBrief(),
        question_id="scope.revision",
        answer="当前发布版本",
    )

    next_message = clarifier.next_message({"format": "xlsx", "units": []}, brief)

    assert next_message.question_id == "missing_data_policy"
    assert brief.scope["revision"] == "当前发布版本"


def test_clarifier_marks_complete_brief_ready_for_confirmation():
    brief = GenerationBrief(
        scope={"revision": "当前发布版本"},
        missing_data_policy="标记未提供",
        inference_policy="禁止推断",
        output_policy={"format": "xlsx"},
    )

    message = RequirementClarifier().next_message({"format": "xlsx", "units": []}, brief)

    assert message.question_id is None
    assert message.reason == "ready_to_generate"
    assert "需求已经明确" in message.content
