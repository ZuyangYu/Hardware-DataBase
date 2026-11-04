"""Workspace tools must stay inside the task root."""

from __future__ import annotations

import json
import os

import pytest

from src.agents.tools.workspace_tools import build_workspace_tools
from src.agents.workspace import Workspace, WorkspaceError


@pytest.fixture()
def workspace(tmp_path):
    return Workspace(str(tmp_path / "ws"))


def test_write_read_list_roundtrip(workspace):
    written = workspace.write("out/list.md", "# 清单\n")
    assert written["written"] is True
    assert os.path.isfile(os.path.join(workspace.root, "out", "list.md"))

    read = workspace.read("out/list.md")
    assert read["content"] == "# 清单\n"

    listing = workspace.list("out")
    assert listing["type"] == "dir"
    assert [item["name"] for item in listing["entries"]] == ["list.md"]


def test_path_escape_is_rejected(workspace, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    for path in ("../secret.txt", "/etc/passwd", "../" + str(outside)):
        with pytest.raises(WorkspaceError):
            workspace.read(path)
    with pytest.raises(WorkspaceError):
        workspace.write("../escape.md", "x")


def test_workspace_tools_return_json(workspace):
    tools = {item.__name__: item for item in build_workspace_tools(str(workspace.root))}
    result = json.loads(tools["workspace_write"]("out/a.md", "hello"))
    assert result["written"] is True
    read = json.loads(tools["workspace_read"]("out/a.md"))
    assert read["content"] == "hello"
    listing = json.loads(tools["workspace_list"]("."))
    assert listing["type"] == "dir"
    missing = json.loads(tools["workspace_read"]("nope.md"))
    assert "error" in missing
