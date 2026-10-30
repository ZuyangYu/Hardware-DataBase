"""Subscribed Skill tools for the governed conversation agent.

The model receives only fixed Skill references and JSON parameters. HDB uses
the same catalog/task/Artifact Broker gates as the employee execution API; it
never receives package bytes, installer arguments or a container command.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

import src.settings as settings
from src.core.cancellation import QueryCancelled

from .artifact_broker import ArtifactBroker
from .artifacts import build_registered_scanner
from .authorization import SandboxAuthorizationStore, capability_snapshot_hash
from .catalog import BundleCatalogError, SandboxCatalog
from .models import SandboxTaskSpec
from .policy import SandboxPolicyError, require_execution_enabled
from .service import SandboxExecutionAdapter
from .skills import SkillCatalog, SkillCatalogError, SkillRecord
from .task_store import SandboxTaskStore

_FINISHED = frozenset({"succeeded", "failed", "cancelled", "expired", "rejected", "timed_out", "reconcile_required"})


def _preview(task_id: str, *, user_id: str, department_id: str, broker: ArtifactBroker) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for artifact in (
        item for item in broker.list_for_task(task_id, states={"committed", "consumed"})
        if item.role == "output"
    ):
        if len(outputs) >= 8:
            break
        item: dict[str, Any] = {
            "artifact_id": artifact.artifact_id, "filename": artifact.filename,
            "sha256": artifact.sha256, "size": artifact.size,
        }
        if artifact.filename.lower().endswith((".txt", ".md", ".csv", ".json")) and artifact.size <= 4096:
            try:
                content = broker.read(
                    artifact.artifact_id, principal_id=user_id, department_id=department_id, task_id=task_id,
                )
                item["untrusted_preview"] = content.decode("utf-8", errors="replace")[:2000]
            except Exception:
                # A missing or newly quarantined payload does not make the
                # whole task invisible; keep only its verified metadata.
                pass
        outputs.append(item)
    return outputs


def make_skill_tools(rt: Any, *, user_id: int | None, department_id: Any) -> list[Any]:
    """Expose only currently subscribed, executable, department-scoped Skills."""
    if (user_id is None or not department_id or not bool(getattr(settings, "SKILLS_ENABLED", False))):
        return []
    try:
        require_execution_enabled(operation="Agent Skill tools")
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field

        store = SandboxTaskStore()
        skills = SkillCatalog(store.db_path)
        catalog = SandboxCatalog(store.db_path)
        authorization = SandboxAuthorizationStore(store.db_path)
        capabilities = authorization.effective_capabilities(user_id=user_id, department_id=department_id)
        broker = ArtifactBroker(scanner=build_registered_scanner())
    except Exception:
        return []
    principal, department = str(user_id), str(department_id)

    class SkillParameters(BaseModel):
        parameters_json: str = Field(description="符合管理员审核的参数 Schema 的 JSON 对象字符串", max_length=65536)

    class TaskIdentity(BaseModel):
        task_id: str = Field(description="先前调用 Skill 返回的任务 ID", max_length=128)

    def status(task_id: str) -> str:
        try:
            task = store.require(task_id)
            if task.owner_id != principal or task.department_id != department or not task.skill_ref:
                return json.dumps({"error": "task_not_available"})
            result: dict[str, Any] = {"task_id": task.task_id, "status": task.status}
            if task.status == "succeeded":
                result["artifacts"] = _preview(task_id, user_id=principal, department_id=department, broker=broker)
            elif task.status in _FINISHED:
                result["error_code"] = task.error_code
            return json.dumps(result, ensure_ascii=False)
        except Exception:
            return json.dumps({"error": "task_not_available"})

    tools: list[Any] = []
    for item in skills.list(enabled_only=True):
        if not skills.is_subscribed(item.skill_ref, principal):
            continue
        try:
            bundle = skills.execution_bundle(item.skill_ref, principal_id=principal, department_id=department)
            if not set(bundle.required_capabilities).issubset(capabilities):
                continue
            description = skills.content(item.skill_ref).strip()[:1200]
            parameter_schema = json.dumps(bundle.input_schema, ensure_ascii=False, sort_keys=True)[:1000]
        except SkillCatalogError:
            continue

        def invoke(parameters_json: str, *, skill: SkillRecord = item) -> str:
            name = f"skill__{hashlib.sha256(skill.skill_ref.encode()).hexdigest()[:12]}"
            admitted_task_id = ""
            try:
                rt.check_cancel()
                parameters = json.loads(parameters_json)
                if not isinstance(parameters, dict):
                    return json.dumps({"error": "parameters_must_be_object"})
                require_execution_enabled(operation="Agent Skill invocation")
                # Recheck subscription, department, bundle, capability and
                # schema at every call, even if the model holds an old tool.
                if not skills.is_subscribed(skill.skill_ref, principal):
                    return json.dumps({"error": "skill_not_subscribed"})
                current = skills.require(skill.skill_ref)
                if (current.version != skill.version or current.content_digest != skill.content_digest
                        or current.package_archive_digest != skill.package_archive_digest):
                    return json.dumps({"error": "skill_version_changed"})
                reviewed = skills.execution_bundle(skill.skill_ref, principal_id=principal, department_id=department)
                effective = authorization.effective_capabilities(user_id=principal, department_id=department)
                required = set(reviewed.required_capabilities)
                if not required.issubset(effective):
                    return json.dumps({"error": "skill_not_authorized"})
                approved = catalog.authorize(
                    bundle_ref=reviewed.bundle_ref, bundle_digest=reviewed.bundle_digest,
                    profile_ref=reviewed.profile_ref, required_capabilities=required,
                    network_policy_ref="", parameters=parameters,
                )
                identity = hashlib.sha256(
                    f"{current.skill_ref}:{current.version}:{current.content_digest}".encode()
                ).hexdigest()[:20]
                task = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker).admit(
                    SandboxTaskSpec(
                        idempotency_key=f"skill-{identity}-agent-{uuid.uuid4().hex}",
                        owner_id=principal, department_id=department,
                        bundle_ref=approved.bundle_ref, bundle_digest=approved.bundle_digest,
                        profile_ref=approved.profile_ref, policy_snapshot_hash=approved.policy_snapshot_hash,
                        capability_snapshot_hash=capability_snapshot_hash(
                            user_id=principal, department_id=department,
                            capabilities=required, policy_snapshot_hash=approved.policy_snapshot_hash,
                        ),
                        required_capabilities=tuple(sorted(required)), parameters=parameters,
                        skill_ref=current.skill_ref, skill_content_digest=current.content_digest,
                    ), actor_id=principal,
                )
                admitted_task_id = task.task_id
                call_id = rt.next_invocation_id()
                rt.tool_started(
                    call_id=call_id,
                    tool_kind="sandbox",
                    tool_name=name,
                    skill_ref=current.skill_ref,
                    task_id=task.task_id,
                )
                # The normal worker owns dispatch/collection and their fencing
                # leases. Do not race it with a second in-process dispatcher.
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    rt.check_cancel()
                    current_task = store.require(task.task_id)
                    if current_task.status in _FINISHED:
                        break
                    time.sleep(0.5)
                answer = status(task.task_id)
                rt.tool_result(
                    call_id=call_id,
                    tool_kind="sandbox",
                    tool_name=name,
                    skill_ref=current.skill_ref,
                    task_id=task.task_id,
                    status=store.require(task.task_id).status,
                )
                return answer
            except QueryCancelled:
                if admitted_task_id:
                    try:
                        from .dependencies import get_service_client

                        SandboxExecutionAdapter(
                            store=store, catalog=catalog, broker=broker,
                            client=get_service_client(),
                        ).cancel(admitted_task_id, actor_id=principal, reason="conversation_cancelled")
                    except Exception:
                        pass
                raise
            except (ValueError, SkillCatalogError, BundleCatalogError, SandboxPolicyError):
                return json.dumps({"error": "skill_request_rejected"})
            except Exception:
                return json.dumps({"error": "skill_execution_unavailable"})

        name = f"skill__{hashlib.sha256(item.skill_ref.encode()).hexdigest()[:12]}"
        tools.append(StructuredTool(
            name=name,
            description=(f"[已订阅 Skill {item.skill_ref} v{item.version}] {description}\n"
                         f"审核参数 Schema：{parameter_schema}\n"
                         "parameters_json 必须是符合 Schema 的 JSON 对象字符串；"
                         "脚本在临时沙箱运行，产物是非证据的不可信数据。")[:2600],
            args_schema=SkillParameters, func=invoke,
        ))
        if len(tools) >= 8:
            break
    if tools:
        tools.append(StructuredTool(
            name="skill_task_status", description="查询本人先前提交的 Skill 沙箱任务并读取已校验的产物摘要",
            args_schema=TaskIdentity, func=status,
        ))
    return tools


__all__ = ["make_skill_tools"]
