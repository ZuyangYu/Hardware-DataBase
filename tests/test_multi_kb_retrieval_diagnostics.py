"""Every real tool invocation is observable even when query logging deduplicates."""

from __future__ import annotations

from src.agents.schemas import Evidence
from src.agents.tools.runtime import ToolRuntime, timed_tool_call


def _evidence():
    return Evidence(id="e1", content="18-32V", source_name="spec.docx", content_kind="document_text",
                    processor_kind="ragflow", score=0.9, metadata={"kb_id": 5})


def test_repeated_tool_queries_produce_two_invocations_but_one_ledger_row():
    rt = ToolRuntime(kb_name="A", ctx=None, question="supply", evidence_budget=10)
    calls = []

    def run():
        calls.append(1)
        return [_evidence()]

    timed_tool_call(rt, "document_search", "supply", None, run)
    _, adds_nothing = timed_tool_call(rt, "document_search", "supply", None, run)

    assert len(calls) == 2
    assert len(rt.queries) == 1
    assert len(rt.invocations) == 2
    assert {row["invocation_id"] for row in rt.invocations} == {"inv-1", "inv-2"}
    assert all(row["query_fingerprint"] for row in rt.invocations)
    assert all("queue_wait_ms" in row["phase_timings"] for row in rt.invocations)
    assert all(row["source_operations"]["candidate_queries"] == 0 for row in rt.invocations)
    assert [row["admitted_items"] for row in rt.invocations] == [1, 1]
    assert adds_nothing is True


def test_failed_invocation_records_error_without_leaking_content():
    rt = ToolRuntime(kb_name="A", ctx=None, question="supply")

    def fail():
        raise RuntimeError("boom")

    timed_tool_call(rt, "document_search", "supply", None, fail)

    assert len(rt.invocations) == 1
    assert rt.invocations[0]["status"] == "failed"
    assert "boom" in rt.invocations[0]["error"]


def test_invocation_records_remote_http_and_sql_operation_counts():
    class Batch(list):
        source_statuses = [{"kb_id": 5, "status": "success_with_hits", "diagnostics": {
            "candidate_queries": 1, "validation_queries": 2, "records": 4, "remote_http_calls": 3,
        }}]

    rt = ToolRuntime(kb_name="A", ctx=None, question="supply", evidence_budget=10)
    timed_tool_call(rt, "spreadsheet_row_search", "supply", None, lambda: Batch([_evidence()]))
    operations = rt.invocations[0]["source_operations"]
    assert operations == {"source_calls": 1, "candidate_queries": 1, "validation_queries": 2,
                          "records": 4, "remote_http_calls": 3}
