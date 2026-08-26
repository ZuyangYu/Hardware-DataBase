"""SQLite persistence for the project-first governance contracts.

The migration ledger is deliberately local to this bounded context.  Each
migration is idempotent and can be run repeatedly on an existing deployment.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any, TypeVar

import config.settings
from src.projects.models import (
    LogicalDocument,
    ProcessingArtifact,
    Project,
    ProjectBaseline,
    ProjectKnowledgeBinding,
    ProjectPrincipalBinding,
    ProjectSourceBinding,
    SourceAsset,
    SourceRegionPolicy,
    SourceSetSnapshot,
    SourceVersion,
)


ModelT = TypeVar("ModelT")


def _as_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _from_json(value: str | None) -> dict[str, Any]:
    return json.loads(value or "{}")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class ProjectStore:
    """Durable project/source records with explicit immutable boundaries."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.path.join(config.settings.STORAGE_DIR, "projects.db")
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            if not self._migration_applied(conn, "001_project_scope"):
                self._apply_project_scope_migration(conn)
                conn.execute("INSERT INTO schema_migrations (migration_id) VALUES ('001_project_scope')")

    @staticmethod
    def _migration_applied(conn, migration_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_id = ?", (migration_id,)
        ).fetchone() is not None

    @staticmethod
    def _apply_project_scope_migration(conn) -> None:
        # payload JSON is retained in addition to searchable identity/scope
        # columns so contract upgrades stay backwards compatible.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                department_id TEXT NOT NULL, name TEXT NOT NULL,
                status TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_knowledge_bindings (
                binding_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL, kb_id TEXT NOT NULL, status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
            );
            CREATE INDEX IF NOT EXISTS idx_project_kb_scope
                ON project_knowledge_bindings(project_id, tenant_id, kb_id, status);
            CREATE TABLE IF NOT EXISTS project_principal_bindings (
                binding_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL, principal_type TEXT NOT NULL,
                principal_id TEXT NOT NULL, project_role TEXT NOT NULL,
                status TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
            );
            CREATE INDEX IF NOT EXISTS idx_project_principal_scope
                ON project_principal_bindings(project_id, tenant_id, principal_type, principal_id, status);
            CREATE TABLE IF NOT EXISTS source_assets (
                asset_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_asset_content_identity
                ON source_assets(tenant_id, content_hash);
            CREATE TABLE IF NOT EXISTS logical_documents (
                document_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                owner_department_id TEXT NOT NULL, document_role TEXT NOT NULL,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_versions (
                version_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                document_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                approval_status TEXT NOT NULL, effective_from TEXT, effective_to TEXT,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES logical_documents(document_id),
                FOREIGN KEY(asset_id) REFERENCES source_assets(asset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_source_version_document
                ON source_versions(tenant_id, document_id, approval_status);
            CREATE TABLE IF NOT EXISTS processing_artifacts (
                artifact_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                asset_id TEXT NOT NULL, status TEXT NOT NULL,
                processor_kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(asset_id) REFERENCES source_assets(asset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_processing_asset
                ON processing_artifacts(tenant_id, asset_id, status);
            CREATE TABLE IF NOT EXISTS project_source_bindings (
                binding_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL, version_id TEXT NOT NULL,
                usage_type TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id),
                FOREIGN KEY(version_id) REFERENCES source_versions(version_id)
            );
            CREATE INDEX IF NOT EXISTS idx_project_source_scope
                ON project_source_bindings(project_id, tenant_id, usage_type, status);
            CREATE TABLE IF NOT EXISTS project_baselines (
                baseline_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL, baseline_version INTEGER NOT NULL,
                content_hash TEXT NOT NULL, status TEXT NOT NULL,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(project_id, baseline_version),
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
            );
            CREATE TABLE IF NOT EXISTS source_region_policies (
                region_policy_id TEXT PRIMARY KEY, source_version_id TEXT NOT NULL,
                processing_artifact_id TEXT NOT NULL, decision TEXT NOT NULL,
                policy_version TEXT NOT NULL, payload_json TEXT NOT NULL,
                FOREIGN KEY(source_version_id) REFERENCES source_versions(version_id),
                FOREIGN KEY(processing_artifact_id) REFERENCES processing_artifacts(artifact_id)
            );
            CREATE INDEX IF NOT EXISTS idx_region_policy_source
                ON source_region_policies(source_version_id, processing_artifact_id, decision);
            CREATE TABLE IF NOT EXISTS source_set_snapshots (
                source_set_snapshot_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                work_order_id TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL,
                baseline_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id),
                FOREIGN KEY(baseline_id) REFERENCES project_baselines(baseline_id)
            );
            """
        )

    @staticmethod
    def _insert(conn, table: str, identifier: str, payload: Any, values: dict[str, Any]) -> None:
        columns = [identifier, *values.keys(), "payload_json"]
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            [getattr(payload, identifier), *values.values(), _as_json(payload)],
        )

    def create_project(self, project: Project) -> Project:
        with closing(self._connect()) as conn:
            self._insert(
                conn, "projects", "project_id", project,
                {"tenant_id": project.tenant_id, "department_id": project.department_id,
                 "name": project.name, "status": project.status,
                 "created_at": _iso(project.created_at), "updated_at": _iso(project.updated_at)},
            )
        return project

    def get_project(self, project_id: str, tenant_id: str | None = None) -> Project | None:
        sql = "SELECT payload_json FROM projects WHERE project_id = ?"
        params: list[Any] = [project_id]
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        with closing(self._connect()) as conn:
            row = conn.execute(sql, params).fetchone()
        return Project.model_validate(_from_json(row["payload_json"])) if row else None

    def list_projects(self, tenant_id: str, department_id: str | None = None) -> list[Project]:
        sql = "SELECT payload_json FROM projects WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if department_id:
            sql += " AND department_id = ?"
            params.append(department_id)
        sql += " ORDER BY name, project_id"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Project.model_validate(_from_json(row["payload_json"])) for row in rows]

    def add_knowledge_binding(self, binding: ProjectKnowledgeBinding) -> ProjectKnowledgeBinding:
        with closing(self._connect()) as conn:
            self._insert(conn, "project_knowledge_bindings", "binding_id", binding, {
                "tenant_id": binding.tenant_id, "project_id": binding.project_id,
                "kb_id": binding.kb_id, "status": binding.status,
            })
        return binding

    def list_knowledge_bindings(self, project_id: str, tenant_id: str, active_only: bool = True) -> list[ProjectKnowledgeBinding]:
        sql = "SELECT payload_json FROM project_knowledge_bindings WHERE project_id = ? AND tenant_id = ?"
        params: list[Any] = [project_id, tenant_id]
        if active_only:
            sql += " AND status = 'active'"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ProjectKnowledgeBinding.model_validate(_from_json(row["payload_json"])) for row in rows]

    def add_principal_binding(self, binding: ProjectPrincipalBinding) -> ProjectPrincipalBinding:
        with closing(self._connect()) as conn:
            self._insert(conn, "project_principal_bindings", "binding_id", binding, {
                "tenant_id": binding.tenant_id, "project_id": binding.project_id,
                "principal_type": binding.principal_type, "principal_id": binding.principal_id,
                "project_role": binding.project_role, "status": binding.status,
                "valid_from": _iso(binding.valid_from), "valid_to": _iso(binding.valid_to),
            })
        return binding

    def list_principal_bindings(self, project_id: str, tenant_id: str, principal_id: str | None = None) -> list[ProjectPrincipalBinding]:
        sql = "SELECT payload_json FROM project_principal_bindings WHERE project_id = ? AND tenant_id = ?"
        params: list[Any] = [project_id, tenant_id]
        if principal_id is not None:
            sql += " AND principal_id = ?"
            params.append(principal_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ProjectPrincipalBinding.model_validate(_from_json(row["payload_json"])) for row in rows]

    def create_source_asset(self, asset: SourceAsset) -> SourceAsset:
        with closing(self._connect()) as conn:
            self._insert(conn, "source_assets", "asset_id", asset, {
                "tenant_id": asset.tenant_id, "content_hash": asset.content_hash,
                "created_at": _iso(asset.created_at),
            })
        return asset

    def create_logical_document(self, document: LogicalDocument) -> LogicalDocument:
        with closing(self._connect()) as conn:
            self._insert(conn, "logical_documents", "document_id", document, {
                "tenant_id": document.tenant_id, "owner_department_id": document.owner_department_id,
                "document_role": document.document_role, "created_at": _iso(document.created_at),
            })
        return document

    def get_logical_document(self, document_id: str, tenant_id: str) -> LogicalDocument | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM logical_documents WHERE document_id = ? AND tenant_id = ?",
                (document_id, tenant_id),
            ).fetchone()
        return LogicalDocument.model_validate(_from_json(row["payload_json"])) if row else None

    def create_source_version(self, version: SourceVersion) -> SourceVersion:
        with closing(self._connect()) as conn:
            self._insert(conn, "source_versions", "version_id", version, {
                "tenant_id": version.tenant_id, "document_id": version.document_id,
                "asset_id": version.asset_id, "approval_status": version.approval_status,
                "effective_from": _iso(version.effective_from), "effective_to": _iso(version.effective_to),
                "created_at": _iso(version.created_at),
            })
        return version

    def get_source_version(self, version_id: str, tenant_id: str) -> SourceVersion | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM source_versions WHERE version_id = ? AND tenant_id = ?",
                (version_id, tenant_id),
            ).fetchone()
        return SourceVersion.model_validate(_from_json(row["payload_json"])) if row else None

    def create_processing_artifact(self, artifact: ProcessingArtifact) -> ProcessingArtifact:
        with closing(self._connect()) as conn:
            self._insert(conn, "processing_artifacts", "artifact_id", artifact, {
                "tenant_id": artifact.tenant_id, "asset_id": artifact.asset_id,
                "status": artifact.status, "processor_kind": artifact.processor_kind,
                "created_at": _iso(artifact.created_at),
            })
        return artifact

    def get_processing_artifact(self, artifact_id: str, tenant_id: str) -> ProcessingArtifact | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM processing_artifacts WHERE artifact_id = ? AND tenant_id = ?",
                (artifact_id, tenant_id),
            ).fetchone()
        return ProcessingArtifact.model_validate(_from_json(row["payload_json"])) if row else None

    def ready_artifacts_for_version(self, version_id: str, tenant_id: str) -> list[ProcessingArtifact]:
        version = self.get_source_version(version_id, tenant_id)
        if version is None:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT payload_json FROM processing_artifacts
                   WHERE tenant_id = ? AND asset_id = ? AND status = 'ready'
                   ORDER BY created_at DESC, artifact_id""",
                (tenant_id, version.asset_id),
            ).fetchall()
        return [ProcessingArtifact.model_validate(_from_json(row["payload_json"])) for row in rows]

    def add_project_source_binding(self, binding: ProjectSourceBinding) -> ProjectSourceBinding:
        with closing(self._connect()) as conn:
            self._insert(conn, "project_source_bindings", "binding_id", binding, {
                "tenant_id": binding.tenant_id, "project_id": binding.project_id,
                "version_id": binding.version_id, "usage_type": binding.usage_type,
                "status": binding.status,
            })
        return binding

    def list_project_source_bindings(self, project_id: str, tenant_id: str, active_only: bool = True) -> list[ProjectSourceBinding]:
        sql = "SELECT payload_json FROM project_source_bindings WHERE project_id = ? AND tenant_id = ?"
        params: list[Any] = [project_id, tenant_id]
        if active_only:
            sql += " AND status = 'active'"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ProjectSourceBinding.model_validate(_from_json(row["payload_json"])) for row in rows]

    def create_baseline(self, baseline: ProjectBaseline) -> ProjectBaseline:
        with closing(self._connect()) as conn:
            self._insert(conn, "project_baselines", "baseline_id", baseline, {
                "tenant_id": baseline.tenant_id, "project_id": baseline.project_id,
                "baseline_version": baseline.baseline_version, "content_hash": baseline.content_hash,
                "status": baseline.status, "created_at": _iso(baseline.created_at),
            })
        return baseline

    def get_baseline(self, baseline_id: str, tenant_id: str) -> ProjectBaseline | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM project_baselines WHERE baseline_id = ? AND tenant_id = ?",
                (baseline_id, tenant_id),
            ).fetchone()
        return ProjectBaseline.model_validate(_from_json(row["payload_json"])) if row else None

    def list_baselines(self, project_id: str, tenant_id: str, approved_only: bool = False) -> list[ProjectBaseline]:
        sql = "SELECT payload_json FROM project_baselines WHERE project_id = ? AND tenant_id = ?"
        params: list[Any] = [project_id, tenant_id]
        if approved_only:
            sql += " AND status IN ('approved', 'released')"
        sql += " ORDER BY baseline_version DESC, created_at DESC"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ProjectBaseline.model_validate(_from_json(row["payload_json"])) for row in rows]

    def add_region_policy(self, policy: SourceRegionPolicy) -> SourceRegionPolicy:
        with closing(self._connect()) as conn:
            self._insert(conn, "source_region_policies", "region_policy_id", policy, {
                "source_version_id": policy.source_version_id,
                "processing_artifact_id": policy.processing_artifact_id,
                "decision": policy.decision, "policy_version": policy.policy_version,
            })
        return policy

    def allowed_region_policies(self, version_id: str, artifact_id: str) -> list[SourceRegionPolicy]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT payload_json FROM source_region_policies
                   WHERE source_version_id = ? AND processing_artifact_id = ? AND decision = 'allow'""",
                (version_id, artifact_id),
            ).fetchall()
        return [SourceRegionPolicy.model_validate(_from_json(row["payload_json"])) for row in rows]

    def create_source_set_snapshot(self, snapshot: SourceSetSnapshot) -> SourceSetSnapshot:
        with closing(self._connect()) as conn:
            self._insert(conn, "source_set_snapshots", "source_set_snapshot_id", snapshot, {
                "tenant_id": snapshot.tenant_id, "work_order_id": snapshot.work_order_id,
                "project_id": snapshot.project_id, "baseline_id": snapshot.baseline_id,
                "content_hash": snapshot.content_hash, "created_at": _iso(snapshot.created_at),
            })
        return snapshot

    def get_source_set_snapshot(self, snapshot_id: str, tenant_id: str) -> SourceSetSnapshot | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM source_set_snapshots WHERE source_set_snapshot_id = ? AND tenant_id = ?",
                (snapshot_id, tenant_id),
            ).fetchone()
        return SourceSetSnapshot.model_validate(_from_json(row["payload_json"])) if row else None
