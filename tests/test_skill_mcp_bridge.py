"""Controlled MCP-result to Sandbox-Artifact bridging tests."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from src.agents.mcp.artifacts import McpArtifactStore
from src.sandbox.agent_tools import _import_mcp_artifact


def test_mcp_result_is_copied_only_from_the_current_turn_registry(tmp_path):
    content = b'{"external": true}'
    source_store = McpArtifactStore(str(tmp_path / "mcp"))
    source = source_store.save(
        user_id=42,
        proposal_id="result-bridge",
        filename="external.json",
        content_bytes=content,
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="application/json",
    )
    record = {
        **source.to_dict(),
        "server": "cad",
        "tool": "read_design",
    }

    class Runtime:
        def get_mcp_artifact(self, artifact_ref):
            return record if artifact_ref == source.artifact_ref else None

    class Store:
        def require(self, task_id):
            assert task_id == "task-bridge-1234567890"
            return SimpleNamespace(
                task_id=task_id,
                owner_id="42",
                department_id="7",
            )

    class Broker:
        def __init__(self):
            self.staged = None
            self.committed = None

        def stage_input(self, **kwargs):
            self.staged = kwargs
            return SimpleNamespace(artifact_id="art-bridge-1234567890")

        def scan_and_commit(self, artifact_id, **kwargs):
            self.committed = (artifact_id, kwargs)
            return SimpleNamespace(
                artifact_id=artifact_id,
                filename=self.staged["filename"],
                media_type=self.staged["media_type"],
                sha256=self.staged["sha256"],
                size=len(self.staged["content"]),
                role="input",
            )

    broker = Broker()
    result = _import_mcp_artifact(
        source.artifact_ref,
        "task-bridge-1234567890",
        user_id="42",
        department_id="7",
        store=Store(),
        broker=broker,
        runtime=Runtime(),
        mcp_store=source_store,
    )

    assert result["artifact_id"] == "art-bridge-1234567890"
    assert result["source_artifact_ref"] == source.artifact_ref
    assert result["source_server"] == "cad"
    assert broker.staged["content"] == content
    assert broker.staged["metadata"]["source_tool"] == "read_design"
    assert broker.committed[0] == "art-bridge-1234567890"
