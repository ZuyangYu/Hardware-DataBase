from __future__ import annotations


def test_explicit_candidate_request_keeps_search_generation_order_and_release_ban():
    """Removing a user constraint or action dependency must fail this test."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan(
        "先结合附件和知识库查电压范围，再生成 Word 候选，先别发布",
        has_attachments=True,
        has_kb=True,
    )

    assert [action.kind for action in plan.actions] == ["search", "create_document"]
    assert plan.actions[1].depends_on == ["a1"]
    assert plan.actions[1].parameters == {"format": "docx", "delivery": "candidate"}
    assert [(constraint.kind, constraint.value) for constraint in plan.constraints] == [
        ("forbid_action", "release"),
    ]


def test_explicit_output_format_is_preserved_by_the_deterministic_plan():
    """An explicit delivery format must not silently become DOCX."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("结合知识库生成 PDF 报告")

    assert [action.kind for action in plan.actions] == ["search", "create_document"]
    assert plan.actions[-1].parameters == {"format": "pdf", "delivery": "candidate"}


def test_multiple_requested_output_formats_require_clarification_before_writing():
    """The planner must not silently select DOCX for a multi-format request."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("请生成 PDF 和 Word 报告")

    assert plan.actions == []
    assert plan.unresolved_refs == ["multiple_output_formats"]


def test_unsupported_requested_output_format_requires_clarification_before_writing():
    """An unregistered format must not fall back to the default document type."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("请生成 TXT 报告")

    assert plan.actions == []
    assert plan.unresolved_refs == ["unsupported_output_format"]


def test_refdes_bulk_instruction_becomes_one_scoped_decision_before_continuing():
    """A missing group scope would turn one user instruction into repeated prompts."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("X1900 全部包含，继续生成")

    assert [action.kind for action in plan.actions] == ["resolve_decision", "resume"]
    assert plan.actions[0].parameters == {
        "action": "include",
        "refdes": ["X1900"],
        "scope": "all_pending_for_refdes",
    }
    assert plan.actions[1].depends_on == ["a1"]


def test_compound_refdes_instructions_keep_each_group_decision_before_one_resume():
    """Collapsing opposite group commands would silently alter user scope."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan(
        "X1900 包含，X1902 排除，然后给我候选，先别发布",
    )

    assert [(action.kind, action.parameters) for action in plan.actions] == [
        ("resolve_decision", {
            "action": "include",
            "refdes": ["X1900"],
            "scope": "all_pending_for_refdes",
            "continue_generation": False,
        }),
        ("resolve_decision", {
            "action": "exclude",
            "refdes": ["X1902"],
            "scope": "all_pending_for_refdes",
            "continue_generation": False,
        }),
        ("resume", {}),
    ]
    assert plan.actions[2].depends_on == ["a1", "a2"]
    assert [(constraint.kind, constraint.value) for constraint in plan.constraints] == [
        ("forbid_action", "release"),
    ]


def test_refdes_bulk_process_compiles_to_server_default_decision():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("X1900 全部处理")

    assert len(plan.actions) == 1
    assert plan.actions[0].kind == "resolve_decision"
    assert plan.actions[0].parameters == {
        "action": "default",
        "refdes": ["X1900"],
        "scope": "all_pending_for_refdes",
    }


def test_deferred_generation_stays_read_only_until_the_user_decides_after_search():
    """Treating a tentative future generation as a current write is unsafe."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan(
        "先查一下这两份手册的电压范围，再决定要不要生成",
        has_attachments=True,
        has_kb=True,
    )

    assert [action.kind for action in plan.actions] == ["search"]
    assert plan.actions[0].parameters["source_scope"] == "attachment_and_knowledge_base"
    assert [(constraint.kind, constraint.value) for constraint in plan.constraints] == [
        ("forbid_action", "create_document"),
    ]


def test_ambiguous_pin_reference_is_left_unresolved_without_a_write_action():
    """Guessing which grouped pin the user meant would mutate the wrong scope."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("那个脚不要了")

    assert plan.actions == []
    assert plan.unresolved_refs == ["ambiguous_pin_reference"]


def test_pause_and_candidate_request_pauses_then_refreshes_the_existing_task_only():
    """Turning “先停一下” into generation or release would violate the user's direction."""
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("先停一下，把刚才的候选发我看看")

    assert [(action.kind, action.required_capabilities) for action in plan.actions] == [
        ("pause", ["document.task.control"]),
        ("get_status", ["document.task.read"]),
    ]
    assert plan.actions[1].depends_on == ["a1"]
    assert not any(action.kind in {"create_document", "request_release"} for action in plan.actions)


def test_explicit_no_generate_never_compiles_a_document_write_action():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan(
        "不要生成文档，只告诉我资料是否齐全",
        has_attachments=True,
        has_kb=True,
    )

    assert [action.kind for action in plan.actions] == []
    assert [(constraint.kind, constraint.value) for constraint in plan.constraints] == [
        ("forbid_action", "create_document"),
    ]


def test_cancel_current_document_task_is_a_task_control_not_a_new_generation():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("取消刚才的文档任务")

    assert [action.kind for action in plan.actions] == ["cancel", "get_status"]
    assert plan.actions[1].depends_on == ["a1"]


def test_resume_current_document_task_is_not_compiled_as_a_new_document():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("继续刚才的文档生成任务")

    assert [action.kind for action in plan.actions] == ["resume", "get_status"]
    assert plan.actions[1].depends_on == ["a1"]


def test_release_request_is_kept_separate_from_document_generation():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("请为当前候选申请正式发布")

    assert [action.kind for action in plan.actions] == ["request_release"]
    assert plan.actions[0].required_capabilities == ["document.release.request"]


def test_section_revision_preserves_unspecified_units_and_binds_the_section_scope():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan(
        "把上一版报告第三章改成表格，其他章节保持不变",
    )

    assert [action.kind for action in plan.actions] == ["revise_document"]
    assert plan.actions[0].parameters == {
        "request_type": "section_update",
        "request": "把上一版报告第三章改成表格，其他章节保持不变",
        "changed_sections": ["第三章"],
        "changed_fields": [],
    }
    assert [(constraint.kind, constraint.value) for constraint in plan.constraints] == [
        ("preserve", "all_non_target_units"),
    ]


def test_revision_question_does_not_create_an_unresolved_write_action():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan("更新报告的原因是什么？")

    assert plan.actions == []
    assert plan.unresolved_refs == []


def test_exact_attachment_text_revision_compiles_to_a_governed_attachment_action():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan(
        "把附件中的“ADAS ICD”替换为“ADAS Interface Control Document”",
        has_attachments=True,
        has_kb=False,
    )

    assert [action.kind for action in plan.actions] == ["revise_attachment"]
    assert plan.actions[0].parameters == {
        "request": "把附件中的“ADAS ICD”替换为“ADAS Interface Control Document”",
        "before": "ADAS ICD",
        "after": "ADAS Interface Control Document",
    }
    assert plan.actions[0].required_capabilities == ["attachment.revise"]


def test_unquoted_attachment_edit_becomes_a_confirmable_section_proposal():
    from src.agents.orchestration.contracts import compile_deterministic_action_plan

    plan = compile_deterministic_action_plan(
        "把附件里的标题改成新的标题",
        has_attachments=True,
        has_kb=False,
    )

    # No direct write action: the request may only continue as a model
    # proposal that the user confirms (two-stage governance).
    assert plan.actions == []
    assert plan.unresolved_refs == ["attachment_section_proposal"]
