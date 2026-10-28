"""Server-owned routing contract for conversational requests.

The browser may provide interaction hints, but it must not decide which
backend executor receives a request.  ``ConversationOrchestrator`` keeps that
decision small, deterministic and serializable so the same result can be
stored on a durable turn, emitted over SSE and asserted in shared tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import re
from typing import Any, Literal

from src.core.intent import IntentName, IntentPlan, classify_intent
from src.services.evidence_policy import resolve_evidence_purpose


ConversationRoute = Literal[
    "chat",
    "retrieval",
    "attachment_analysis",
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
    comparison_requested: bool = False
    export_requested: bool = False
    requested_formats: tuple[str, ...] = ()
    source_scope: str = "auto"
    has_attachments: bool = False
    has_kb: bool = False
    confidence: float = 0.0
    reason_codes: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    evidence_purpose: str = "qa"

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

        intent = classify_intent(
            query,
            has_attachments=bool(has_attachments),
            has_kb=bool(has_kb),
        )
        reasons = list(intent.reason_codes)
        if invalid_scope:
            reasons.append("invalid_source_scope_fallback")

        route = self._route(
            intent,
            query=query,
            has_attachments=bool(has_attachments),
            has_kb=bool(has_kb),
        )

        plan = ConversationPlan(
            schema_version=self.schema_version,
            route=route,
            intent=intent.intent,
            action=intent.action,
            target=intent.target,
            authority="backend",
            routed_by="deterministic",
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
        return replace(
            plan,
            evidence_purpose=resolve_evidence_purpose(
                query, conversation_plan=plan, intent_plan=intent,
            ),
        )

    @staticmethod
    def _route(
        intent: IntentPlan,
        *,
        query: str,
        has_attachments: bool,
        has_kb: bool,
    ) -> ConversationRoute:
        if has_attachments and _ATTACHMENT_ANALYSIS_PATTERN.search(query):
            return "attachment_analysis"
        if intent.intent in {"knowledge_base_qa", "attachment_qa", "compare", "export"}:
            return "retrieval"
        if has_kb or has_attachments:
            return "retrieval"
        return "chat"

    @staticmethod
    def _allowed_tools(route: ConversationRoute, has_attachments: bool) -> tuple[str, ...]:
        if route == "attachment_analysis":
            return ("attachment",)
        if route == "retrieval":
            return ("retrieval",) + (("attachment",) if has_attachments else ())
        return ()


__all__ = ["ConversationOrchestrator", "ConversationPlan", "ConversationRoute"]
