"""Server-owned routing contract for conversational requests.

The browser may provide interaction hints, but it must not decide which
backend executor receives a request.  ``ConversationOrchestrator`` keeps that
decision small, deterministic and serializable so the same result can be
stored on a durable turn, emitted over SSE and asserted in shared tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Literal

from src.core.intent import IntentName, IntentPlan, classify_intent


ConversationRoute = Literal[
    "chat",
    "retrieval",
    "attachment_analysis",
    "document_authoring",
]

_SOURCE_SCOPES = {
    "auto",
    "attachment_only",
    "knowledge_base_only",
    "attachment_and_knowledge_base",
}
_ATTACHMENT_ANALYSIS_PATTERN = re.compile(
    r"分析|解析|提取|识别|总结|读取|extract|analy[sz]e|parse|summari[sz]e",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConversationPlan:
    """Versioned, backend-authoritative route decision."""

    schema_version: str
    route: ConversationRoute
    intent: IntentName
    action: str
    target: str
    authority: Literal["backend"]
    routed_by: Literal["explicit", "deterministic", "fallback"]
    document_flow: bool
    template_required: bool = False
    comparison_requested: bool = False
    export_requested: bool = False
    requested_formats: tuple[str, ...] = ()
    source_scope: str = "auto"
    has_attachments: bool = False
    has_kb: bool = False
    confidence: float = 0.0
    reason_codes: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["requested_formats"] = list(self.requested_formats)
        payload["reason_codes"] = list(self.reason_codes)
        payload["allowed_tools"] = list(self.allowed_tools)
        return payload


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


class ConversationOrchestrator:
    """Choose the single executor for one request at the backend boundary."""

    schema_version = "v1"

    def plan(
        self,
        request: Any | None = None,
        context: Any | None = None,
        *,
        query: str | None = None,
        document_context: Any | None = None,
        has_document_tools: bool | None = None,
        explicit_flow: bool | None = None,
        has_attachments: bool | None = None,
        has_kb: bool | None = None,
        source_scope: str | None = None,
    ) -> ConversationPlan:
        """Return a route from request/context objects or explicit arguments.

        Accepting both a request object and keyword arguments keeps the
        contract convenient for HTTP adapters, durable workers and unit tests.
        Values supplied explicitly take precedence over object attributes.
        """

        query = str(
            query if query is not None else _value(request, "query", "")
        ).strip()
        if document_context is None:
            document_context = _value(request, "document_context", None)
        if document_context is None:
            document_context = _value(context, "document_context", None)
        if explicit_flow is None:
            explicit_flow = _value(request, "document_flow", None)
        if explicit_flow is None:
            explicit_flow = _value(context, "document_flow", None)
        if has_document_tools is None:
            has_document_tools = bool(
                _value(request, "has_document_tools", _value(context, "has_document_tools", False))
            )
        if has_attachments is None:
            has_attachments = bool(
                _value(request, "has_attachments", _value(context, "has_attachments", False))
            )
        if has_kb is None:
            has_kb = bool(_value(request, "has_kb", _value(context, "has_kb", True)))
        if source_scope is None:
            source_scope = str(
                _value(request, "source_scope", _value(context, "source_scope", "auto"))
                or "auto"
            ).strip()
        if source_scope not in _SOURCE_SCOPES:
            source_scope = "auto"
            invalid_scope = True
        else:
            invalid_scope = False

        expired = bool(_value(document_context, "expired", False))
        context_valid = bool(document_context is not None and not expired)
        document_capable = bool(has_document_tools and context_valid)
        intent = classify_intent(
            query,
            has_template_context=context_valid,
            has_attachments=bool(has_attachments),
            has_kb=bool(has_kb),
        )
        reasons = list(intent.reason_codes)
        if invalid_scope:
            reasons.append("invalid_source_scope_fallback")

        if explicit_flow is True:
            if document_capable:
                route: ConversationRoute = "document_authoring"
                document_flow = True
                routed_by: Literal["explicit", "deterministic", "fallback"] = "explicit"
                reasons.append("explicit_document_flow")
            else:
                route, document_flow = self._non_document_route(
                    intent,
                    query=query,
                    has_attachments=bool(has_attachments),
                    has_kb=bool(has_kb),
                )
                routed_by = "fallback"
                document_flow = False
                if document_context is None:
                    reasons.append("document_context_missing")
                elif expired:
                    reasons.append("document_context_expired")
                if not has_document_tools:
                    reasons.append("document_tools_unavailable")
        elif explicit_flow is False:
            route, document_flow = self._non_document_route(
                intent,
                query=query,
                has_attachments=bool(has_attachments),
                has_kb=bool(has_kb),
            )
            routed_by = "explicit"
            reasons.append("explicit_document_flow_disabled")
        elif document_capable and intent.intent == "template_generation":
            route = "document_authoring"
            document_flow = True
            routed_by = "deterministic"
            reasons.append("backend_document_intent")
        else:
            route, document_flow = self._non_document_route(
                intent,
                query=query,
                has_attachments=bool(has_attachments),
                has_kb=bool(has_kb),
            )
            routed_by = "deterministic"

        return ConversationPlan(
            schema_version=self.schema_version,
            route=route,
            intent=intent.intent,
            action=intent.action,
            target=intent.target,
            authority="backend",
            routed_by=routed_by,
            document_flow=document_flow,
            template_required=bool(intent.template_required and document_flow),
            comparison_requested=intent.comparison_requested,
            export_requested=intent.export_requested,
            requested_formats=intent.requested_formats,
            source_scope=source_scope,
            has_attachments=bool(has_attachments),
            has_kb=bool(has_kb),
            confidence=float(intent.confidence),
            reason_codes=tuple(dict.fromkeys(reasons)),
            allowed_tools=self._allowed_tools(route, bool(has_attachments)),
        )

    @staticmethod
    def _non_document_route(
        intent: IntentPlan,
        *,
        query: str,
        has_attachments: bool,
        has_kb: bool,
    ) -> tuple[ConversationRoute, bool]:
        if has_attachments and _ATTACHMENT_ANALYSIS_PATTERN.search(query):
            return "attachment_analysis", False
        if intent.intent in {"knowledge_base_qa", "attachment_qa", "compare", "export"}:
            return "retrieval", False
        if has_kb or has_attachments:
            return "retrieval", False
        return "chat", False

    @staticmethod
    def _allowed_tools(route: ConversationRoute, has_attachments: bool) -> tuple[str, ...]:
        if route == "document_authoring":
            return ("document_authoring", "evidence") + (("attachment",) if has_attachments else ())
        if route == "attachment_analysis":
            return ("attachment",)
        if route == "retrieval":
            return ("retrieval",) + (("attachment",) if has_attachments else ())
        return ()


__all__ = ["ConversationOrchestrator", "ConversationPlan", "ConversationRoute"]
