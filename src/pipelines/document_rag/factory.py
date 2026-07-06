from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_rag.ragflow_backend import RAGFlowBackend


def create_rag_backend() -> RAGBackend:
    return RAGFlowBackend()
