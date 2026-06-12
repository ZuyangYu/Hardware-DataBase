# src/ingestion/docling_parser.py
from pathlib import Path
from typing import Callable

from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from llama_index.core.schema import TextNode
from llama_index.readers.docling import DoclingReader
from llama_index.node_parser.docling import DoclingNodeParser
from pypdf import PdfReader

import config.settings
from src.core.logger import log, warn


_pdf_pipeline_options = PdfPipelineOptions(
    accelerator_options=AcceleratorOptions(num_threads=1, device="cpu"),
    do_ocr=False,
    do_table_structure=True,
    ocr_batch_size=1,
    layout_batch_size=1,
    table_batch_size=1,
    queue_max_size=2,
)
_doc_converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=_pdf_pipeline_options),
    }
)
_reader = DoclingReader(
    export_type=DoclingReader.ExportType.JSON,
    doc_converter=_doc_converter,
)
_node_parser = DoclingNodeParser()


def _split_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    chunks = []
    start = 0
    text_length = len(text)
    step = max(1, chunk_size - chunk_overlap)

    while start < text_length:
        end = min(start + chunk_size, text_length)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_length:
            break
        start += step

    return chunks


def _parse_pdf_text_layer(
    file_path: str,
    filename: str,
    kb_name: str,
    source_group: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
):
    """优先解析 PDF 自带文本层，避免 OCR/版面模型导致内存占用过高。"""
    reader = PdfReader(file_path)
    nodes = []
    total_pages = len(reader.pages)

    for page_number, page in enumerate(reader.pages, start=1):
        if progress_callback and (page_number == 1 or page_number % 5 == 0 or page_number == total_pages):
            progress = 40 + int((page_number / max(1, total_pages)) * 25)
            progress_callback(progress, f"解析 PDF 文本层（第 {page_number}/{total_pages} 页）")

        text = page.extract_text() or ""
        text = text.strip()
        if not text:
            continue

        metadata = {
            "file_name": filename,
            "kb_name": kb_name,
            "page_label": str(page_number),
            "source": filename,
        }
        if source_group:
            metadata["source_group"] = source_group

        for chunk in _split_text(text, config.settings.CHUNK_SIZE, config.settings.CHUNK_OVERLAP):
            nodes.append(TextNode(text=chunk, metadata=metadata.copy()))

    log(f"PDF 文本层解析完成: {len(reader.pages)} 页 -> {len(nodes)} 个节点")
    return nodes


def parse_file(
    file_path: str,
    filename: str,
    kb_name: str,
    source_group: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
):
    """
    使用 Docling 解析文档，返回 LlamaIndex Node 列表。
    DoclingReader 以 JSON 格式读取文档（保留布局、表格等结构），
    DoclingNodeParser 利用 Docling 格式知识进行语义感知的分块。
    """
    log(f"Docling 解析文件: {filename}")

    if Path(filename).suffix.lower() == ".pdf":
        nodes = _parse_pdf_text_layer(
            file_path,
            filename,
            kb_name,
            source_group=source_group,
            progress_callback=progress_callback,
        )
        if nodes:
            return nodes
        warn(f"PDF 未提取到文本层，回退到 Docling 解析: {filename}")

    if progress_callback:
        progress_callback(45, "Docling 深度解析文档")
    documents = _reader.load_data(file_path=file_path)

    for doc in documents:
        doc.metadata["file_name"] = filename
        doc.metadata["kb_name"] = kb_name
        if source_group:
            doc.metadata["source_group"] = source_group

    if progress_callback:
        progress_callback(60, "生成文档分块")
    nodes = _node_parser.get_nodes_from_documents(documents)
    if progress_callback:
        progress_callback(65, f"文档分块完成（{len(nodes)} 个分块）")

    log(f"Docling 解析完成: {len(documents)} 个文档 -> {len(nodes)} 个节点")
    return nodes
