"""Governed external-extension execution contracts.

This package contains only the HDB-side control-plane pieces.  It deliberately
does not import Docker/Podman SDKs and does not start subprocesses.  Runtime
control belongs to an independently deployed Sandbox Runner.
"""

from .artifact_broker import ArtifactBroker
from .artifacts import ArtifactManifest, ArtifactRejected
from .catalog import ExecutionBundle, SandboxCatalog
from .client import SandboxServiceClient
from .dependencies import configure_service_client_factory, get_service_client
from .models import SandboxTask, SandboxTaskSpec
from .policy import SandboxPolicyError, require_execution_enabled, validate_task_spec
from .reconciler import SandboxReconciler
from .service import SandboxExecutionAdapter
from .skills import SkillCatalog, SkillRecord
from .task_store import SandboxTaskStore

__all__ = [
    "SandboxPolicyError",
    "ArtifactBroker",
    "ArtifactManifest",
    "ArtifactRejected",
    "ExecutionBundle",
    "SandboxCatalog",
    "SandboxServiceClient",
    "configure_service_client_factory",
    "get_service_client",
    "SandboxReconciler",
    "SandboxExecutionAdapter",
    "SkillCatalog",
    "SkillRecord",
    "SandboxTask",
    "SandboxTaskSpec",
    "SandboxTaskStore",
    "require_execution_enabled",
    "validate_task_spec",
]
