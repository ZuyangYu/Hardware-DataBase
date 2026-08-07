"""Minimum-question clarification policy for document generation briefs."""

from __future__ import annotations

from typing import Any

from src.document_authoring.generation_sessions import (
    ClarificationMessage,
    GenerationBrief,
)


class RequirementClarifier:
    """Ask only decisions that materially change the generated document."""

    def next_message(
        self,
        template_analysis: dict[str, Any],
        brief: GenerationBrief,
    ) -> ClarificationMessage:
        if not str(brief.scope.get("revision", "")).strip():
            return ClarificationMessage(
                role="assistant",
                question_id="scope.revision",
                content="请确认本次文档应使用哪个项目或资料版本？",
                options=["当前发布版本", "最新上传版本", "指定其他版本"],
                reason="版本范围会决定检索使用的事实基线。",
            )
        if not brief.missing_data_policy:
            return ClarificationMessage(
                role="assistant",
                question_id="missing_data_policy",
                content="检索不到可靠资料的字段应如何处理？",
                options=["标记未提供", "保留空白", "停止并提示"],
                reason="缺失数据策略决定文档是否可以继续生成。",
            )
        if not brief.inference_policy:
            return ClarificationMessage(
                role="assistant",
                question_id="inference_policy",
                content="是否允许 AI 根据现有证据进行有限推断？",
                options=["禁止推断", "允许但必须标注", "允许有限推断"],
                reason="推断策略决定 Writer 可以生成的内容边界。",
            )

        template_format = str(
            brief.output_policy.get("format") or template_analysis.get("format") or "文档",
        )
        return ClarificationMessage(
            role="assistant",
            content=f"需求已经明确，将按已确认范围生成 {template_format} 文档。",
            reason="ready_to_generate",
        )

    def apply_answer(
        self,
        brief: GenerationBrief,
        *,
        question_id: str,
        answer: str,
    ) -> GenerationBrief:
        normalized = answer.strip()
        if not normalized:
            raise ValueError("clarification answer is required")

        updates: dict[str, Any]
        if question_id == "scope.revision":
            updates = {"scope": {**brief.scope, "revision": normalized}}
        elif question_id == "missing_data_policy":
            updates = {"missing_data_policy": normalized}
        elif question_id == "inference_policy":
            updates = {"inference_policy": normalized}
        else:
            raise ValueError("unknown clarification question")

        answered = sum([
            bool(brief.scope.get("revision")),
            bool(brief.missing_data_policy),
            bool(brief.inference_policy),
        ]) + 1
        updates["confidence"] = min(1.0, answered / 3)
        return brief.model_copy(update=updates)
