"""Regression tests for the RAGFlow SiliconFlow timeout overlay."""

from __future__ import annotations

import ast
from pathlib import Path


SOURCE_PATH = Path(__file__).parents[1] / "deploy" / "ragflow" / "embedding_model.py"


def _siliconflow_call_method() -> ast.FunctionDef:
    tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SILICONFLOWEmbed":
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == "_call":
                    return member
    raise AssertionError("SILICONFLOWEmbed._call was not found")


def test_siliconflow_timeout_defaults_to_100_seconds() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    assert "DEFAULT_SILICONFLOW_EMBEDDING_TIMEOUT_SECONDS = 100.0" in source


def test_siliconflow_request_uses_validated_timeout_helper() -> None:
    method = _siliconflow_call_method()
    post_calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "post"
    ]
    assert len(post_calls) == 1
    timeout_keywords = [keyword for keyword in post_calls[0].keywords if keyword.arg == "timeout"]
    assert len(timeout_keywords) == 1
    timeout_value = timeout_keywords[0].value
    assert isinstance(timeout_value, ast.Call)
    assert isinstance(timeout_value.func, ast.Name)
    assert timeout_value.func.id == "_siliconflow_embedding_timeout"

