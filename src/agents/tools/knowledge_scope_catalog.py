"""Agent-facing catalog for a multi-knowledge-base turn.

The catalog is navigation-only: it exposes the authorized KB envelope and the
available processor/status groups without reading document bodies.  The model
uses it to decide which source to query; the session anchor is reported for
ownership/audit only and never gets a ranking or reserved slot.
"""

from __future__ import annotations

from collections import Counter

from src.agents.schemas import Evidence
from src.agents.tools.runtime import format_tool_result, timed_tool_call


def make_knowledge_scope_catalog_tool(rt):
    def list_knowledge_sources(query: str = "") -> str:
        """列出本轮已授权知识库、资料类型/状态和少量文件名；不读取正文。

        所有已选知识库在检索时同级；``role=primary`` 只表示会话锚定库。
        需要限定单个库时，把 ``source_ref=kb:<id>`` 传给对应检索工具。
        """
        scope = getattr(rt, "knowledge_scope", None)
        if scope is None:
            return "当前未启用多知识库范围；请使用当前知识库的检索工具。"

        store = getattr(rt, "document_store", None)

        def run(_: str) -> list[Evidence]:
            lines = [
                "本轮授权知识库（锚定库仅用于会话归属，检索时所有库同级）：",
            ]
            for source in scope.sources:
                records = []
                if store is not None and hasattr(store, "list_documents"):
                    try:
                        records = list(store.list_documents(source.kb_name, source.department_id))
                    except Exception:  # noqa: BLE001 - one source must not hide others
                        records = []
                kinds = Counter(
                    str(getattr(record, "processor_kind", "") or "document")
                    for record in records
                )
                statuses = Counter(
                    str(getattr(record, "status", "") or "unknown")
                    for record in records
                )
                kind_text = "、".join(
                    f"{kind}:{count}" for kind, count in sorted(kinds.items())
                ) or "无可用资料"
                status_text = "、".join(
                    f"{status}:{count}" for status, count in sorted(statuses.items())
                ) or "-"
                lines.append(
                    f"- kb:{source.kb_id} {source.kb_name} | 会话角色:{source.role} | "
                    f"资料:{len(records)}（{kind_text}）| 状态:{status_text}"
                )
                names = [
                    str(getattr(record, "document_name", "") or "")
                    for record in records[:6]
                ]
                if names:
                    lines.append("  文件: " + "；".join(name for name in names if name))
            lines.append(
                "提示：用检索工具的 source_ref=kb:<id> 限定单个库；不指定时检索全部已选库。"
            )
            return [Evidence(
                id="catalog:knowledge_scope",
                content="\n".join(lines),
                source_name="知识库目录",
                content_kind="catalog",
                processor_kind="knowledge_scope_catalog",
                score=1.0,
                metadata={
                    "tool": "list_knowledge_sources",
                    "evidence_role": "navigation",
                },
            )]

        items, adds_nothing = timed_tool_call(
            rt, "list_knowledge_sources", query, None, lambda: run(""),
        )
        return format_tool_result(rt, adds_nothing, items)

    return list_knowledge_sources


__all__ = ["make_knowledge_scope_catalog_tool"]
