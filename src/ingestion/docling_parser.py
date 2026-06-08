# src/ingestion/docling_parser.py
from llama_index.readers.docling import DoclingReader
from llama_index.node_parser.docling import DoclingNodeParser
from src.core.logger import log, error


_reader = DoclingReader(export_type=DoclingReader.ExportType.JSON)
_node_parser = DoclingNodeParser()


def parse_file(file_path: str, filename: str, kb_name: str):
    """
    使用 Docling 解析文档，返回 LlamaIndex Node 列表。
    DoclingReader 以 JSON 格式读取文档（保留布局、表格等结构），
    DoclingNodeParser 利用 Docling 格式知识进行语义感知的分块。
    """
    log(f"Docling 解析文件: {filename}")

    documents = _reader.load_data(file_path=file_path)

    for doc in documents:
        doc.metadata["file_name"] = filename
        doc.metadata["kb_name"] = kb_name

    nodes = _node_parser.get_nodes_from_documents(documents)

    log(f"Docling 解析完成: {len(documents)} 个文档 -> {len(nodes)} 个节点")
    return nodes
