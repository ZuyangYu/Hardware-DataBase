"""Conversation-led construction of versioned OutputSpec drafts."""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from pydantic import BaseModel, Field

from .models import OutputSpec


class OutputSpecVersionConflict(ValueError):
    """Raised when a clarification answer was based on an old draft version."""


class IntakeQuestion(BaseModel):
    question_id: str
    prompt: str
    options: list[str] = Field(default_factory=list, max_length=3)
    reason: str = ""
    free_text_allowed: bool = True


class OutputSpecIntakeService:
    """Keep conversational draft edits deterministic and schema-bound.

    A draft is a plain JSON-compatible mapping until all required decisions are
    present.  Only :meth:`to_output_spec` constructs the strict immutable
    planning contract; this lets the conversation ask one question at a time
    without inventing placeholder values that look user-confirmed.
    """

    _RECOMMENDATION_ID = "recommended_defaults_v1"

    def start_draft(
        self,
        *,
        output_spec_id: str,
        version: int = 1,
        purpose: str | None = None,
        audience: list[str] | None = None,
        document_type: str | None = None,
        deliverables: list[Mapping[str, Any]] | None = None,
        artifact: Mapping[str, Any] | None = None,
        layout_source: Mapping[str, Any] | None = None,
        template_version_id: str | None = None,
        template_schema_id: str | None = None,
        template_schema_version: str | None = None,
        outline: list[Mapping[str, Any]] | None = None,
        table_requirements: list[Mapping[str, Any]] | None = None,
        source_scope: Mapping[str, Any] | None = None,
        target_identity: Mapping[str, Any] | None = None,
        language: str = "en-US",
        style: Mapping[str, Any] | None = None,
        missing_data_policy: str | None = None,
        inference_policy: str | None = None,
        approval_policy_id: str | None = None,
        accepted_recommendations: list[str] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        draft: dict[str, Any] = {
            "output_spec_id": str(output_spec_id).strip(),
            "version": int(version),
            "status": "draft",
            "purpose": str(purpose).strip() if purpose is not None else None,
            "audience": list(audience or []),
            "document_type": str(document_type).strip() if document_type is not None else None,
            "outline": copy.deepcopy(outline) if outline is not None else None,
            "table_requirements": copy.deepcopy(table_requirements or []),
            "source_scope": copy.deepcopy(source_scope) if source_scope is not None else None,
            "target_identity": copy.deepcopy(target_identity or {}),
            "language": language,
            "style": copy.deepcopy(style or {}),
            "missing_data_policy": missing_data_policy,
            "inference_policy": inference_policy,
            "approval_policy_id": approval_policy_id,
            "accepted_recommendations": list(accepted_recommendations or []),
            "confirmed": False,
        }
        if artifact is not None:
            draft["artifact"] = copy.deepcopy(dict(artifact))
        elif deliverables is not None:
            draft["artifact"] = {"deliverables": copy.deepcopy(list(deliverables))}
        else:
            draft["artifact"] = None
        if layout_source is not None:
            draft["layout_source"] = copy.deepcopy(dict(layout_source))
        elif all(
            str(value or "").strip()
            for value in (template_version_id, template_schema_id, template_schema_version)
        ):
            draft["layout_source"] = {
                "mode": "provided_template",
                "template_version_id": str(template_version_id).strip(),
                "template_schema_id": str(template_schema_id).strip(),
                "template_schema_version": str(template_schema_version).strip(),
            }
        else:
            draft["layout_source"] = None
        if extra:
            draft.update(copy.deepcopy(extra))
        if not draft["output_spec_id"]:
            raise ValueError("output_spec_id is required")
        if draft["version"] < 1:
            raise ValueError("draft version must be positive")
        return draft

    def next_question(self, draft: Mapping[str, Any]) -> IntakeQuestion | None:
        if not str(draft.get("purpose") or "").strip():
            return IntakeQuestion(
                question_id="purpose",
                prompt="这份文档要解决什么问题，主要给谁使用？",
                reason="用途和受众会决定内容取舍。",
            )
        if not str(draft.get("document_type") or "").strip():
            return IntakeQuestion(
                question_id="document_type",
                prompt="需要生成哪一种文档？",
                options=["报告", "评审表", "测试记录"],
                reason="文档类型决定结构和校验规则。",
            )
        artifact = draft.get("artifact")
        deliverables = artifact.get("deliverables") if isinstance(artifact, Mapping) else None
        if not deliverables:
            return IntakeQuestion(
                question_id="deliverables",
                prompt="希望交付什么格式？",
                options=["Markdown", "Word", "Excel"],
                reason="交付格式会决定可用的布局和渲染器。",
            )
        if not isinstance(draft.get("layout_source"), Mapping):
            return IntakeQuestion(
                question_id="layout_source",
                prompt="使用现有模板，还是按标准结构生成？",
                options=["使用现有模板", "采用标准结构", "生成受限结构"],
                reason="布局来源决定模板绑定或结构能力。",
            )
        if not draft.get("outline"):
            return IntakeQuestion(
                question_id="outline",
                prompt="需要包含哪些章节或字段？可直接输入名称列表。",
                reason="章节范围决定计划中的语义单元。",
            )
        if draft.get("missing_data_policy") not in {"mark_tbd", "keep_blank", "block_generation"}:
            return IntakeQuestion(
                question_id="missing_data_policy",
                prompt="找不到可靠资料时如何处理？",
                options=["标记未提供", "保留空白", "停止并提示"],
                reason="缺失数据策略决定是否允许继续生成。",
            )
        if draft.get("inference_policy") not in {"forbid", "allow_labeled", "allow_limited"}:
            return IntakeQuestion(
                question_id="inference_policy",
                prompt="是否允许基于证据进行推断？",
                options=["禁止推断", "允许但必须标注", "允许有限推断"],
                reason="推断策略决定内容边界。",
            )
        if not str(draft.get("approval_policy_id") or "").strip():
            return IntakeQuestion(
                question_id="approval_policy",
                prompt="采用哪种审批策略？",
                options=["默认人工审核", "仅机器校验", "自定义审批"],
                reason="审批策略决定发布前的人工门禁。",
            )
        return None

    def merge_answer(
        self,
        draft: Mapping[str, Any],
        *,
        expected_version: int,
        question_id: str,
        answer: str,
    ) -> dict[str, Any]:
        current_version = int(draft.get("version") or 0)
        if current_version != int(expected_version):
            raise OutputSpecVersionConflict(
                f"expected output spec version {expected_version}, current version is {current_version}"
            )
        value = str(answer or "").strip()
        if not value:
            raise ValueError("intake answer is required")
        updated = copy.deepcopy(dict(draft))
        if question_id == "purpose":
            updated["purpose"] = value
        elif question_id == "document_type":
            updated["document_type"] = value
        elif question_id == "deliverables":
            updated["artifact"] = {"deliverables": [self._deliverable_from_answer(value)]}
        elif question_id == "layout_source":
            updated["layout_source"] = self._layout_from_answer(value, updated)
        elif question_id == "outline":
            names = self._split_values(value)
            updated["outline"] = [
                {"unit_id": self._slug(name, index), "kind": "section", "title": name, "required": True}
                for index, name in enumerate(names, start=1)
            ]
        elif question_id == "target_identity":
            updated["target_identity"] = self._mapping_or_label(value)
        elif question_id == "source_scope":
            updated["source_scope"] = self._mapping_or_label(value)
        elif question_id == "missing_data_policy":
            updated["missing_data_policy"] = self._normalize_missing(value)
        elif question_id == "inference_policy":
            updated["inference_policy"] = self._normalize_inference(value)
        elif question_id == "approval_policy":
            updated["approval_policy_id"] = "default-document-v1" if value in {"默认人工审核", "default"} else self._slug(value, 0)
        elif question_id == "recommendations" or value.casefold() in {"采用推荐方案", "use recommended", "accept recommendations"}:
            return self.accept_recommendation(updated, recommendation_id=self._RECOMMENDATION_ID, expected_version=current_version)
        else:
            raise ValueError(f"unknown output spec question: {question_id}")
        updated["version"] = current_version + 1
        updated["confirmed"] = False
        return updated

    def recommendations(self, draft: Mapping[str, Any]) -> list[dict[str, Any]]:
        del draft
        return [{
            "id": self._RECOMMENDATION_ID,
            "label": "按当前发布资料生成，缺失标记为未提供，禁止无证据推断",
            "values": {
                "missing_data_policy": "mark_tbd",
                "inference_policy": "forbid",
                "approval_policy_id": "default-document-v1",
            },
        }]

    def accept_recommendation(
        self,
        draft: Mapping[str, Any],
        *,
        recommendation_id: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        current_version = int(draft.get("version") or 0)
        if expected_version is not None and current_version != expected_version:
            raise OutputSpecVersionConflict(
                f"expected output spec version {expected_version}, current version is {current_version}"
            )
        recommendation = next(
            (item for item in self.recommendations(draft) if item["id"] == recommendation_id),
            None,
        )
        if recommendation is None:
            raise ValueError("unknown output spec recommendation")
        updated = copy.deepcopy(dict(draft))
        updated.update(copy.deepcopy(recommendation["values"]))
        accepted = list(updated.get("accepted_recommendations") or [])
        if recommendation_id not in accepted:
            accepted.append(recommendation_id)
        updated["accepted_recommendations"] = accepted
        updated["version"] = current_version + 1
        updated["confirmed"] = False
        return updated

    @staticmethod
    def is_complete(draft: Mapping[str, Any]) -> bool:
        artifact = draft.get("artifact")
        return bool(
            str(draft.get("purpose") or "").strip()
            and str(draft.get("document_type") or "").strip()
            and isinstance(artifact, Mapping)
            and artifact.get("deliverables")
            and isinstance(draft.get("layout_source"), Mapping)
            and draft.get("outline")
            and draft.get("missing_data_policy") in {"mark_tbd", "keep_blank", "block_generation"}
            and draft.get("inference_policy") in {"forbid", "allow_labeled", "allow_limited"}
            and str(draft.get("approval_policy_id") or "").strip()
        )

    @staticmethod
    def to_output_spec(draft: Mapping[str, Any]) -> OutputSpec:
        if not OutputSpecIntakeService.is_complete(draft):
            raise ValueError("output spec draft is incomplete")
        payload = copy.deepcopy(dict(draft))
        for key in ("status", "confirmed", "content_hash"):
            payload.pop(key, None)
        if payload.get("source_scope") is None:
            payload["source_scope"] = {}
        return OutputSpec.model_validate(payload)

    @staticmethod
    def _split_values(value: str) -> list[str]:
        values = [item.strip() for item in value.replace("，", ",").replace("、", ",").split(",")]
        return [item for item in values if item][:100]

    @staticmethod
    def _slug(value: str, index: int) -> str:
        text = "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
        return text[:120] or f"unit-{index + 1}"

    @staticmethod
    def _mapping_or_label(value: str) -> dict[str, str]:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping):
            return {str(key): str(item) for key, item in parsed.items() if str(item).strip()}
        return {"value": value}

    @staticmethod
    def _deliverable_from_answer(value: str) -> dict[str, str | bool]:
        lowered = value.casefold()
        if "word" in lowered or "docx" in lowered:
            fmt = "docx"
        elif "excel" in lowered or "xlsx" in lowered:
            fmt = "xlsx"
        elif "pdf" in lowered:
            fmt = "pdf"
        else:
            fmt = "markdown"
        return {"format": fmt, "role": "primary", "required": True, "requested_by": "user"}

    @staticmethod
    def _layout_from_answer(value: str, draft: Mapping[str, Any]) -> dict[str, str]:
        lowered = value.casefold()
        if "模板" in value or "template" in lowered:
            existing = draft.get("layout_source")
            if isinstance(existing, Mapping) and existing.get("mode") == "provided_template":
                return dict(existing)
            raise ValueError("provided-template layout requires a server-owned template reference")
        if "标准" in value or "recipe" in lowered:
            return {"mode": "system_recipe", "recipe_id": "generic-report", "recipe_version": "1"}
        return {
            "mode": "generated_structure",
            "constraints_profile_id": "generic-report",
            "constraints_profile_version": "1",
        }

    @staticmethod
    def _normalize_missing(value: str) -> str:
        if value in {"标记未提供", "mark_tbd", "tbd"}:
            return "mark_tbd"
        if value in {"保留空白", "keep_blank", "blank"}:
            return "keep_blank"
        if value in {"停止并提示", "block_generation", "block"}:
            return "block_generation"
        raise ValueError("invalid missing-data policy")

    @staticmethod
    def _normalize_inference(value: str) -> str:
        if value in {"禁止推断", "forbid", "不允许"}:
            return "forbid"
        if value in {"允许但必须标注", "allow_labeled"}:
            return "allow_labeled"
        if value in {"允许有限推断", "allow_limited"}:
            return "allow_limited"
        raise ValueError("invalid inference policy")


__all__ = ["IntakeQuestion", "OutputSpecIntakeService", "OutputSpecVersionConflict"]
