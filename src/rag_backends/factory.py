import config.settings
from src.rag_backends.base import RAGBackend
from src.rag_backends.local_backend import LocalRAGBackend
from src.rag_backends.ragflow_backend import RAGFlowBackend


def create_rag_backend() -> RAGBackend:
    backend_name = config.settings.RAG_BACKEND.strip().lower()
    if backend_name == "local":
        return LocalRAGBackend()
    if backend_name == "ragflow":
        return RAGFlowBackend()
    raise ValueError(f"未知 RAG_BACKEND: {backend_name}")
