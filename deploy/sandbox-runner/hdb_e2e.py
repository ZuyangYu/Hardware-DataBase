#!/usr/bin/env python3
"""HDB control-plane <-> reference Runner/Gateway end-to-end validation.

Uses HDB's real client, catalog, task ledger, artifact broker and Gateway
registry (no fakes) against the live reference services:

  SandboxExecutionAdapter: admit -> submit -> observe -> collect
  GatewayRegistry:         register -> review/tested/enable -> attach ->
                           discover -> invoke -> disable

Run with sandbox settings in the environment (see the README).  Requires a
non-root operator, a reachable Runner/Gateway and a 0600 token file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time

import src.settings as settings
from src.agents.mcp.gateway import GatewayRegistry
from src.sandbox.artifact_broker import ArtifactBroker
from src.sandbox.artifacts import build_registered_scanner
from src.sandbox.catalog import SandboxCatalog
from src.sandbox.client import SandboxServiceClient
from src.sandbox.dependencies import FileTokenProvider
from src.sandbox.models import SandboxTaskSpec
from src.sandbox.protocol import RequestEnvelope
from src.sandbox.service import SandboxExecutionAdapter
from src.sandbox.task_store import SandboxTaskStore

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BUNDLE_DIR = os.path.join(HERE, "bundles", "report")
TOKEN_FILE = os.getenv(
    "SANDBOX_SERVICE_TOKEN_FILE", os.path.join(HERE, "secrets", "token")
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'ok' if condition else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def bundle_digest() -> str:
    with open(os.path.join(BUNDLE_DIR, "entrypoint.py"), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="hdb-sandbox-e2e-")
    db_path = os.path.join(workdir, "sandbox.db")
    store = SandboxTaskStore(db_path)
    catalog = SandboxCatalog(db_path)
    broker = ArtifactBroker(
        os.path.join(workdir, "artifacts"), db_path, scanner=build_registered_scanner()
    )
    client = SandboxServiceClient(
        runner_url=settings.SANDBOX_RUNNER_URL,
        gateway_url=settings.SANDBOX_GATEWAY_URL,
        auth_provider=FileTokenProvider(TOKEN_FILE),
        require_authentication=True,
    )

    bundle = catalog.register(
        bundle_ref="bundle/report",
        bundle_digest=bundle_digest(),
        profile_ref="profile/dev-l1",
        required_capabilities=("sandbox.execute", "sandbox.write_artifact"),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 80},
                "rows": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        output_schema={},
    )
    catalog.review(bundle.bundle_ref)
    catalog.mark_tested(bundle.bundle_ref)
    bundle = catalog.enable(bundle.bundle_ref)
    check("bundle registered/reviewed/enabled", bundle.enabled is True)

    adapter = SandboxExecutionAdapter(
        store=store, catalog=catalog, broker=broker, client=client
    )
    spec = SandboxTaskSpec(
        idempotency_key=f"hdb-e2e-{os.getpid()}-{int(__import__('time').time_ns())}",
        owner_id="user-1",
        department_id="dept-1",
        bundle_ref=bundle.bundle_ref,
        bundle_digest=bundle.bundle_digest,
        profile_ref=bundle.profile_ref,
        policy_snapshot_hash=bundle.policy_snapshot_hash,
        required_capabilities=("sandbox.execute", "sandbox.write_artifact"),
        parameters={"title": "hdb-e2e", "rows": 3},
    )
    task = adapter.admit(spec)
    check("task admitted", task.status == "admitted", task.status)
    task = adapter.submit(task.task_id)
    check("task submitted to the real runner", task.status == "running" and bool(task.execution_id), task.status)
    deadline = time.monotonic() + 120
    while task.status in {"running", "queued"} and time.monotonic() < deadline:
        task = adapter.observe(task.task_id)
        if task.status in {"collecting", "succeeded", "failed", "cancelled", "timed_out", "reconcile_required"}:
            break
        time.sleep(0.2)
    check("observation reaches collecting", task.status == "collecting", task.status)
    result = adapter.collect(task.task_id)
    items = (result.get("output_manifest") or {}).get("items") or []
    check("collect commits broker artifacts", result["task"]["status"] == "succeeded" and len(items) == 2, str(len(items)))
    if items:
        artifact_id = items[0]["artifact_id"]
        content = broker.read(artifact_id, principal_id="user-1", department_id="dept-1")
        check(
            "committed artifact content matches sha256",
            hashlib.sha256(content).hexdigest() == items[0]["sha256"],
            items[0]["filename"],
        )

    with open(os.path.join(REPO, "deploy", "mcp-gateway", "deployments.json"), encoding="utf-8") as handle:
        image_digest = json.load(handle)["deployments"]["deploy/sandbox-info"]["image_digest"]
    registry = GatewayRegistry(db_path)
    deployment = registry.register(
        deployment_ref="deploy/sandbox-info",
        profile_ref="profile/dev-l1",
        endpoint=settings.SANDBOX_GATEWAY_URL,
        digest=image_digest,
        capabilities=("external_tool.invoke",),
        tool_allowlist={
            "sandbox_runtime_info": {
                "type": "object",
                "properties": {"label": {"type": "string", "maxLength": 64}},
                "required": ["label"],
                "additionalProperties": False,
            }
        },
    )
    registry.review(deployment.deployment_ref)
    registry.mark_tested(deployment.deployment_ref)
    deployment = registry.enable(deployment.deployment_ref)
    check("gateway deployment enabled", deployment.enabled is True)

    envelope = RequestEnvelope.create(
        actor_id="user-1",
        department_id="dept-1",
        policy_snapshot_hash=f"gateway:{deployment.deployment_ref}:{deployment.digest}",
        capability_snapshot=("external_tool.invoke",),
        audience="sandbox-gateway",
    )
    registry.subscribe(deployment.deployment_ref, user_id="user-1", department_id="dept-1")
    attached = registry.attach(deployment.deployment_ref, client=client, envelope=envelope)
    check("gateway attach via runner", attached.get("attached") is True, str(attached)[:100])

    discovered = registry.discover(deployment.deployment_ref, client=client, envelope=envelope)
    names = [str(item.get("name")) for item in discovered.get("tools") or []]
    check("gateway discover honours the allowlist", names == ["sandbox_runtime_info"], str(names))

    registry.authorize_invocation(
        deployment.deployment_ref,
        user_id="user-1",
        department_id="dept-1",
        tool_name="sandbox_runtime_info",
        arguments={"label": "hdb"},
        capabilities=("external_tool.invoke",),
    )
    invoked = client.gateway_invoke(
        deployment.deployment_ref,
        "sandbox_runtime_info",
        {"label": "hdb"},
        envelope=envelope,
    )
    text = ((invoked.get("result") or {}).get("content") or [{}])[0].get("text", "")
    check(
        "gateway invoke executes the stdio MCP tool",
        '"label": "hdb"' in text and '"network": "none"' in text,
        text[:80],
    )

    client.gateway_disable(deployment.deployment_ref, envelope=envelope)
    client.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
