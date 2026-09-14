"""Tests for the fail-closed structured conversational plan parser."""

from __future__ import annotations

from src.agents.orchestration.capabilities import ACTION_CAPABILITIES
from src.agents.orchestration.intent_parser import (
    StructuredActionPlanParser,
    parse_action_plan_payload,
)


def test_parser_preserves_composite_actions_source_scope_and_edit_references():
    plan = parse_action_plan_payload(
        """{
          "plan_id": "model-plan-1",
          "goal": "先查附件和知识库，再改第三章",
          "context_revision": "turn:12",
          "actions": [
            {
              "action_id": "search",
              "kind": "search",
              "parameters": {
                "source_scope": "attachment_and_knowledge_base",
                "source_refs": ["attachment:manual.docx", "kb:power-guide"]
              },
              "required_capabilities": ["evidence.search"]
            },
            {
              "action_id": "revise",
              "kind": "revise_document",
              "target_ref": "artifact:candidate-7",
              "input_refs": ["section:第三章", "refdes:X1900"],
              "depends_on": ["search"],
              "parameters": {"preserve": ["section:其他章节"]},
              "required_capabilities": ["document.revise"]
            }
          ],
          "constraints": [
            {
              "constraint_id": "preserve-other-sections",
              "kind": "preserve",
              "value": ["section:其他章节"],
              "source_text_span": "其他章节别动"
            }
          ]
        }"""
    )

    assert plan is not None
    assert [action.kind for action in plan.actions] == ["search", "revise_document"]
    assert plan.actions[1].depends_on == ["search"]
    assert plan.actions[1].input_refs == ["section:第三章", "refdes:X1900"]
    assert plan.actions[0].parameters["source_scope"] == "attachment_and_knowledge_base"
    assert plan.constraints[0].value == ["section:其他章节"]


def test_unsafe_model_proposal_falls_back_without_the_forbidden_release_action():
    parser = StructuredActionPlanParser(
        complete=lambda _messages: """{
          "plan_id": "unsafe-model-plan",
          "goal": "先生成候选但不要发布",
          "actions": [{
            "action_id": "release",
            "kind": "request_release",
            "required_capabilities": ["document.release.request"]
          }],
          "constraints": [{
            "constraint_id": "no-release",
            "kind": "forbid_action",
            "value": "release",
            "source_text_span": "不要发布"
          }]
        }""",
    )

    plan = parser.parse(
        "先结合附件和知识库查电压范围，再生成 Word 候选，先别发布",
        context={"has_attachments": True, "has_kb": True},
        available_capabilities=set(ACTION_CAPABILITIES.values()),
    )

    assert plan is not None
    assert [action.kind for action in plan.actions] == ["search", "create_document"]
    assert all(action.kind != "request_release" for action in plan.actions)
    assert [(constraint.kind, constraint.value) for constraint in plan.constraints] == [
        ("forbid_action", "release"),
    ]


def test_parser_rejects_malformed_or_schema_expanding_model_output():
    assert parse_action_plan_payload("not json") is None
    assert parse_action_plan_payload(
        '{"plan_id":"p","goal":"g","actions":[],"surprise":true}',
    ) is None


def test_deterministic_output_format_block_wins_over_model_create_proposal():
    parser = StructuredActionPlanParser(
        complete=lambda _messages: """{
          "plan_id": "model-create-docx",
          "goal": "生成候选",
          "actions": [{
            "action_id": "create",
            "kind": "create_document",
            "parameters": {"format": "docx"},
            "required_capabilities": ["document.authoring"]
          }],
          "constraints": [],
          "unresolved_refs": []
        }""",
    )

    plan = parser.parse(
        "请生成 PDF 和 Word 报告",
        context={"has_kb": True},
        available_capabilities={"document.authoring"},
    )

    assert plan is not None
    assert plan.actions == []
    assert plan.unresolved_refs == ["multiple_output_formats"]
