"""Prepare the ADAS_new knowledge base for RAGAS evaluation.

The evaluation dataset ``hardware_qa_v1`` is bound to department 96 / KB
``ADAS_new``.  Documents already live in RAGFlow, but this deployment has no
local ``pipeline_documents.db`` mapping for them, so the department-scoped
filter drops every remote chunk.  This script performs the local environment
repair, without re-uploading or fabricating content:

1. register the existing remote documents in the pipeline document store with
   the evaluation's department scope;
2. index the local ADAS netlist (``.EDF``) into the ADAS_new circuit store so
   circuit questions have evidence.

Both steps are idempotent.  Run it once before ``hardware-database-eval``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import src.settings as settings  # noqa: E402
from src.pipelines.document_rag.ragflow_backend import RAGFlowClient  # noqa: E402
from src.pipelines.document_store_sqlite import PipelineDocumentStore  # noqa: E402

DEFAULT_KB = "ADAS_new"
DEFAULT_DEPARTMENT = "96"
DEFAULT_EDF = (
    REPO_ROOT
    / "docs"
    / "test_chat"
    / "AA250004_ 奇瑞 T1G 高法规 ADAS项目"
    / "825504380_ADAS_SCH_TCN2.EDF"
)

# Remote document name -> ingestion source group.  The netlist is indexed as a
# circuit (local) instead of a RAGFlow document.
SOURCE_GROUPS = {
    "825504380_ADAS_SCH_TCN2.pdf": "原理图数据",
}
DEFAULT_SOURCE_GROUP = "文档资料"


def _remote_documents(client: RAGFlowClient, kb: str) -> tuple[str, list[dict]]:
    datasets = client.list_datasets()
    target = next((item for item in datasets if item.get("name") == kb), None)
    if target is None:
        raise SystemExit(f"RAGFlow dataset not found: {kb}")
    documents = client.list_documents(str(target["id"])) if hasattr(client, "list_documents") else []
    return str(target["id"]), documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", default=DEFAULT_KB)
    parser.add_argument("--department-id", default=DEFAULT_DEPARTMENT)
    parser.add_argument("--edf", default=str(DEFAULT_EDF))
    parser.add_argument("--skip-circuit", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    client = RAGFlowClient(
        base_url=settings.RAGFLOW_BASE_URL,
        api_key=settings.RAGFLOW_API_KEY,
        timeout=30,
    )
    dataset_id, documents = _remote_documents(client, args.kb)
    store = PipelineDocumentStore()
    registered: list[dict] = []
    for document in documents:
        name = str(document.get("name") or document.get("document_name") or "").strip()
        remote_id = str(document.get("id") or document.get("document_id") or "").strip()
        if not name or not remote_id:
            continue
        source_group = SOURCE_GROUPS.get(name, DEFAULT_SOURCE_GROUP)
        store.upsert_document(
            kb_name=args.kb,
            document_name=name,
            dataset_kind="design",
            dataset_id=dataset_id,
            document_id=remote_id,
            source_group=source_group,
            department_id=str(args.department_id),
            uploaded_by="evaluation-setup",
            status="parsed",
            original_file_name=name,
            content_kind="document_text",
            processor_kind="ragflow",
        )
        registered.append({"document_name": name, "remote_document_id": remote_id, "source_group": source_group})

    circuit_result: dict[str, object] = {"indexed": False}
    if not args.skip_circuit:
        edf = Path(args.edf)
        if not edf.is_file():
            circuit_result = {"indexed": False, "error": f"EDF not found: {edf}"}
        else:
            from src.circuit.index_service import CircuitIndexService

            service = CircuitIndexService()
            result = service.index_file(
                kb_name=args.kb,
                record_id=None,
                file_path=str(edf),
                original_name=edf.name,
                department_id=str(args.department_id),
                uploaded_by="evaluation-setup",
            )
            circuit_result = {
                "indexed": True,
                "design_id": getattr(result, "design_id", ""),
                "record_id": getattr(result, "record_id", None),
            }

    report = {
        "kb": args.kb,
        "department_id": str(args.department_id),
        "dataset_id": dataset_id,
        "registered_documents": registered,
        "circuit": circuit_result,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
