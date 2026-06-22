# src/ingestion/data_loader.py
import os
from llama_index.core import SimpleDirectoryReader
import config.settings
from src.core.logger import log, error
from src.ingestion.kb_paths import get_kb_data_path


def get_kb_path(kb_name: str) -> str:
    """获取知识库的文件路径"""
    return get_kb_data_path(kb_name)


def list_knowledge_bases() -> list[str]:
    """列出所有知识库"""
    os.makedirs(config.settings.DATA_ROOT, exist_ok=True)
    try:
        return [
            d for d in os.listdir(config.settings.DATA_ROOT)
            if os.path.isdir(os.path.join(config.settings.DATA_ROOT, d))
        ]
    except Exception as e:
        error(f"列出知识库失败: {e}")
        return []


def load_documents(kb_name: str):
    """加载知识库的所有文档"""
    path = get_kb_path(kb_name)
    os.makedirs(path, exist_ok=True)

    try:
        files = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
        if not files:
            log(f"知识库 '{kb_name}' 为空")
            return []

        log(f"加载知识库 '{kb_name}': {len(files)} 个文件")
        reader = SimpleDirectoryReader(input_dir=path, recursive=True)
        docs = reader.load_data()
        log(f"加载完成: {len(docs)} 个文档块")
        return docs

    except Exception as e:
        error(f"加载文档失败: {e}")
        return []
