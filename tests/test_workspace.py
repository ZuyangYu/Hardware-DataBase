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


def test_edit_requires_unique_match(workspace):
    workspace.write("out/a.md", "alpha beta alpha")
    with pytest.raises(WorkspaceError):
        workspace.edit("out/a.md", "alpha", "gamma")
    result = workspace.edit("out/a.md", "alpha", "gamma", replace_all=True)
    assert result["replaced"] == 2
    assert workspace.read("out/a.md")["content"] == "gamma beta gamma"


def test_glob_and_grep(workspace):
    workspace.write("out/a.md", "# 清单\nMCU: SAK-TC377\n")
    workspace.write("out/b.csv", "refdes,model\nU1800,TPS62872\n")
    globbed = workspace.glob("**/*.md")
    assert globbed["matches"] == ["out/a.md"]
    found = workspace.grep("SAK-TC377")
    assert found["matches"][0]["path"] == "out/a.md"
    assert found["matches"][0]["line"] == 2


def test_workspace_tools_include_edit_glob_grep(workspace):
    tools = {item.__name__: item for item in build_workspace_tools(str(workspace.root))}
    assert {"workspace_edit", "workspace_glob", "workspace_grep"} <= set(tools)
    json.loads(tools["workspace_write"]("out/a.md", "hello world"))
    edited = json.loads(tools["workspace_edit"]("out/a.md", "world", "agent"))
    assert edited["replaced"] == 1
    assert json.loads(tools["workspace_read"]("out/a.md"))["content"] == "hello agent"
    assert json.loads(tools["workspace_glob"]("**/*.md"))["matches"] == ["out/a.md"]
    assert json.loads(tools["workspace_grep"]("agent"))["matches"][0]["line"] == 1


class _FakeRuntime:
    def __init__(self):
        self.started = []
        self.finished = []
        self._seq = 0

    def check_cancel(self):
        return None

    def next_invocation_id(self):
        self._seq += 1
        return f"call-{self._seq}"

    def tool_started(self, **kwargs):
        self.started.append(kwargs)

    def tool_result(self, **kwargs):
        self.finished.append(kwargs)


def test_workspace_tools_emit_tool_events(tmp_path):
    rt = _FakeRuntime()
    tools = {
        item.__name__: item
        for item in build_workspace_tools(str(tmp_path / "ws"), rt)
    }
    json.loads(tools["workspace_write"]("out/a.md", "hello"))
    assert rt.started[0]["tool_name"] == "workspace_write"
    assert rt.started[0]["tool_kind"] == "workspace"
    assert rt.finished[0]["status"] == "success"
