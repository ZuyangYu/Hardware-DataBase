"""Server-side adapters from validated action specs to governed business tools.

The adapter has no filesystem or framework privileges of its own.  It may only
invoke tools already assembled for the authenticated turn, which means the
same ACL, source-snapshot, idempotency, and audit checks apply whether an
action came from a model plan or from a card button.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.agents.orchestration.contracts import ActionReceipt, ActionSpec
from src.attachments.models import AttachmentError


_SUCCESS = {"succeeded", "success", "ok", "completed", "complete"}
_ACCEPTED = {"accepted", "queued", "running", "waiting_human", "waiting"}
_BLOCKED = {"blocked", "unavailable"}
_REFERENCE_KEYS = {
    "task_id": "task",
    "work_order_id": "work_order",
    "run_id": "run",
    "artifact_id": "artifact",
    "attachment_id": "attachment",
    "parent_attachment_id": "attachment_parent",
    "child_attachment_id": "attachment_child",
    "revision_id": "revision",
    "generation_session_id": "generation_session",
}


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", "") or getattr(tool, "__name__", "")).strip()


def _as_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {"status": "completed", "message": value}
        return dict(parsed) if isinstance(parsed, Mapping) else {"status": "completed", "data": parsed}
    return {"status": "completed", "data": value}


def _refs_from_payload(payload: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for key, prefix in _REFERENCE_KEYS.items():
        value = str(payload.get(key) or "").strip()
        if value:
            refs.append(f"{prefix}:{value}")
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        for key, prefix in _REFERENCE_KEYS.items():
            value = str(nested.get(key) or "").strip()
            ref = f"{prefix}:{value}" if value else ""
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def _outcome(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("status") or "completed").strip().casefold()
    if raw in _SUCCESS:
        status = "completed"
    elif raw in _ACCEPTED:
        status = "accepted"
    elif raw in _BLOCKED:
        status = "blocked"
    elif raw in {"rejected", "denied"}:
        status = "rejected"
    elif raw in {"failed", "error"}:
        status = "failed"
    else:
        status = "completed"
    return {
        "status": status,
        "result_refs": _refs_from_payload(payload),
        "error_code": str(payload.get("error_code") or ""),
        "message": str(payload.get("message") or ""),
        "data": dict(payload),
    }


def _reference_value(refs: Iterable[str], prefix: str) -> str:
    needle = f"{prefix}:"
    for ref in refs:
        value = str(ref or "").strip()
        if value.startswith(needle):
            return value[len(needle):].strip()
    return ""


@dataclass
class GovernedToolActionAdapters:
    """Adapt plan actions to the controlled tools visible in a single turn."""

    tools: Iterable[Any]
    query: str
    document_context: Any | None = None
    # This projection is resolved by the authenticated server context, never
    # copied from the model plan.  It lets a follow-up such as “修改第三章”
    # bind the task's current candidate after a browser refresh, even though
    # the client-side v2 context intentionally carries no artifact authority.
    document_projection: Mapping[str, Any] | None = None
    # Governed attachment proposals: the executor may create a confirmable
    # proposal, but only the confirmation path writes a candidate.  These
    # values come from the authenticated turn, never from the model plan.
    attachment_service: Any | None = None
    attachment_session_id: int | None = None
    attachment_user_id: int | None = None
    evidence_ids: frozenset[str] = frozenset()
    _tools_by_name: dict[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tools_by_name = {
            name: tool
            for tool in self.tools
            if (name := _tool_name(tool))
        }

    def handlers(self) -> dict[str, Any]:
        """Return handlers suitable for :class:`ConversationActionExecutor`."""

        return {
            "search": self.search,
            "inspect": self.search,
            "compare": self.search,
            "summarize": self.search,
            "create_document": self.create_document,
            "revise_document": self.revise_document,
            "revise_attachment": self.revise_attachment,
            "get_status": self.get_status,
            "pause": self._task_control("pause_document_task"),
            "cancel": self._task_control("cancel_document_task"),
            "resume": self.resume,
            "retry": self._task_control("retry_document_task"),
            "rebase_sources": self._task_control("rebase_document_task_sources"),
            "request_release": self.request_release,
            "resolve_decision": self.resolve_decision,
            "export_result": self.export_result,
        }

    def _invoke(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        tool = self._tools_by_name.get(name)
        if tool is None:
            return {
                "status": "blocked",
                "error_code": "tool_not_available",
                "message": f"the authorized tool {name} is not available",
            }
        invoke = getattr(tool, "invoke", None)
        if callable(invoke):
            return _as_payload(invoke(dict(arguments)))
        if callable(tool):
            return _as_payload(tool(**dict(arguments)))
        return {
            "status": "blocked",
            "error_code": "tool_not_invocable",
            "message": f"the authorized tool {name} cannot be invoked",
        }

    @staticmethod
    def _blocked(code: str, message: str) -> dict[str, Any]:
        return {"status": "blocked", "error_code": code, "message": message}

    def search(self, action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        """Retrieve through the frozen source scope, never a raw file read."""

        parameters = dict(action.parameters or {})
        scope = str(parameters.get("source_scope") or "auto").strip()
        query = str(parameters.get("query") or self.query or "").strip()
        if not query:
            return self._blocked("search_query_missing", "a search action requires a query")
        if scope == "attachment_only":
            names = ["attachment_search"]
        elif scope == "knowledge_base_only":
            names = ["document_search"]
        else:
            names = ["attachment_search", "document_search"]

        payloads: list[dict[str, Any]] = []
        for name in names:
            if name not in self._tools_by_name:
                continue
            payloads.append(self._invoke(name, {"query": query}))
        if not payloads:
            return self._blocked("search_tool_unavailable", "no authorized source search tool is available")
        if all(str(item.get("status") or "").casefold() in _BLOCKED for item in payloads):
            first = payloads[0]
            return self._blocked(
                str(first.get("error_code") or "search_unavailable"),
                str(first.get("message") or "authorized search is unavailable"),
            )
        refs: list[str] = []
        for payload in payloads:
            refs.extend(ref for ref in _refs_from_payload(payload) if ref not in refs)
        return {
            "status": "completed",
            "result_refs": refs,
            "message": "scoped evidence retrieved",
            "data": {"source_scope": scope, "results": payloads},
        }

    def create_document(self, action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        """Start candidate generation only through the document service."""

        parameters = dict(action.parameters or {})
        output_format = str(parameters.get("format") or "docx").strip().lower()
        if "generate_document_from_template" in self._tools_by_name:
            return _outcome(self._invoke("generate_document_from_template", {
                "purpose": str(parameters.get("purpose") or self.query or "").strip(),
                "output_format": output_format if output_format else "docx",
                "use_recommended_defaults": True,
            }))
        if "start_document_generation_session" in self._tools_by_name:
            return _outcome(self._invoke("start_document_generation_session", {
                "purpose": str(parameters.get("purpose") or self.query or "").strip(),
            }))
        return self._blocked(
            "document_authoring_unavailable",
            "no authorized document-generation tool is available",
        )

    def revise_document(self, action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        """Create an immutable child-revision request; never edit a source file."""

        if "create_document_revision" not in self._tools_by_name:
            return self._blocked("revision_tool_unavailable", "no authorized revision tool is available")
        parameters = dict(action.parameters or {})
        refs = [action.target_ref or "", *action.input_refs]
        task_id = str(parameters.get("task_id") or _reference_value(refs, "task") or getattr(
            self.document_context, "task_id", ""
        ) or (self.document_projection or {}).get("task_id") or "").strip()
        parent_artifact_id = str(
            parameters.get("parent_artifact_id")
            or _reference_value(refs, "artifact")
            or getattr(self.document_context, "candidate_artifact_id", "")
            or getattr(self.document_context, "artifact_id", "")
            or (self.document_projection or {}).get("current_artifact_id")
            or ""
        ).strip()
        def _unique_strings(value: Any) -> list[str]:
            if isinstance(value, str):
                values = [value]
            elif isinstance(value, (list, tuple, set)):
                values = list(value)
            else:
                values = []
            return list(dict.fromkeys(
                str(item).strip() for item in values if str(item).strip()
            ))

        sections = _unique_strings(parameters.get("changed_sections"))
        sections.extend(
            item for item in (
                ref.split(":", 1)[1]
                for ref in refs
                if str(ref).startswith("section:")
                and str(ref).split(":", 1)[1].strip()
            )
            if item not in sections
        )
        fields = _unique_strings(parameters.get("changed_fields"))
        fields.extend(
            item for item in (
                ref.split(":", 1)[1]
                for ref in refs
                if str(ref).startswith("field:")
                and str(ref).split(":", 1)[1].strip()
            )
            if item not in fields
        )
        if not task_id or not parent_artifact_id:
            return self._blocked(
                "revision_target_unresolved",
                "the revision requires both a task and a candidate artifact reference",
            )
        request_type = str(parameters.get("request_type") or "").strip()
        if request_type not in {"field_update", "section_update", "full_regeneration"}:
            request_type = "section_update" if sections else "field_update"
        return _outcome(self._invoke("create_document_revision", {
            "task_id": task_id,
            "parent_artifact_id": parent_artifact_id,
            "request_type": request_type,
            "request": str(parameters.get("request") or self.query or "").strip(),
            "changed_fields": fields,
            "changed_sections": sections,
            "client_request_id": str(parameters.get("client_request_id") or f"conversation-action:{action.action_id}"),
        }))

    def revise_attachment(self, action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        """Propose a section rewrite or execute one exact-text replacement."""

        parameters = dict(action.parameters or {})
        if str(parameters.get("operation") or "").strip().lower() == "replace_section":
            return self._propose_attachment_section(action, parameters)

        if "attachment_edit" not in self._tools_by_name:
            return self._blocked(
                "attachment_revision_tool_unavailable",
                "no authorized attachment revision tool is available",
            )
        # Never trust model-supplied before/after values.  The server parses
        # the original user turn and passes only that exact request to the
        # attachment tool, which performs occurrence and ACL checks again.
        from src.document_authoring.planning.revision import parse_revision_text_edit

        edit = parse_revision_text_edit(self.query)
        if edit is None:
            return self._blocked(
                "attachment_edit_requires_exact_text",
                "an attachment edit requires one unambiguous quoted text replacement",
            )
        parameters = dict(action.parameters or {})
        refs = [action.target_ref or "", *action.input_refs]
        attachment_id = str(
            parameters.get("attachment_id")
            or _reference_value(refs, "attachment")
            or ""
        ).strip()
        return _outcome(self._invoke("attachment_edit", {
            "before": edit.before,
            "after": edit.after,
            "attachment_id": attachment_id,
            "client_request_id": str(
                parameters.get("client_request_id")
                or f"conversation-action:{action.action_id}"
            ),
        }))

    def _propose_attachment_section(
        self, action: ActionSpec, parameters: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create a confirmable section proposal; never write a candidate."""

        if (
            self.attachment_service is None
            or not self.attachment_session_id
            or not self.attachment_user_id
        ):
            return self._blocked(
                "attachment_proposal_unavailable",
                "当前轮没有可用的附件服务或会话，无法创建章节提案",
            )
        heading_ref = [
            str(item).strip()
            for item in (parameters.get("heading_path") or [])
            if str(item).strip()
        ]
        if not heading_ref:
            return self._blocked(
                "attachment_proposal_heading_missing",
                "章节提案必须给出 heading_path 标题",
            )
        after = str(parameters.get("after") or "").strip()
        if not after:
            return self._blocked(
                "attachment_proposal_content_missing",
                "章节提案必须给出 after 正文",
            )
        evidence_refs = [
            str(item) for item in (parameters.get("evidence_refs") or []) if str(item)
        ]
        if not evidence_refs:
            return self._blocked(
                "attachment_proposal_evidence_missing",
                "章节提案必须引用本轮证据 evidence_refs",
            )
        unknown = [ref for ref in evidence_refs if ref not in self.evidence_ids]
        if unknown:
            return self._blocked(
                "attachment_proposal_evidence_unresolved",
                f"证据引用不在本轮证据中: {', '.join(unknown[:5])}",
            )
        refs = [action.target_ref or "", *action.input_refs]
        attachment_id = str(
            parameters.get("attachment_id")
            or _reference_value(refs, "attachment")
            or ""
        ).strip()
        if not attachment_id:
            return self._blocked(
                "attachment_target_unresolved",
                "章节提案需要 attachment_id 或 attachment 引用",
            )
        try:
            proposal = self.attachment_service.create_attachment_edit_proposal(
                session_id=int(self.attachment_session_id),
                user_id=int(self.attachment_user_id),
                parent_attachment_id=attachment_id,
                operation="replace_section",
                heading_path=heading_ref,
                after=after,
                before=str(parameters.get("before") or ""),
                evidence_refs=evidence_refs,
                note=str(parameters.get("request") or self.query or ""),
            )
        except (ValueError, AttachmentError) as exc:
            return self._blocked("attachment_proposal_rejected", str(exc)[:500])
        except Exception as exc:  # noqa: BLE001 - executor must stay fail-soft
            return self._blocked("attachment_proposal_unavailable", str(exc)[:300])
        return _outcome({
            "status": "accepted",
            "proposal_id": proposal.proposal_id,
            "target_attachment_id": attachment_id,
            "next_actions": ["confirm_attachment_proposal"],
            "message": "已生成章节修改提案，等待用户确认后才会写入候选。",
        })

    def get_status(self, action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        task_id = str(
            (action.parameters or {}).get("task_id")
            or _reference_value([action.target_ref or "", *action.input_refs], "task")
            or getattr(self.document_context, "task_id", "")
            or ""
        ).strip()
        if not task_id:
            return self._blocked("task_ref_unresolved", "the task status action needs a task reference")
        for name in ("get_document_task_status", "get_document_generation_status"):
            if name in self._tools_by_name:
                key = "task_id" if name == "get_document_task_status" else "work_order_id"
                return _outcome(self._invoke(name, {key: task_id}))
        return self._blocked("status_tool_unavailable", "no authorized task-status tool is available")

    def resolve_decision(self, action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        parameters = dict(action.parameters or {})
        decision = str(parameters.get("action") or "").strip().lower()
        refdes = [str(item).strip().upper() for item in parameters.get("refdes") or [] if str(item).strip()]
        if decision not in {"include", "exclude", "default", "process", "resolve"} or not refdes:
            return self._blocked(
                "decision_target_unresolved",
                "a bulk decision requires include/exclude/default and at least one group reference",
            )
        if str(parameters.get("scope") or "") != "all_pending_for_refdes":
            return self._blocked(
                "decision_scope_unsupported",
                "the requested decision scope is not safely expandable",
            )
        if decision in {"default", "process", "resolve"}:
            projected = self.document_projection or {}
            pending = projected.get("pending_decisions")
            pending = pending if isinstance(pending, list) else []
            defaults: list[str] = []
            for group in refdes:
                matches = [
                    item for item in pending
                    if isinstance(item, Mapping)
                    and str(item.get("group_key") or "").strip().casefold() == group.casefold()
                ]
                options = {
                    str(item.get("default_option") or "").strip().lower()
                    for item in matches
                    if str(item.get("default_option") or "").strip()
                }
                if not options or not options.issubset({"include", "exclude"}):
                    return self._blocked(
                        "decision_default_unresolved",
                        f"the server projection has no safe default for group {group}",
                    )
                if len(options) != 1:
                    return self._blocked(
                        "decision_default_conflict",
                        f"the server projection has conflicting defaults for group {group}",
                    )
                defaults.append(next(iter(options)))
            if len(set(defaults)) != 1:
                return self._blocked(
                    "decision_default_conflict",
                    "the selected groups do not share one safe default option",
                )
            decision = defaults[0]
        arguments: dict[str, Any] = {
            "action": decision,
            "comment": str(parameters.get("comment") or self.query or "").strip(),
            "refdes": list(dict.fromkeys(refdes)),
        }
        if parameters.get("continue_generation") is False:
            # Compound instructions resolve every explicitly named group
            # before one final task resume.  Do not let the first group queue
            # a run against an incomplete frozen scope.
            arguments["continue_generation"] = False
        work_order_id = str(parameters.get("work_order_id") or _reference_value(
            [action.target_ref or "", *action.input_refs], "work_order"
        ) or "").strip()
        if work_order_id:
            arguments["work_order_id"] = work_order_id
        return _outcome(self._invoke("resolve_icd_scope_exception", arguments))

    def resume(self, action: ActionSpec, prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        """Avoid a duplicate resume when the preceding decision already resumed it."""

        for dependency in action.depends_on:
            receipt = prior.get(dependency)
            data = receipt.data if receipt is not None else {}
            nested = data.get("data") if isinstance(data, Mapping) else None
            status = str(
                (nested.get("status") if isinstance(nested, Mapping) else None)
                or (data.get("status") if isinstance(data, Mapping) else None)
                or ""
            ).strip().casefold()
            if status == "resumed":
                return {
                    "status": "completed",
                    "result_refs": list(receipt.result_refs),
                    "message": "the preceding governed decision already resumed the task",
                    "data": dict(data),
                }
        return self._task_control("resume_document_task")(action, prior)

    def request_release(self, action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        artifact_id = str(
            (action.parameters or {}).get("artifact_id")
            or _reference_value([action.target_ref or "", *action.input_refs], "artifact")
            or getattr(self.document_context, "candidate_artifact_id", "")
            or getattr(self.document_context, "artifact_id", "")
            or (self.document_projection or {}).get("current_artifact_id")
            or ""
        ).strip()
        if not artifact_id:
            return self._blocked("candidate_ref_unresolved", "a release request needs a candidate artifact reference")
        return _outcome(self._invoke("request_document_release", {
            "artifact_id": artifact_id,
            "comment": str((action.parameters or {}).get("comment") or self.query or "").strip(),
        }))

    def export_result(self, action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
        return _outcome(self._invoke("export_result", dict(action.parameters or {})))

    def _task_control(self, tool_name: str):
        def handler(action: ActionSpec, _prior: Mapping[str, ActionReceipt]) -> dict[str, Any]:
            task_id = str(
                (action.parameters or {}).get("task_id")
                or _reference_value([action.target_ref or "", *action.input_refs], "task")
                or getattr(self.document_context, "task_id", "")
                or ""
            ).strip()
            if not task_id:
                return self._blocked("task_ref_unresolved", "the task control action needs a task reference")
            return _outcome(self._invoke(tool_name, {"task_id": task_id}))

        return handler


__all__ = ["GovernedToolActionAdapters"]
