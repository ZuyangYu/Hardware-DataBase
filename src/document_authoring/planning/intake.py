"""Conversation-led construction of versioned OutputSpec drafts."""

from __future__ import annotations

import copy
import json
import re
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


_OPTION_MARKER = re.compile(
    r"^\s*(?:选择|选)?\s*([A-Da-d])"
    r"(?:\s*(?:[.、:：,，)\]）-]\s*|\s+)(.*))?\s*$"
)

_GENERIC_OPTION_VALUES: dict[str, dict[str, str]] = {
    "document_type": {"a": "报告", "b": "评审表", "c": "测试记录"},
    "missing_data_policy": {
        "a": "mark_tbd", "b": "keep_blank", "c": "block_generation",
    },
    "inference_policy": {
        "a": "forbid", "b": "allow_labeled", "c": "allow_limited",
    },
    "approval_policy": {
        "a": "default-document-v1",
        "b": "machine-only-document-v1",
        "c": "custom-document-v1",
    },
}

_ALLOWED_UPDATE_FIELDS = frozenset({
    "purpose", "audience", "document_type", "artifact", "layout_source", "outline",
    "table_requirements", "source_scope", "target_identity", "language", "style",
    "missing_data_policy", "inference_policy", "approval_policy_id",
    "accepted_recommendations",
})


class OutputSpecIntakeService:
    """Keep conversational draft edits deterministic and schema-bound.

    A draft is a plain JSON-compatible mapping until all required decisions are
    present.  Only :meth:`to_output_spec` constructs the strict immutable
    planning contract; this lets the conversation ask one question at a time
    without inventing placeholder values that look user-confirmed.
    """

    _RECOMMENDATION_ID = "recommended_defaults_v1"
    _CUSTOM_POLICY_QUESTIONS_ID = "custom_policy_questions_v1"

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
        additional_requirements: list[Mapping[str, Any]] | None = None,
        clarification_answers: list[Mapping[str, Any]] | None = None,
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
            "additional_requirements": copy.deepcopy(list(additional_requirements or [])),
            "clarification_answers": copy.deepcopy(list(clarification_answers or [])),
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
        document_type = str(draft.get("document_type") or "").strip().casefold()
        layout_source = draft.get("layout_source")
        if (
            document_type == "icd"
            and isinstance(layout_source, Mapping)
            and layout_source.get("mode") == "provided_template"
            and not any(
                str(value or "").strip()
                for value in (draft.get("target_identity") or {}).values()
            )
        ):
            return IntakeQuestion(
                question_id="target_identity",
                prompt="这份 ICD 对应哪个硬件/总成？如已知，请同时提供连接器位号（例如 X1900）。",
                reason="示例模板可能包含旧产品数据，必须先确定生成对象，避免把模板示例当成知识库事实。",
            )
        policy_values_missing = (
            draft.get("missing_data_policy") not in {"mark_tbd", "keep_blank", "block_generation"}
            and draft.get("inference_policy") not in {"forbid", "allow_labeled", "allow_limited"}
            and not str(draft.get("approval_policy_id") or "").strip()
        )
        if policy_values_missing and self._CUSTOM_POLICY_QUESTIONS_ID not in set(
            draft.get("accepted_recommendations") or []
        ):
            return IntakeQuestion(
                question_id="recommendations",
                prompt="是否采用推荐的生成与审核策略？",
                options=["采用推荐方案", "逐项设置"],
                reason="可一次确认安全默认值，也可切换为逐项设置。",
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
        """Interpret and apply one complete clarification response."""

        return self.merge_user_answer(
            draft,
            expected_version=expected_version,
            question_id=question_id,
            answer=answer,
        )

    def merge_missing_data_resolution(
        self,
        draft: Mapping[str, Any],
        *,
        expected_version: int,
        answer: str,
    ) -> dict[str, Any]:
        """Record a worker-discovered evidence gap through the same CAS path.

        This question is emitted only after execution has found a required
        field without reliable evidence.  The answer stays conversational:
        policy choices update the frozen missing-data policy, while free text
        is retained as an auditable additional requirement for the next plan.
        """
        raw = str(answer or "").strip()
        if not raw:
            raise ValueError("missing-data resolution answer is required")
        lowered = raw.casefold()
        if "暂停" in raw or "等待资料" in raw:
            normalized = "block_generation"
            updates = {"missing_data_policy": normalized}
            additional: list[dict[str, Any]] = []
        elif "标记" in raw or "待提供" in raw or "tbd" in lowered:
            normalized = "mark_tbd"
            updates = {"missing_data_policy": normalized}
            additional = []
        elif "留空" in raw or "空白" in raw:
            normalized = "keep_blank"
            updates = {"missing_data_policy": normalized}
            additional = []
        else:
            normalized = "user_supplied_resolution"
            updates = {}
            additional = [{
                "field": "missing_data_resolution",
                "value": raw,
                "meaning": "用户补充的缺失字段处理说明",
                "raw_text": raw,
            }]
        return self.apply_interpreted_answer(
            draft,
            expected_version=expected_version,
            interpretation={
                "question_id": "missing_data_resolution",
                "raw_answer": raw,
                "normalized_answer": normalized,
                "updates": updates,
                "additional_requirements": additional,
                "ambiguities": [],
            },
        )

    def merge_user_answer(
        self,
        draft: Mapping[str, Any],
        *,
        expected_version: int,
        question_id: str,
        answer: str,
    ) -> dict[str, Any]:
        """Merge the current answer and explicit cross-field requirements."""

        interpretation = self.interpret_answer(
            draft,
            question_id=question_id,
            answer=answer,
        )
        return self.apply_interpreted_answer(
            draft,
            expected_version=expected_version,
            interpretation=interpretation,
        )

    def interpret_answer(
        self,
        draft: Mapping[str, Any],
        *,
        question_id: str,
        answer: str,
    ) -> dict[str, Any]:
        """Return a canonical, auditable interpretation without mutating a draft."""

        normalized_question = str(question_id or "").strip()
        raw_answer = str(answer or "").strip()
        if not normalized_question:
            raise ValueError("clarification question is required")
        if not raw_answer:
            raise ValueError("intake answer is required")

        option_key, option_text = self._option_marker(raw_answer)
        candidate = option_text if option_key is not None else raw_answer
        updates, additional = self._extract_additional_requirements(raw_answer)
        normalized_answer: str | None = None

        if normalized_question == "purpose":
            updates["purpose"] = raw_answer
            normalized_answer = raw_answer
        elif normalized_question == "document_type":
            normalized_answer = self._normalize_document_type(
                candidate,
                option_key=option_key,
            )
            if normalized_answer is not None:
                updates["document_type"] = normalized_answer
        elif normalized_question == "deliverables":
            deliverable_format = self._normalize_deliverable_format(
                candidate,
                option_key=option_key,
            )
            if deliverable_format is not None:
                normalized_answer = deliverable_format
                updates["artifact"] = {
                    "deliverables": [{
                        "format": deliverable_format,
                        "role": "primary",
                        "required": True,
                        "requested_by": "user",
                    }],
                }
        elif normalized_question == "layout_source":
            layout = self._layout_from_interpreted_answer(
                candidate,
                draft,
                option_key=option_key,
            )
            if layout is not None:
                normalized_answer = str(layout["mode"])
                updates["layout_source"] = layout
        elif normalized_question == "outline":
            updates["outline"] = self._outline_from_interpreted_answer(
                candidate,
                option_key=option_key,
            )
            normalized_answer = candidate
        elif normalized_question == "target_identity":
            if not candidate:
                raise ValueError(
                    "target_identity is a free-text question; provide the identity value"
                )
            normalized_answer = candidate
            updates["target_identity"] = self._combine_mapping_updates(
                updates.get("target_identity"),
                self._mapping_or_label(candidate),
            )
        elif normalized_question == "source_scope":
            if not candidate:
                raise ValueError(
                    "source_scope is a free-text question; provide the source scope"
                )
            normalized_answer = candidate
            updates["source_scope"] = self._combine_mapping_updates(
                updates.get("source_scope"),
                self._source_scope_from_answer(candidate),
            )
        elif normalized_question == "missing_data_policy":
            normalized_answer = self._normalize_missing(candidate, option_key=option_key)
            updates["missing_data_policy"] = normalized_answer
        elif normalized_question == "inference_policy":
            normalized_answer = self._normalize_inference(candidate, option_key=option_key)
            updates["inference_policy"] = normalized_answer
        elif normalized_question == "approval_policy":
            normalized_answer = self._normalize_approval(candidate, option_key=option_key)
            updates["approval_policy_id"] = normalized_answer
        elif normalized_question == "recommendations":
            normalized = candidate.strip().casefold()
            if option_key == "b" or normalized in {"逐项设置", "自定义", "custom", "customize"}:
                normalized_answer = self._CUSTOM_POLICY_QUESTIONS_ID
                accepted = list(draft.get("accepted_recommendations") or [])
                if self._CUSTOM_POLICY_QUESTIONS_ID not in accepted:
                    accepted.append(self._CUSTOM_POLICY_QUESTIONS_ID)
                updates["accepted_recommendations"] = accepted
            elif option_key == "a" or normalized in {
                "采用推荐方案", "use recommended", "accept recommendations",
            }:
                normalized_answer = self._RECOMMENDATION_ID
            else:
                raise ValueError("recommendations answer must select recommended defaults or itemized settings")
        elif raw_answer.casefold() in {
            "采用推荐方案", "use recommended", "accept recommendations",
        }:
            normalized_answer = self._RECOMMENDATION_ID
        else:
            raise ValueError(f"unknown output spec question: {normalized_question}")

        return {
            "question_id": normalized_question,
            "raw_answer": raw_answer,
            "normalized_answer": normalized_answer,
            "updates": updates,
            "additional_requirements": additional,
            "ambiguities": [],
        }

    def apply_interpreted_answer(
        self,
        draft: Mapping[str, Any],
        *,
        expected_version: int,
        interpretation: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate and commit an interpretation as one draft revision."""

        current_version = int(draft.get("version") or 0)
        if current_version != int(expected_version):
            raise OutputSpecVersionConflict(
                f"expected output spec version {expected_version}, current version is {current_version}"
            )
        if not isinstance(interpretation, Mapping):
            raise ValueError("clarification interpretation must be an object")
        normalized_question = str(interpretation.get("question_id") or "").strip()
        raw_answer = str(interpretation.get("raw_answer") or "").strip()
        normalized_answer = interpretation.get("normalized_answer")
        updates = interpretation.get("updates") or {}
        additional = interpretation.get("additional_requirements") or []
        ambiguities = [
            str(item).strip()
            for item in (interpretation.get("ambiguities") or [])
            if str(item).strip()
        ]
        if not normalized_question or not raw_answer:
            raise ValueError("clarification interpretation is missing question or answer")
        if not isinstance(updates, Mapping):
            raise ValueError("clarification updates must be an object")
        unknown_fields = sorted(set(str(field) for field in updates) - _ALLOWED_UPDATE_FIELDS)
        if unknown_fields:
            raise ValueError("unknown clarification update fields: " + ", ".join(unknown_fields))
        if ambiguities:
            raise ValueError("ambiguous clarification answer: " + "; ".join(ambiguities))
        if normalized_answer is None and not additional and not updates:
            raise ValueError(
                f"could not determine an answer for {normalized_question}; provide the option text"
            )

        updated = copy.deepcopy(dict(draft))
        if normalized_answer == self._RECOMMENDATION_ID:
            return self.accept_recommendation(
                updated,
                recommendation_id=self._RECOMMENDATION_ID,
                expected_version=current_version,
            )

        for field, value in updates.items():
            self._merge_update(updated, str(field), value)
        self._append_requirements(updated, additional)
        audit = list(updated.get("clarification_answers") or [])
        audit.append({
            "question_id": normalized_question,
            "raw_answer": raw_answer,
            "normalized_answer": normalized_answer,
            "updates": copy.deepcopy(dict(updates)),
            "additional_requirements": copy.deepcopy(list(additional)),
        })
        updated["clarification_answers"] = audit[-256:]
        updated["version"] = current_version + 1
        updated["confirmed"] = False
        return updated

    @staticmethod
    def _option_marker(value: str) -> tuple[str | None, str]:
        match = _OPTION_MARKER.match(value)
        if match is None:
            return None, value.strip()
        return match.group(1).lower(), (match.group(2) or "").strip()

    @staticmethod
    def _normalize_document_type(value: str, *, option_key: str | None) -> str | None:
        if option_key and not value.strip():
            normalized = _GENERIC_OPTION_VALUES["document_type"].get(option_key)
            if normalized is None:
                raise ValueError(f"invalid document-type option: {option_key.upper()}")
            return normalized
        text = value.strip()
        if not text:
            return None
        if "连接器管脚" in text or "管脚定义" in text:
            return "连接器管脚定义表"
        if any(term in text for term in ("摄像头", "雷达", "传感器")):
            return "ADAS 部件接口控制文档"
        if any(term in text for term in ("ERP", "物料号", "知识库", "自主规划", "实际功能")) and not any(
            term in text for term in ("报告", "评审", "测试", "文档", "表")
        ):
            # This is an extra requirement, not a document type answer.
            return _GENERIC_OPTION_VALUES["document_type"].get(option_key) if option_key else None
        return text

    @staticmethod
    def _normalize_deliverable_format(
        value: str,
        *,
        option_key: str | None,
    ) -> str | None:
        text = value.strip()
        lowered = text.casefold()
        formats = (
            ("docx", ("word", "docx")),
            ("xlsx", ("excel", "xlsx", "电子表")),
            ("xlsm", ("xlsm",)),
            ("pptx", ("ppt", "pptx", "演示")),
            ("pdf", ("pdf",)),
            ("markdown", ("markdown", "md")),
        )
        for fmt, terms in formats:
            if any(term in lowered or term in text for term in terms):
                return fmt
        if option_key and not text:
            # This mapping applies only to the server's concise format
            # question.  A rewritten A/B/C option with its label is handled
            # by the explicit text above and cannot guess a format.
            return {"a": "markdown", "b": "docx", "c": "xlsx"}.get(option_key)
        return None

    def _layout_from_interpreted_answer(
        self,
        value: str,
        draft: Mapping[str, Any],
        *,
        option_key: str | None,
    ) -> dict[str, str] | None:
        text = value.strip()
        lowered = text.casefold()
        if option_key and not text:
            if option_key == "a":
                existing = draft.get("layout_source")
                if isinstance(existing, Mapping) and existing.get("mode") == "provided_template":
                    return dict(existing)
                raise ValueError(
                    "provided-template layout requires a server-owned template reference"
                )
            if option_key == "b":
                return {
                    "mode": "system_recipe",
                    "recipe_id": "generic-report",
                    "recipe_version": "1",
                }
            if option_key == "c":
                return {
                    "mode": "generated_structure",
                    "constraints_profile_id": "generic-report",
                    "constraints_profile_version": "1",
                }
        if "模板" in text or "样例" in text or "template" in lowered:
            existing = draft.get("layout_source")
            if isinstance(existing, Mapping) and existing.get("mode") == "provided_template":
                return dict(existing)
            raise ValueError(
                "provided-template layout requires a server-owned template reference"
            )
        if "标准" in text or "recipe" in lowered:
            return {
                "mode": "system_recipe",
                "recipe_id": "generic-report",
                "recipe_version": "1",
            }
        if "受限" in text or ("生成" in text and "结构" in text):
            return {
                "mode": "generated_structure",
                "constraints_profile_id": "generic-report",
                "constraints_profile_version": "1",
            }
        if any(term in text for term in ("自主规划", "自主检索", "知识库", "内容引擎")):
            return {
                "mode": "generated_structure",
                "constraints_profile_id": "generic-report",
                "constraints_profile_version": "1",
            }
        if option_key:
            raise ValueError(
                f"invalid layout-source option text: {text}; provide the complete option"
            )
        if text:
            return {
                "mode": "generated_structure",
                "constraints_profile_id": "generic-report",
                "constraints_profile_version": "1",
            }
        return None

    def _outline_from_interpreted_answer(
        self,
        value: str,
        *,
        option_key: str | None,
    ) -> list[dict[str, Any]]:
        text = value.strip()
        if not text:
            if option_key:
                raise ValueError(
                    "outline is a free-text question; provide the option text or chapter names"
                )
            raise ValueError("outline must contain at least one chapter or field")
        if "仅一张" in text or ("核心" in text and "表" in text and "说明" not in text):
            names = ["核心管脚定义明细表"]
            kinds = ["table"]
        elif "表格为主" in text or "表头说明" in text or "图例" in text:
            names = ["核心管脚定义明细表", "表头说明/图例"]
            kinds = ["table", "section"]
        else:
            names = self._split_values(text)
            kinds = ["section"] * len(names)
        if not names:
            raise ValueError("outline must contain at least one chapter or field")
        return [
            {
                "unit_id": self._slug(name, index),
                "kind": kind,
                "title": name,
                "required": True,
            }
            for index, (name, kind) in enumerate(zip(names, kinds), start=1)
        ]

    @staticmethod
    def _normalize_approval(value: str, *, option_key: str | None) -> str:
        text = value.strip()
        if option_key and not text:
            normalized = _GENERIC_OPTION_VALUES["approval_policy"].get(option_key)
            if normalized is None:
                raise ValueError(f"invalid approval-policy option: {option_key.upper()}")
            return normalized
        lowered = text.casefold()
        if text in {"默认人工审核", "default"} or "人工审核" in text:
            return "default-document-v1"
        if text in {"仅机器校验", "machine-only"} or "机器校验" in text:
            return "machine-only-document-v1"
        if text == "自定义审批" or "自定义" in text:
            return "custom-document-v1"
        if lowered in _GENERIC_OPTION_VALUES["approval_policy"].values():
            return lowered
        if option_key:
            raise ValueError(f"invalid approval-policy option text: {text}")
        return OutputSpecIntakeService._slug(text, 0)

    @staticmethod
    def _merge_update(updated: dict[str, Any], field: str, value: Any) -> None:
        if field in {"target_identity", "source_scope", "style"} and isinstance(value, Mapping):
            existing = updated.get(field)
            merged = dict(existing) if isinstance(existing, Mapping) else {}
            for key, child in value.items():
                if key in merged and merged[key] != child:
                    raise ValueError(f"conflicting clarification update for {field}.{key}")
                merged[str(key)] = copy.deepcopy(child)
            updated[field] = merged
            return
        updated[field] = copy.deepcopy(value)

    @staticmethod
    def _combine_mapping_updates(
        existing: Any,
        incoming: Mapping[str, Any],
    ) -> dict[str, Any]:
        merged = dict(existing) if isinstance(existing, Mapping) else {}
        for key, value in incoming.items():
            if key in merged and merged[key] != value:
                raise ValueError(f"conflicting clarification update for {key}")
            merged[str(key)] = copy.deepcopy(value)
        return merged

    @staticmethod
    def _source_scope_from_answer(value: str) -> dict[str, Any]:
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping):
            return copy.deepcopy(dict(parsed))
        if "最新" in text:
            return {"version_policy": "latest"}
        if any(term in text for term in ("自主检索", "自主规划", "当前发布", "知识库")):
            return {"version_policy": "current_published"}
        return {"knowledge_bases": [text]}

    @staticmethod
    def _append_requirements(updated: dict[str, Any], values: Any) -> None:
        if not isinstance(values, (list, tuple)):
            raise ValueError("additional clarification requirements must be a list")
        requirements = list(updated.get("additional_requirements") or [])
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError("additional clarification requirement must be an object")
            item = {
                "field": str(value.get("field") or "").strip(),
                "value": value.get("value"),
                "meaning": str(value.get("meaning") or "").strip(),
                "raw_text": value.get("raw_text"),
            }
            if not item["field"]:
                raise ValueError("additional clarification requirement field is required")
            if not any(
                existing.get("field") == item["field"]
                and existing.get("value") == item["value"]
                and existing.get("meaning") == item["meaning"]
                for existing in requirements
                if isinstance(existing, Mapping)
            ):
                requirements.append(item)
        updated["additional_requirements"] = requirements[-256:]

    def _extract_additional_requirements(
        self,
        answer: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        text = answer.strip()
        updates: dict[str, Any] = {}
        requirements: list[dict[str, Any]] = []

        erp_match = re.search(
            r"(?:ERP|物料号)[^；;。\n]{0,40}?(?:先不填(?:写)?|暂不填(?:写)?|不填写|留空|未填写|不填)",
            text,
            flags=re.IGNORECASE,
        )
        if erp_match:
            requirements.append({
                "field": "target_identity.erp",
                "value": None,
                "meaning": "explicitly_unspecified",
                "raw_text": erp_match.group(0).strip("；;，, "),
            })

        function_match = re.search(
            r"[^；;。\n]{0,12}(?:按|根据)实际(?:情况和)?功能[^；;。\n]{0,12}",
            text,
        )
        if function_match:
            requirements.append({
                "field": "generation_basis",
                "value": "actual_function",
                "meaning": "use_actual_function",
                "raw_text": function_match.group(0).strip("；;，, "),
            })

        source_match = re.search(
            r"[^；;。\n]{0,12}知识库[^；;。\n]{0,20}(?:自主规划|自主检索|自行规划|自动检索)[^；;。\n]{0,12}",
            text,
        )
        if source_match or "知识库自主检索" in text or "知识库中内容自主规划" in text:
            updates["source_scope"] = {"version_policy": "current_published"}
            requirements.append({
                "field": "source_scope",
                "value": "knowledge_base_auto",
                "meaning": "allow_knowledge_base_planning_and_retrieval",
                "raw_text": (source_match.group(0) if source_match else "知识库自主检索").strip("；;，, "),
            })

        if "仅连接器管脚" in text or "仅管脚定义明细表" in text or "核心表格" in text:
            requirements.append({
                "field": "delivery_scope",
                "value": "core_table_only",
                "meaning": "deliver_core_pin_table_only",
                "raw_text": text,
            })
        elif "表格 + 前言" in text or "前言/编制说明" in text:
            requirements.append({
                "field": "delivery_scope",
                "value": "table_with_introduction",
                "meaning": "include_introduction_and_abbreviations",
                "raw_text": text,
            })

        outline_text = ""
        outline_match = re.search(
            r"(?:文档)?(?:章节|结构|章节范围)\s*(?:包括|为|是)?\s*[:：]?\s*"
            r"([^；;。\n]+)",
            text,
        )
        if outline_match:
            outline_text = outline_match.group(1).strip()
        elif any(term in text for term in ("仅一张核心", "表格为主", "表头说明", "图例")):
            outline_text = text
        if outline_text:
            updates["outline"] = self._outline_from_interpreted_answer(
                outline_text,
                option_key=None,
            )
            requirements.append({
                "field": "outline",
                "value": "explicit_outline",
                "meaning": "use_user_supplied_outline",
                "raw_text": outline_text,
            })

        return updates, requirements

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
        for key in (
            "status", "confirmed", "content_hash", "clarification_answers",
            "content_preview", "content_preview_confirmed",
        ):
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
    def _normalize_missing(value: str, *, option_key: str | None = None) -> str:
        text = value.strip()
        lowered = text.casefold()
        if option_key and not text:
            return _GENERIC_OPTION_VALUES["missing_data_policy"][option_key]
        recognized = (
            text in {"标记未提供", "mark_tbd", "tbd", "use_default", "mark_unknown"}
            or any(
                term in text or term in lowered
                for term in (
                    "待补充", "数据缺失", "保留占位", "明确标注",
                    "默认值", "占位", "待确认", "未知", "unknown",
                )
            )
        )
        if recognized:
            return "mark_tbd"
        recognized = (
            text in {"保留空白", "keep_blank", "blank", "leave_blank"}
            or any(
                term in text or term in lowered
                for term in ("跳过缺失", "仅输出已有", "跳过未找到", "留空", "留白")
            )
        )
        if recognized:
            return "keep_blank"
        if (
            text in {"停止并提示", "block_generation", "block", "abort"}
            or any(term in text for term in ("停止", "报错", "中止"))
        ):
            return "block_generation"
        if lowered in _GENERIC_OPTION_VALUES["missing_data_policy"].values():
            return lowered
        if option_key and (
            not text
            or any(term in text for term in (
                "ERP", "物料号", "知识库", "自主", "实际功能", "结构", "章节",
            ))
        ):
            normalized = _GENERIC_OPTION_VALUES["missing_data_policy"].get(option_key)
            if normalized is not None:
                return normalized
            raise ValueError(f"invalid missing-data policy option: {option_key.upper()}")
        raise ValueError(f"invalid missing-data policy answer: {text}")

    @staticmethod
    def _normalize_inference(value: str, *, option_key: str | None = None) -> str:
        text = value.strip()
        if option_key and not text:
            return _GENERIC_OPTION_VALUES["inference_policy"][option_key]
        if text in {"禁止推断", "forbid", "不允许"} or any(
            term in text for term in ("禁止", "不推断", "不进行推断")
        ):
            return "forbid"
        if text in {"允许但必须标注", "allow_labeled"} or (
            "推断" in text and any(term in text for term in ("标注", "注明", "加注"))
        ):
            return "allow_labeled"
        if text in {"允许有限推断", "allow_limited"} or (
            "有限" in text and "推断" in text
        ):
            return "allow_limited"
        if option_key and (
            not text
            or any(term in text for term in (
                "ERP", "物料号", "知识库", "自主", "实际功能", "结构", "章节",
            ))
        ):
            normalized = _GENERIC_OPTION_VALUES["inference_policy"].get(option_key)
            if normalized is not None:
                return normalized
            raise ValueError(f"invalid inference-policy option: {option_key.upper()}")
        raise ValueError(f"invalid inference-policy answer: {text}")


__all__ = ["IntakeQuestion", "OutputSpecIntakeService", "OutputSpecVersionConflict"]
