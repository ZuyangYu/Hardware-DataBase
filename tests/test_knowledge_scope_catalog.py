"""The multi-KB catalog exposes sources without making the anchor special."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.tools.knowledge_scope_catalog import make_knowledge_scope_catalog_tool
from src.agents.tools.runtime import ToolRuntime


class _Store:
    def __init__(self):
        self.records = {
            "KB-A": [
                SimpleNamespace(document_name="a.docx", processor_kind="ragflow", status="completed"),
                SimpleNamespace(document_name="a.xlsx", processor_kind="spreadsheet_table", status="completed"),
            ],
            "KB-B": [
                SimpleNamespace(document_name="b.pdf", processor_kind="ragflow", status="completed"),
            ],
        }

    def list_documents(self, kb_name, department_id):
        assert department_id == "7"
        return self.records.get(kb_name, [])


def _runtime():
    scope = SimpleNamespace(sources=[
        SimpleNamespace(kb_id=101, kb_name="KB-A", department_id="7", role="primary"),
        SimpleNamespace(kb_id=205, kb_name="KB-B", department_id="7", role="reference"),
    ])
    return ToolRuntime(
        kb_name="KB-A", ctx=None, knowledge_scope=scope, document_store=_Store(),
        source_scope="knowledge_base_only", evidence_budget=10,
    )


def test_catalog_lists_all_selected_kbs_and_marks_anchor_as_session_only():
    rt = _runtime()
    output = make_knowledge_scope_catalog_tool(rt)("")

    assert "kb:101 KB-A" in output
    assert "kb:205 KB-B" in output
    assert "会话角色:primary" in output
    assert "会话角色:reference" in output
    assert "锚定库仅用于会话归属" in output
    assert "所有库同级" in output
    assert "a.docx" in output and "b.pdf" in output
    # Navigation is not formal evidence and must not allocate a citation.
    assert rt.evidence == []
