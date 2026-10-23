"""Runtime glue between TaskPlan, comparison pages and tools.

Kept out of ``runner.py`` so the agent shell remains a thin adapter and the
architecture line budget stays stable.
"""

from __future__ import annotations

from typing import Any

from src.services import task_settings
from src.services.comparison_scope import (
    resolve_scope,
    scope_chapter_groups,
    scope_chapter_item_ids,
)
from src.services.document_comparison_models import comparison_report_from_dict
from src.services.task_budget import TurnBudget
from src.services.task_completion import CompletionDecision, CompletionGate
from src.services.task_planning import TaskPlan, TaskPlanCompiler
from src.services.task_progress import (
    TaskProgress,
    ToolObservation,
    build_answer_manifest,
)
from src.storage.document_comparison_store import DocumentComparisonStore


def create_compare_task(
    rt: Any,
    *,
    query: str,
    turn_id: str,
    evidence_purpose: str,
    source_scope_fingerprint: str,
    has_attachments: bool,
    has_kb: bool,
    comparison_available: bool,
) -> TaskPlan | None:
    """Create and attach a task contract when the planning flag is enabled."""

    if not task_settings.TASK_PLANNING_ENABLED:
        return None
    if str(evidence_purpose or "") not in {"qa", "summarize", "extract", "compare"}:
        return None
    compiler = TaskPlanCompiler()
    plan = compiler.compile(
        query=query,
        turn_id=turn_id,
        evidence_purpose=evidence_purpose,
        source_scope_fingerprint=source_scope_fingerprint,
        has_attachments=has_attachments,
        has_kb=has_kb,
        comparison_available=comparison_available,
    )
    progress = TaskProgress.new(plan.plan_id)
    progress.ensure_deliverables(tuple(item.deliverable_id for item in plan.deliverables))
    setattr(rt, "task_plan", plan)
    setattr(rt, "task_progress", progress)
    if plan.task_kind == "compare":
        setattr(rt, "turn_budget", TurnBudget(plan.budget_profile))
    return plan


def prepare_compare_task_for_turn(
    rt: Any,
    *,
    query: str,
    thread_id: str,
    purpose: str,
    scope: str,
    knowledge_scope: Any,
    has_attachments: bool,
    is_general: bool,
) -> TaskPlan | None:
    try:
        return create_compare_task(
            rt,
            query=query,
            turn_id=str(thread_id or ""),
            evidence_purpose=purpose,
            source_scope_fingerprint=str(
                getattr(knowledge_scope, "selection_hash", "") or scope
            ),
            has_attachments=has_attachments,
            has_kb=not is_general,
            comparison_available=True,
        )
    except Exception:
        return None


def run_comparison_preflight(tools: list[Any], rt: Any) -> str:
    """Run document_compare once before the model loop for compare turns.

    This prevents the model from spending the ordinary retrieval budget on
    exploratory searches and then claiming document_compare could not run.
    The comparison reserve is used first, and the frozen report is registered
    as formal evidence for the subsequent answer.
    """

    if not task_settings.TASK_COMPARISON_PREFLIGHT_ENABLED:
        return ""
    tool = next(
        (
            item for item in tools
            if str(getattr(item, "name", "") or getattr(item, "__name__", "") or "")
            == "document_compare"
        ),
        None,
    )
    if tool is None:
        return ""
    refs = list(getattr(rt, "attachment_refs", []) or [])
    args: dict[str, Any] = {}
    if len(refs) >= 2:
        args["left"] = f"attachment:{getattr(refs[0], 'attachment_id', '')}"
        args["right"] = f"attachment:{getattr(refs[1], 'attachment_id', '')}"
    elif len(refs) == 1:
        args["left"] = f"attachment:{getattr(refs[0], 'attachment_id', '')}"
    try:
        if callable(getattr(tool, "invoke", None)):
            result = tool.invoke(args)
        else:
            result = tool(**args)
    except Exception as exc:  # noqa: BLE001 - preflight is fail-open
        return f"比较预检失败：{type(exc).__name__}: {str(exc)[:300]}"
    return str(result or "").strip()


def should_buffer_answer(rt: Any) -> bool:
    return bool(
        task_settings.TASK_COMPLETION_GATE_ENABLED
        and getattr(rt, "task_plan", None) is not None
        and str(getattr(rt.task_plan, "task_kind", "")) == "compare"
    )


def handle_final_answer(
    rt: Any,
    *,
    answer_text: str,
    evidence: list[Any],
) -> tuple[str, str, CompletionDecision | None]:
    if getattr(rt, "task_plan", None) is None:
        return answer_text, "", None
    if str(getattr(rt.task_plan, "task_kind", "")) != "compare":
        return answer_text, "", None
    if not task_settings.TASK_COMPLETION_GATE_ENABLED:
        return answer_text, "", None
    answer_text = ensure_comparison_answer(rt, answer_text)
    artifact_id = ""
    if getattr(rt.task_plan, "delivery_scope", None) is not None:
        if rt.task_plan.delivery_scope.scope_kind == "full_report":
            report = _latest_report(rt)
            artifact_id = str(getattr(report, "report_id", "") or "")
    decision = finalize_task(
        rt, answer_text=answer_text, evidence=evidence, artifact_id=artifact_id,
    )
    note = completion_note(decision)
    if note:
        answer_text = (
            f"{answer_text.rstrip()}\n\n{note}" if answer_text else note
        )
    return answer_text, note, decision


def attach_observation(rt: Any, observation: ToolObservation) -> None:
    progress = getattr(rt, "task_progress", None)
    if progress is not None:
        progress.apply_observation(observation)


def record_comparison_page(rt: Any, report: Any, page: Any, admitted_items: Any = None) -> None:
    """Update scope coverage after a comparison page is admitted."""

    plan = getattr(rt, "task_plan", None)
    progress = getattr(rt, "task_progress", None)
    if plan is None or progress is None:
        return
    if plan.delivery_scope.scope_kind not in {
        "chapter_summary", "chapter_detail", "full_report", "item_detail",
    }:
        return
    selection = resolve_scope(report, plan.delivery_scope)
    coverage = progress.coverage
    coverage.scope_key = selection.scope_key
    coverage.report_id = str(report.report_id)
    coverage.report_hash = str(report.immutable_hash or "")
    coverage.warnings = tuple(str(item) for item in (report.warnings or ()))
    coverage.required_item_ids = selection.required_item_ids
    coverage.set_chapters(scope_chapter_item_ids(report, plan.delivery_scope))
    rendered_ids = {
        str(getattr(item, "metadata", {}).get("comparison_item_id") or "")
        for item in (page.evidence or ())
    }
    if admitted_items is not None:
        admitted_ids = {
            str(getattr(item, "metadata", {}).get("comparison_item_id") or "")
            for item in admitted_items
        }
        rendered_ids &= admitted_ids
    rendered_tuple = tuple(item for item in rendered_ids if item)
    coverage.mark_rendered(rendered_tuple)
    coverage.mark_admitted(rendered_tuple)
    coverage.next_cursor = str(getattr(page, "next_cursor", "") or "")
    progress.next_cursor = coverage.next_cursor
    for deliverable in plan.deliverables:
        if not deliverable.required:
            continue
        state = progress.deliverable_states[deliverable.deliverable_id]
        if deliverable.coverage_rule.rule_kind == "one_per_chapter":
            chapters = coverage.chapters
            satisfied = bool(chapters) and all(
                chapter.state == "satisfied" for chapter in chapters.values()
            )
            state.state = "satisfied" if satisfied else "partial"
            state.required_count = len(chapters)
            state.satisfied_count = sum(
                1 for chapter in chapters.values() if chapter.state == "satisfied"
            )
        elif deliverable.coverage_rule.rule_kind == "all_scope_items":
            state.state = "satisfied" if not coverage.missing_item_ids else "partial"
            state.required_count = len(coverage.required_item_ids)
            state.satisfied_count = len(coverage.rendered_item_ids)
            state.missing_item_ids = coverage.missing_item_ids
        elif deliverable.coverage_rule.rule_kind == "artifact_complete":
            state.state = "satisfied" if report else "pending"
    progress.status = "running"


def _sync_budget(rt: Any) -> None:
    budget = getattr(rt, "turn_budget", None)
    snapshot_fn = getattr(rt, "source_budget_snapshot", None)
    if budget is None or not callable(snapshot_fn):
        return
    try:
        snapshot = dict(snapshot_fn() or {})
        slices = dict(snapshot.get("slices") or {})
        comparison = int((slices.get("comparison") or {}).get("used_bytes") or 0)
        budget.note_evidence(
            evidence_bytes=int(snapshot.get("global_used_bytes") or 0),
            comparison_bytes=comparison,
        )
    except Exception:
        return


def _latest_report(rt: Any) -> Any | None:
    for item in reversed(list(getattr(rt, "evidence", []) or [])):
        metadata = dict(getattr(item, "metadata", None) or {})
        report_id = str(metadata.get("comparison_report_id") or "").strip()
        if not report_id:
            continue
        try:
            payload = DocumentComparisonStore().get(
                report_id,
                owner_user_id=getattr(rt, "attachment_user_id", None),
            )
            return comparison_report_from_dict(payload)
        except Exception:
            continue
    return None


def _chapter_summary_text(report: Any, plan: TaskPlan) -> str:
    groups = scope_chapter_groups(report, plan.delivery_scope)
    lines = ["## 章节概览（服务端依据冻结报告生成）"]
    for chapter_key, items in groups.items():
        changed = [item for item in items if item.kind != "unchanged"]
        if not changed:
            lines.append(f"- {chapter_key}: 未发现可解析变化")
            continue
        kinds: dict[str, int] = {}
        for item in changed:
            kinds[item.kind] = kinds.get(item.kind, 0) + 1
        kind_text = "、".join(f"{key} {value} 项" for key, value in kinds.items())
        key_points = "；".join(item.summary for item in changed[:2] if item.summary)
        lines.append(
            f"- {chapter_key}: 共 {len(changed)} 项变化（{kind_text}）"
            + (f"；关键项：{key_points}" if key_points else "")
        )
    if len(lines) == 1:
        lines.append("- 可解析范围内未发现变化")
    if getattr(report, "warnings", None):
        lines.append("- 警告：" + "；".join(str(item) for item in report.warnings[:5]))
    return "\n".join(lines)


def ensure_comparison_answer(rt: Any, answer_text: str) -> str:
    """Add deterministic scope coverage when the model omitted the summary.

    This is intentionally limited to the chapter-summary mode.  Detailed
    chapter pages still go through ``comparison_expand`` so citations and
    paging remain auditable.
    """

    plan = getattr(rt, "task_plan", None)
    progress = getattr(rt, "task_progress", None)
    if plan is None or progress is None:
        return answer_text
    if plan.delivery_scope.scope_kind not in {"chapter_summary", "full_report"}:
        return answer_text
    report = _latest_report(rt)
    if report is None:
        return answer_text
    if plan.delivery_scope.scope_kind == "full_report":
        coverage = progress.coverage
        coverage.report_id = str(report.report_id)
        coverage.report_hash = str(report.immutable_hash or "")
        coverage.scope_key = plan.delivery_scope.scope_key
        coverage.warnings = tuple(str(item) for item in (report.warnings or ()))
        coverage.required_item_ids = ()
        coverage.missing_item_ids = ()
        coverage.set_chapters(scope_chapter_item_ids(report, plan.delivery_scope))
        for chapter in coverage.chapters.values():
            chapter.covered_count = chapter.required_count
            chapter.state = "satisfied"
        for deliverable in plan.deliverables:
            state = progress.deliverable_states[deliverable.deliverable_id]
            if deliverable.kind == "artifact":
                state.state = "satisfied"
                state.required_count = 1
                state.satisfied_count = 1
            elif deliverable.coverage_rule.rule_kind == "one_per_chapter":
                state.state = "satisfied"
                state.required_count = len(coverage.chapters)
                state.satisfied_count = len(coverage.chapters)
        note = (
            "\n\n## 完整比较报告\n"
            f"report_id={report.report_id}；"
            f"共 {len(report.changed_items())} 项变化，完整正文和逐字 diff 已保存在冻结报告中。"
            f"可通过 `/api/comparisons/{report.report_id}` 或前端报告入口查看全部变化项。"
        )
        if "完整比较报告" not in str(answer_text or ""):
            answer_text = f"{str(answer_text or '').rstrip()}{note}" if str(answer_text or '').strip() else note.strip()
        return answer_text
    groups = scope_chapter_groups(report, plan.delivery_scope)
    answer_body = str(answer_text or "")
    if not groups or not all(str(key) in answer_body for key in groups):
        summary = _chapter_summary_text(report, plan)
        answer_text = (
            f"{answer_body.rstrip()}\n\n{summary}" if answer_body.strip() else summary
        )
    coverage = progress.coverage
    coverage.report_id = str(report.report_id)
    coverage.report_hash = str(report.immutable_hash or "")
    coverage.scope_key = plan.delivery_scope.scope_key
    coverage.warnings = tuple(str(item) for item in (report.warnings or ()))
    # Chapter summary is a chapter-level contract; item bodies are not
    # required and must not make the audit look partially delivered.
    coverage.required_item_ids = ()
    coverage.missing_item_ids = ()
    chapters = scope_chapter_item_ids(report, plan.delivery_scope)
    coverage.set_chapters(chapters)
    for chapter in coverage.chapters.values():
        chapter.covered_count = chapter.required_count
        chapter.state = "satisfied"
    for deliverable in plan.deliverables:
        if deliverable.coverage_rule.rule_kind == "one_per_chapter":
            state = progress.deliverable_states[deliverable.deliverable_id]
            state.state = "satisfied"
            state.required_count = len(coverage.chapters)
            state.satisfied_count = len(coverage.chapters)
    return answer_text


def finalize_task(
    rt: Any,
    *,
    answer_text: str,
    evidence: list[Any],
    artifact_id: str = "",
) -> CompletionDecision | None:
    """Build the answer manifest and run the completion gate."""

    plan = getattr(rt, "task_plan", None)
    progress = getattr(rt, "task_progress", None)
    if plan is None or progress is None or not task_settings.TASK_COMPLETION_GATE_ENABLED:
        return None
    _sync_budget(rt)
    manifest = build_answer_manifest(
        scope_key=plan.delivery_scope.scope_key,
        required_item_ids=progress.coverage.required_item_ids,
        coverage=progress.coverage,
        answer_text=answer_text,
        evidence=evidence,
        artifact_id=artifact_id,
    )
    progress.coverage.mark_cited(manifest.cited_item_ids)
    progress.answer_manifest = manifest
    progress.status = "verifying"
    decision = CompletionGate().evaluate(
        plan,
        progress,
        answer_text=answer_text,
        artifact_id=artifact_id,
        warnings=progress.coverage.warnings,
    )
    progress.status = decision.status if decision.status != "continue" else "running"
    progress.missing_item_ids = decision.missing_item_ids
    progress.stop_reason = "" if decision.status in {"complete", "complete_with_warnings"} else decision.reason
    return decision


def completion_note(decision: CompletionDecision | None) -> str:
    if decision is None or decision.status in {"complete", "complete_with_warnings"}:
        return ""
    lines = [f"任务完成状态：{decision.status}。"]
    if decision.missing_deliverables:
        lines.append("未完成交付项：" + "、".join(decision.missing_deliverables[:8]) + "。")
    if decision.missing_item_ids:
        lines.append(f"仍缺 {len(decision.missing_item_ids)} 个范围项。")
    if decision.reason:
        lines.append(f"原因：{decision.reason}。")
    if decision.coverage.get("next_cursor"):
        lines.append(f"可用 next_cursor 继续展开：{decision.coverage['next_cursor']}。")
    return "\n".join(lines)


def audit_fields(rt: Any) -> dict[str, Any]:
    plan = getattr(rt, "task_plan", None)
    progress = getattr(rt, "task_progress", None)
    budget = getattr(rt, "turn_budget", None)
    if plan is None and progress is None:
        return {}
    _sync_budget(rt)
    return {
        "task_plan": plan.model_dump(mode="json") if plan is not None else {},
        "task_progress": progress.snapshot() if progress is not None else {},
        "task_budget": budget.snapshot() if budget is not None else {},
    }


__all__ = [
    "attach_observation",
    "audit_fields",
    "completion_note",
    "create_compare_task",
    "ensure_comparison_answer",
    "finalize_task",
    "handle_final_answer",
    "prepare_compare_task_for_turn",
    "record_comparison_page",
    "run_comparison_preflight",
    "should_buffer_answer",
]
