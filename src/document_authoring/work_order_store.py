"""Persistent template/work-order/artifact records for the P2a workflow."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import config.settings
from src.document_authoring.harness.policy import HarnessLeaseLost
from src.document_authoring.models import (
    DeterministicRuleSpec,
    DocumentUnitDraft,
    DocumentArtifact,
    DocumentHumanEvent,
    DocumentOutboxEvent,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessCheckpoint,
    HarnessPolicy,
    HarnessRun,
    LegacyTemplateClaim,
    NodeExecutionReceipt,
    AuthoringRunManifest,
    content_hash,
    RendererPolicy,
    TemplateSecurityReport,
    TemplateUnitBinding,
    TemplateVersion,
    ValidationReport,
    WorkbookRegionSchema,
)
from src.document_authoring.template_analysis import DocxRegionSchema, TemplateAnalysis


ModelT = TypeVar("ModelT")


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload(row) -> dict[str, Any]:
    return json.loads(row["payload_json"])


class DocumentAuthoringStore:
    def __init__(self, db_path: str | None = None, artifact_root: str | None = None):
        self.db_path = db_path or os.path.join(config.settings.STORAGE_DIR, "document_authoring.db")
        self.artifact_root = artifact_root or os.path.join(config.settings.STORAGE_DIR, "document_authoring")
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        os.makedirs(self.artifact_root, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS template_versions (
                    template_version_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    content_hash TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS template_analyses (
                    template_version_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(template_version_id) REFERENCES template_versions(template_version_id)
                );
                CREATE TABLE IF NOT EXISTS template_security_reports (
                    report_id TEXT PRIMARY KEY, template_version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(template_version_id) REFERENCES template_versions(template_version_id)
                );
                CREATE TABLE IF NOT EXISTS renderer_policies (
                    renderer_policy_id TEXT NOT NULL, version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(renderer_policy_id, version)
                );
                CREATE TABLE IF NOT EXISTS document_schemas (
                    document_schema_id TEXT NOT NULL, version TEXT NOT NULL,
                    status TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(document_schema_id, version)
                );
                CREATE TABLE IF NOT EXISTS workbook_regions (
                    region_id TEXT PRIMARY KEY, template_schema_id TEXT NOT NULL,
                    template_schema_version TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS docx_regions (
                    region_id TEXT NOT NULL, template_schema_id TEXT NOT NULL,
                    template_schema_version TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(template_schema_id, template_schema_version, region_id)
                );
                CREATE TABLE IF NOT EXISTS template_unit_bindings (
                    binding_id TEXT PRIMARY KEY, template_schema_id TEXT NOT NULL,
                    template_schema_version TEXT NOT NULL, semantic_unit_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deterministic_rule_specs (
                    rule_id TEXT NOT NULL, rule_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY(rule_id, rule_version)
                );
                CREATE TABLE IF NOT EXISTS harness_policies (
                    harness_policy_id TEXT NOT NULL, version TEXT NOT NULL,
                    status TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(harness_policy_id, version)
                );
                CREATE TABLE IF NOT EXISTS document_work_orders (
                    work_order_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL, status TEXT NOT NULL,
                    idempotency_key TEXT, payload_json TEXT NOT NULL,
                    UNIQUE(tenant_id, project_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS evidence_matrices (
                    work_order_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
                    FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id)
                );
                CREATE TABLE IF NOT EXISTS authoring_run_manifests (
                    run_manifest_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id)
                );
                CREATE TABLE IF NOT EXISTS harness_runs (
                    harness_run_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id)
                );
                CREATE INDEX IF NOT EXISTS idx_harness_runs_work_order
                    ON harness_runs(work_order_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS document_unit_drafts (
                    work_order_id TEXT NOT NULL, harness_run_id TEXT NOT NULL,
                    unit_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(harness_run_id, unit_id),
                    FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id),
                    FOREIGN KEY(harness_run_id) REFERENCES harness_runs(harness_run_id)
                );
                CREATE TABLE IF NOT EXISTS harness_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, harness_run_id TEXT NOT NULL,
                    work_order_id TEXT NOT NULL, status TEXT NOT NULL,
                    updated_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    FOREIGN KEY(harness_run_id) REFERENCES harness_runs(harness_run_id),
                    FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id)
                );
                CREATE INDEX IF NOT EXISTS idx_harness_checkpoints_run
                    ON harness_checkpoints(harness_run_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS node_execution_receipts (
                    receipt_id TEXT PRIMARY KEY, harness_run_id TEXT NOT NULL,
                    node_name TEXT NOT NULL, unit_id TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(harness_run_id, node_name, unit_id, input_fingerprint),
                    FOREIGN KEY(harness_run_id) REFERENCES harness_runs(harness_run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_node_execution_receipts_run
                    ON node_execution_receipts(harness_run_id, status);
                CREATE TABLE IF NOT EXISTS legacy_template_claims (
                    template_version_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(template_version_id, claim_id),
                    FOREIGN KEY(template_version_id) REFERENCES template_versions(template_version_id)
                );
                CREATE TABLE IF NOT EXISTS validation_reports (
                    validation_report_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id)
                );
                CREATE TABLE IF NOT EXISTS document_artifacts (
                    artifact_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    work_order_id TEXT NOT NULL, stage TEXT NOT NULL,
                    content_hash TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id)
                );
                CREATE INDEX IF NOT EXISTS idx_document_artifacts_work_order
                    ON document_artifacts(work_order_id, stage, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_document_artifacts_idempotency
                    ON document_artifacts(json_extract(payload_json, '$.idempotency_fingerprint'))
                    WHERE json_extract(payload_json, '$.idempotency_fingerprint') IS NOT NULL
                      AND json_extract(payload_json, '$.idempotency_fingerprint') != '';
                CREATE TABLE IF NOT EXISTS document_human_events (
                    event_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(work_order_id) REFERENCES document_work_orders(work_order_id),
                    FOREIGN KEY(artifact_id) REFERENCES document_artifacts(artifact_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_document_human_events_idempotency
                    ON document_human_events(json_extract(payload_json, '$.idempotency_fingerprint'))
                    WHERE json_extract(payload_json, '$.idempotency_fingerprint') IS NOT NULL
                      AND json_extract(payload_json, '$.idempotency_fingerprint') != '';
                CREATE TABLE IF NOT EXISTS document_outbox_events (
                    event_id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_document_outbox_events_pending
                    ON document_outbox_events(status, created_at, event_id);
                """
            )
            self._migrate_docx_regions(conn)

    @staticmethod
    def _migrate_docx_regions(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(docx_regions)").fetchall()
        primary_key = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
        expected_key = ["template_schema_id", "template_schema_version", "region_id"]
        if primary_key == expected_key:
            return
        conn.execute("BEGIN")
        try:
            conn.execute("ALTER TABLE docx_regions RENAME TO docx_regions_legacy")
            conn.execute(
                """CREATE TABLE docx_regions (
                    region_id TEXT NOT NULL, template_schema_id TEXT NOT NULL,
                    template_schema_version TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(template_schema_id, template_schema_version, region_id)
                )"""
            )
            conn.execute(
                """INSERT INTO docx_regions
                    (region_id, template_schema_id, template_schema_version, payload_json)
                    SELECT region_id, template_schema_id, template_schema_version, payload_json
                    FROM docx_regions_legacy"""
            )
            conn.execute("DROP TABLE docx_regions_legacy")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _put(conn, table: str, columns: dict[str, Any], payload: Any) -> None:
        names = [*columns, "payload_json"]
        conn.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            [*columns.values(), _json(payload)],
        )

    def save_template(self, template: TemplateVersion, content: bytes, report: TemplateSecurityReport) -> TemplateVersion:
        extension = template.format
        target = self._storage_path("templates", f"{template.template_version_id}.{extension}")
        self._atomic_write(target, content)
        template = template.model_copy(update={"storage_ref": target, "security_report_id": report.report_id})
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                self._put(conn, "template_versions", {
                    "template_version_id": template.template_version_id, "status": template.status,
                    "content_hash": template.content_hash,
                }, template)
                self._put(conn, "template_security_reports", {
                    "report_id": report.report_id, "template_version_id": template.template_version_id,
                }, report)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return template

    def get_template(self, template_version_id: str) -> TemplateVersion | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM template_versions WHERE template_version_id = ?", (template_version_id,)).fetchone()
        return TemplateVersion.model_validate(_payload(row)) if row else None

    def save_template_analysis(self, analysis: TemplateAnalysis) -> TemplateAnalysis:
        template = self.get_template(analysis.template_version_id)
        if template is None:
            raise KeyError(f"template not found: {analysis.template_version_id}")
        if analysis.content_hash != template.content_hash:
            raise ValueError("template analysis content hash does not match template content hash")
        if analysis.format != template.format:
            raise ValueError("template analysis format does not match template format")
        analysis.validate_suggestions()
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO template_analyses (template_version_id, content_hash, payload_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(template_version_id) DO UPDATE SET
                       content_hash = excluded.content_hash, payload_json = excluded.payload_json""",
                (analysis.template_version_id, analysis.content_hash, _json(analysis)),
            )
        return analysis

    def get_template_analysis(self, template_version_id: str) -> TemplateAnalysis | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json, content_hash FROM template_analyses WHERE template_version_id = ?",
                (template_version_id,),
            ).fetchone()
        if row is None:
            return None
        analysis = TemplateAnalysis.model_validate(_payload(row))
        if analysis.template_version_id != template_version_id:
            raise ValueError("template analysis template version does not match persistence key")
        template = self.get_template(template_version_id)
        if template is None:
            raise KeyError(f"template not found: {template_version_id}")
        if (
            row["content_hash"] != template.content_hash
            or analysis.content_hash != row["content_hash"]
            or analysis.content_hash != template.content_hash
        ):
            raise ValueError("template analysis content hash does not match template content hash")
        if analysis.format != template.format:
            raise ValueError("template analysis format does not match template format")
        analysis.validate_suggestions()
        return analysis

    def get_template_analysis_by_id(self, analysis_id: str) -> TemplateAnalysis | None:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM template_analyses WHERE json_extract(payload_json, '$.analysis_id') = ?",
                (analysis_id,),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("template analysis id is not unique")
        analysis = TemplateAnalysis.model_validate(_payload(rows[0]))
        return self.get_template_analysis(analysis.template_version_id)

    def list_templates(self, approved_only: bool = False) -> list[TemplateVersion]:
        sql = "SELECT payload_json FROM template_versions"
        if approved_only:
            sql += " WHERE status = 'approved'"
        sql += " ORDER BY template_id, template_version_id"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql).fetchall()
        return [TemplateVersion.model_validate(_payload(row)) for row in rows]

    def read_template_content(self, template_version_id: str) -> bytes:
        template = self.get_template(template_version_id)
        if template is None or not template.storage_ref:
            raise KeyError(f"template not found: {template_version_id}")
        return Path(template.storage_ref).read_bytes()

    def get_template_security_report(self, template_version_id: str) -> TemplateSecurityReport | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM template_security_reports WHERE template_version_id = ?", (template_version_id,)
            ).fetchone()
        return TemplateSecurityReport.model_validate(_payload(row)) if row else None

    def save_legacy_template_claims(self, template_version_id: str, claims: list[LegacyTemplateClaim]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM legacy_template_claims WHERE template_version_id = ?", (template_version_id,))
                for claim in claims:
                    self._put(conn, "legacy_template_claims", {
                        "template_version_id": template_version_id, "claim_id": claim.claim_id,
                    }, claim)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_legacy_template_claims(self, template_version_id: str) -> list[LegacyTemplateClaim]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM legacy_template_claims WHERE template_version_id = ? ORDER BY claim_id",
                (template_version_id,),
            ).fetchall()
        return [LegacyTemplateClaim.model_validate(_payload(row)) for row in rows]

    def replace_template(self, template: TemplateVersion) -> TemplateVersion:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE template_versions SET status = ?, payload_json = ? WHERE template_version_id = ?",
                (template.status, _json(template), template.template_version_id),
            )
        return template

    def activate_template_analysis(
        self,
        *,
        template: TemplateVersion,
        schema: DocumentSchema,
        regions: list[WorkbookRegionSchema] | list[DocxRegionSchema],
        bindings: list[TemplateUnitBinding],
    ) -> TemplateVersion:
        """Approve the hash-checked template and its generated schema atomically."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                current = conn.execute(
                    "SELECT content_hash FROM template_versions WHERE template_version_id = ?",
                    (template.template_version_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(f"template not found: {template.template_version_id}")
                if current["content_hash"] != template.content_hash:
                    raise ValueError("template content hash changed before activation")
                conn.execute(
                    "UPDATE template_versions SET status = ?, payload_json = ? WHERE template_version_id = ?",
                    (template.status, _json(template), template.template_version_id),
                )
                self._put(conn, "document_schemas", {
                    "document_schema_id": schema.document_schema_id, "version": schema.version,
                    "status": schema.status,
                }, schema)
                for region in regions:
                    if isinstance(region, DocxRegionSchema):
                        self._put(conn, "docx_regions", {
                            "region_id": region.region_id, "template_schema_id": template.template_schema_id,
                            "template_schema_version": template.template_schema_version,
                        }, region)
                    else:
                        self._put(conn, "workbook_regions", {
                            "region_id": region.region_id, "template_schema_id": template.template_schema_id,
                            "template_schema_version": template.template_schema_version,
                        }, region)
                for binding in bindings:
                    self._put(conn, "template_unit_bindings", {
                        "binding_id": binding.binding_id, "template_schema_id": binding.template_schema_id,
                        "template_schema_version": binding.template_schema_version,
                        "semantic_unit_id": binding.semantic_unit_id,
                    }, binding)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return template

    def save_renderer_policy(self, policy: RendererPolicy) -> RendererPolicy:
        with closing(self._connect()) as conn:
            self._put(conn, "renderer_policies", {
                "renderer_policy_id": policy.renderer_policy_id, "version": policy.version,
            }, policy)
        return policy

    def get_renderer_policy(self, policy_id: str, version: str | None = None) -> RendererPolicy | None:
        sql = "SELECT payload_json FROM renderer_policies WHERE renderer_policy_id = ?"
        params: list[Any] = [policy_id]
        if version is not None:
            sql += " AND version = ?"
            params.append(version)
        else:
            sql += " ORDER BY version DESC LIMIT 1"
        with closing(self._connect()) as conn:
            row = conn.execute(sql, params).fetchone()
        return RendererPolicy.model_validate(_payload(row)) if row else None

    def save_document_schema(self, schema: DocumentSchema) -> DocumentSchema:
        with closing(self._connect()) as conn:
            self._put(conn, "document_schemas", {
                "document_schema_id": schema.document_schema_id, "version": schema.version, "status": schema.status,
            }, schema)
        return schema

    def get_document_schema(self, schema_id: str, version: str) -> DocumentSchema | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM document_schemas WHERE document_schema_id = ? AND version = ?", (schema_id, version)
            ).fetchone()
        return DocumentSchema.model_validate(_payload(row)) if row else None

    def list_document_schemas(self, approved_only: bool = False) -> list[DocumentSchema]:
        sql = "SELECT payload_json FROM document_schemas"
        if approved_only:
            sql += " WHERE status = 'approved'"
        sql += " ORDER BY document_schema_id, version"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql).fetchall()
        return [DocumentSchema.model_validate(_payload(row)) for row in rows]

    def save_workbook_regions(self, template_schema_id: str, template_schema_version: str, regions: list[WorkbookRegionSchema]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                for region in regions:
                    self._put(conn, "workbook_regions", {
                        "region_id": region.region_id, "template_schema_id": template_schema_id,
                        "template_schema_version": template_schema_version,
                    }, region)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_workbook_regions(self, template_schema_id: str, version: str) -> list[WorkbookRegionSchema]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT payload_json FROM workbook_regions
                   WHERE template_schema_id = ? AND template_schema_version = ? ORDER BY region_id""",
                (template_schema_id, version),
            ).fetchall()
        return [WorkbookRegionSchema.model_validate(_payload(row)) for row in rows]

    def save_docx_regions(self, template_schema_id: str, template_schema_version: str, regions: list[DocxRegionSchema]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                for region in regions:
                    self._put(conn, "docx_regions", {
                        "region_id": region.region_id, "template_schema_id": template_schema_id,
                        "template_schema_version": template_schema_version,
                    }, region)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_docx_regions(self, template_schema_id: str, version: str) -> list[DocxRegionSchema]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT payload_json FROM docx_regions
                   WHERE template_schema_id = ? AND template_schema_version = ? ORDER BY region_id""",
                (template_schema_id, version),
            ).fetchall()
        return [DocxRegionSchema.model_validate(_payload(row)) for row in rows]

    def save_unit_bindings(self, bindings: list[TemplateUnitBinding]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                for binding in bindings:
                    self._put(conn, "template_unit_bindings", {
                        "binding_id": binding.binding_id, "template_schema_id": binding.template_schema_id,
                        "template_schema_version": binding.template_schema_version,
                        "semantic_unit_id": binding.semantic_unit_id,
                    }, binding)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_unit_bindings(self, schema_id: str, version: str) -> list[TemplateUnitBinding]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT payload_json FROM template_unit_bindings
                   WHERE template_schema_id = ? AND template_schema_version = ? ORDER BY binding_id""",
                (schema_id, version),
            ).fetchall()
        return [TemplateUnitBinding.model_validate(_payload(row)) for row in rows]

    def save_rule_spec(self, spec: DeterministicRuleSpec) -> DeterministicRuleSpec:
        with closing(self._connect()) as conn:
            self._put(conn, "deterministic_rule_specs", {
                "rule_id": spec.rule_id, "rule_version": spec.rule_version,
            }, spec)
        return spec

    def get_rule_spec(self, rule_id: str) -> DeterministicRuleSpec | None:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM deterministic_rule_specs WHERE rule_id = ?", (rule_id,)
            ).fetchall()
        if len(rows) != 1:
            return None
        return DeterministicRuleSpec.model_validate(_payload(rows[0]))

    def save_harness_policy(self, policy: HarnessPolicy) -> HarnessPolicy:
        with closing(self._connect()) as conn:
            self._put(conn, "harness_policies", {
                "harness_policy_id": policy.harness_policy_id, "version": policy.version, "status": policy.status,
            }, policy)
        return policy

    def get_harness_policy(self, policy_id: str, version: str | None = None) -> HarnessPolicy | None:
        sql = "SELECT payload_json FROM harness_policies WHERE harness_policy_id = ?"
        params: list[Any] = [policy_id]
        if version:
            sql += " AND version = ?"
            params.append(version)
        else:
            sql += " ORDER BY version DESC LIMIT 1"
        with closing(self._connect()) as conn:
            row = conn.execute(sql, params).fetchone()
        return HarnessPolicy.model_validate(_payload(row)) if row else None

    def list_harness_policies(self, approved_only: bool = False) -> list[HarnessPolicy]:
        sql = "SELECT payload_json FROM harness_policies"
        if approved_only:
            sql += " WHERE status = 'approved'"
        sql += " ORDER BY harness_policy_id, version"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql).fetchall()
        return [HarnessPolicy.model_validate(_payload(row)) for row in rows]

    def create_work_order(self, work_order: DocumentWorkOrder) -> DocumentWorkOrder:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._put(conn, "document_work_orders", {
                    "work_order_id": work_order.work_order_id, "tenant_id": work_order.tenant_id,
                    "project_id": work_order.project_id, "status": work_order.status,
                    "idempotency_key": work_order.idempotency_key,
                }, work_order)
                self._put_outbox_event(conn, self._work_order_outbox_event(work_order, "created"))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return work_order

    def find_work_order_by_idempotency(self, tenant_id: str, project_id: str, key: str) -> DocumentWorkOrder | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT payload_json FROM document_work_orders
                   WHERE tenant_id = ? AND project_id = ? AND idempotency_key = ?""",
                (tenant_id, project_id, key),
            ).fetchone()
        return DocumentWorkOrder.model_validate(_payload(row)) if row else None

    def get_work_order(self, work_order_id: str) -> DocumentWorkOrder | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM document_work_orders WHERE work_order_id = ?", (work_order_id,)).fetchone()
        return DocumentWorkOrder.model_validate(_payload(row)) if row else None

    def replace_work_order(self, order: DocumentWorkOrder) -> DocumentWorkOrder:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute(
                    """UPDATE document_work_orders SET status = ?, payload_json = ?
                       WHERE work_order_id = ? AND json_extract(payload_json, '$.lock_version') = ?""",
                    (order.status, _json(order), order.work_order_id, order.lock_version - 1),
                ).rowcount
                if updated == 1:
                    self._put_outbox_event(conn, self._work_order_outbox_event(order, "state_changed"))
                if updated != 1:
                    raise RuntimeError("work order changed concurrently")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return order

    def save_evidence_matrix(self, work_order_id: str, rows: list[dict[str, Any]]) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO evidence_matrices (work_order_id, payload_json) VALUES (?, ?)
                   ON CONFLICT(work_order_id) DO UPDATE SET payload_json = excluded.payload_json""",
                (work_order_id, _json(rows)),
            )

    def get_evidence_matrix(self, work_order_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM evidence_matrices WHERE work_order_id = ?", (work_order_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else []

    def save_run_manifest(self, manifest: AuthoringRunManifest) -> AuthoringRunManifest:
        with closing(self._connect()) as conn:
            self._put(conn, "authoring_run_manifests", {
                "run_manifest_id": manifest.run_manifest_id, "work_order_id": manifest.work_order_id,
            }, manifest)
        return manifest

    def get_run_manifest(self, run_manifest_id: str) -> AuthoringRunManifest | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM authoring_run_manifests WHERE run_manifest_id = ?",
                (run_manifest_id,),
            ).fetchone()
        return AuthoringRunManifest.model_validate(_payload(row)) if row else None

    def replace_run_manifest(self, manifest: AuthoringRunManifest) -> AuthoringRunManifest:
        with closing(self._connect()) as conn:
            updated = conn.execute(
                "UPDATE authoring_run_manifests SET payload_json = ? WHERE run_manifest_id = ?",
                (_json(manifest), manifest.run_manifest_id),
            ).rowcount
        if updated != 1:
            raise KeyError(f"run manifest not found: {manifest.run_manifest_id}")
        return manifest

    def create_harness_run(self, run: HarnessRun) -> HarnessRun:
        with closing(self._connect()) as conn:
            self._put(conn, "harness_runs", {
                "harness_run_id": run.harness_run_id, "work_order_id": run.work_order_id,
                "status": run.status, "created_at": run.created_at.isoformat(),
            }, run)
        return run

    def get_harness_run(self, run_id: str) -> HarnessRun | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (run_id,)).fetchone()
        return HarnessRun.model_validate(_payload(row)) if row else None

    def list_harness_runs(self, work_order_id: str) -> list[HarnessRun]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM harness_runs WHERE work_order_id = ? ORDER BY created_at",
                (work_order_id,),
            ).fetchall()
        return [HarnessRun.model_validate(_payload(row)) for row in rows]

    def replace_harness_run(self, run: HarnessRun) -> HarnessRun:
        with closing(self._connect()) as conn:
            updated = conn.execute(
                "UPDATE harness_runs SET status = ?, payload_json = ? WHERE harness_run_id = ?",
                (run.status, _json(run), run.harness_run_id),
            ).rowcount
        if updated != 1:
            raise KeyError(f"harness run not found: {run.harness_run_id}")
        return run

    def claim_harness_run(self, run_id: str, lease_owner: str, lease_seconds: int) -> HarnessRun:
        """Claim a queued/retrying run and advance its fencing token atomically."""
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"harness run not found: {run_id}")
                current = HarnessRun.model_validate(_payload(row))
                if current.status not in {"planned", "queued", "retrying"}:
                    raise ValueError(f"harness run cannot be claimed from status {current.status}")
                if current.lease_expires_at and current.lease_expires_at > now:
                    raise RuntimeError("harness run already has an active lease")
                claimed = current.model_copy(update={
                    "status": "running", "lease_owner": lease_owner,
                    "fencing_token": current.fencing_token + 1,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "heartbeat_at": now, "updated_at": now, "error": None, "last_error_code": None,
                })
                conn.execute(
                    "UPDATE harness_runs SET status = ?, payload_json = ? WHERE harness_run_id = ?",
                    (claimed.status, _json(claimed), run_id),
                )
                conn.execute("COMMIT")
                return claimed
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def update_harness_run_owned(
        self,
        run_id: str,
        owner: str,
        token: int,
        **updates: Any,
    ) -> HarnessRun:
        """Update a run only while the caller owns its unexpired fencing lease."""
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"harness run not found: {run_id}")
                current = HarnessRun.model_validate(_payload(row))
                self._require_active_lease(current, owner, token, now)
                revised = current.model_copy(update={**updates, "updated_at": now})
                conn.execute(
                    "UPDATE harness_runs SET status = ?, payload_json = ? WHERE harness_run_id = ?",
                    (revised.status, _json(revised), run_id),
                )
                conn.execute("COMMIT")
                return revised
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def heartbeat_harness_run(
        self,
        run_id: str,
        lease_owner: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> HarnessRun:
        now = datetime.now(timezone.utc)
        return self.update_harness_run_owned(
            run_id,
            lease_owner,
            fencing_token,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )

    def request_harness_run_state(self, run_id: str, target_status: str) -> HarnessRun:
        """Pause/cancel a run and revoke any old worker lease via fencing."""
        if target_status not in {"paused", "cancelled"}:
            raise ValueError("only paused or cancelled HarnessRun states may be requested")
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"harness run not found: {run_id}")
                current = HarnessRun.model_validate(_payload(row))
                if current.status in {"completed", "cancelled"}:
                    raise ValueError(f"harness run cannot be changed from status {current.status}")
                revised = current.model_copy(update={
                    "status": target_status, "lease_owner": None,
                    "lease_expires_at": None, "heartbeat_at": now,
                    "fencing_token": current.fencing_token + 1, "updated_at": now,
                })
                conn.execute(
                    "UPDATE harness_runs SET status = ?, payload_json = ? WHERE harness_run_id = ?",
                    (revised.status, _json(revised), run_id),
                )
                conn.execute("COMMIT")
                return revised
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def queue_harness_retry(self, run_id: str, max_retries: int) -> HarnessRun:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"harness run not found: {run_id}")
                current = HarnessRun.model_validate(_payload(row))
                if current.status not in {"failed", "paused"}:
                    raise ValueError(f"harness run cannot be retried from status {current.status}")
                if current.retry_count >= max_retries:
                    raise ValueError("harness retry budget is exhausted")
                now = datetime.now(timezone.utc)
                revised = current.model_copy(update={
                    "status": "retrying", "retry_count": current.retry_count + 1,
                    "max_retries": max_retries, "lease_owner": None, "lease_expires_at": None,
                    "error": None, "last_error_code": None, "updated_at": now,
                })
                conn.execute(
                    "UPDATE harness_runs SET status = ?, payload_json = ? WHERE harness_run_id = ?",
                    (revised.status, _json(revised), run_id),
                )
                conn.execute("COMMIT")
                return revised
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def save_harness_checkpoint_owned(
        self,
        checkpoint: HarnessCheckpoint,
        lease_owner: str,
        fencing_token: int,
    ) -> HarnessCheckpoint:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._get_run_for_update(conn, checkpoint.harness_run_id)
                self._require_active_lease(run, lease_owner, fencing_token, datetime.now(timezone.utc))
                existing = conn.execute(
                    "SELECT 1 FROM harness_checkpoints WHERE checkpoint_id = ?", (checkpoint.checkpoint_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE harness_checkpoints SET status = ?, updated_at = ?, payload_json = ? WHERE checkpoint_id = ?",
                        (checkpoint.status, checkpoint.updated_at.isoformat(), _json(checkpoint), checkpoint.checkpoint_id),
                    )
                else:
                    self._put(conn, "harness_checkpoints", {
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "harness_run_id": checkpoint.harness_run_id,
                        "work_order_id": checkpoint.work_order_id,
                        "status": checkpoint.status,
                        "updated_at": checkpoint.updated_at.isoformat(),
                    }, checkpoint)
                conn.execute("COMMIT")
                return checkpoint
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_harness_checkpoint(self, checkpoint_id: str) -> HarnessCheckpoint | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM harness_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
        return HarnessCheckpoint.model_validate(_payload(row)) if row else None

    def finalize_harness_checkpoint(self, checkpoint_id: str, status: str) -> HarnessCheckpoint:
        if status not in {"paused", "waiting_human", "failed", "completed", "cancelled"}:
            raise ValueError("invalid terminal checkpoint status")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM harness_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"harness checkpoint not found: {checkpoint_id}")
            checkpoint = HarnessCheckpoint.model_validate(_payload(row)).model_copy(update={
                "status": status, "updated_at": datetime.now(timezone.utc),
            })
            conn.execute(
                "UPDATE harness_checkpoints SET status = ?, updated_at = ?, payload_json = ? WHERE checkpoint_id = ?",
                (checkpoint.status, checkpoint.updated_at.isoformat(), _json(checkpoint), checkpoint_id),
            )
        return checkpoint

    def begin_node_execution_owned(
        self,
        receipt: NodeExecutionReceipt,
        lease_owner: str,
        fencing_token: int,
    ) -> NodeExecutionReceipt:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._get_run_for_update(conn, receipt.harness_run_id)
                self._require_active_lease(run, lease_owner, fencing_token, datetime.now(timezone.utc))
                row = conn.execute(
                    """SELECT payload_json FROM node_execution_receipts
                       WHERE harness_run_id = ? AND node_name = ? AND unit_id = ? AND input_fingerprint = ?""",
                    (receipt.harness_run_id, receipt.node_name, receipt.unit_id, receipt.input_fingerprint),
                ).fetchone()
                if row is not None:
                    existing = NodeExecutionReceipt.model_validate(_payload(row))
                    if existing.status == "committed":
                        conn.execute("COMMIT")
                        return existing
                    receipt = existing.model_copy(update={
                        "status": "started", "fencing_token": fencing_token, "error": None,
                    })
                    conn.execute(
                        "UPDATE node_execution_receipts SET status = ?, payload_json = ? WHERE receipt_id = ?",
                        (receipt.status, _json(receipt), receipt.receipt_id),
                    )
                else:
                    self._put(conn, "node_execution_receipts", {
                        "receipt_id": receipt.receipt_id,
                        "harness_run_id": receipt.harness_run_id,
                        "node_name": receipt.node_name,
                        "unit_id": receipt.unit_id,
                        "input_fingerprint": receipt.input_fingerprint,
                        "status": receipt.status,
                    }, receipt)
                conn.execute("COMMIT")
                return receipt
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def commit_node_execution_owned(
        self,
        receipt_id: str,
        harness_run_id: str,
        lease_owner: str,
        fencing_token: int,
        output_payload: dict[str, Any],
    ) -> NodeExecutionReceipt:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._get_run_for_update(conn, harness_run_id)
                self._require_active_lease(run, lease_owner, fencing_token, datetime.now(timezone.utc))
                row = conn.execute(
                    "SELECT payload_json FROM node_execution_receipts WHERE receipt_id = ?", (receipt_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"node receipt not found: {receipt_id}")
                current = NodeExecutionReceipt.model_validate(_payload(row))
                if current.status == "committed":
                    conn.execute("COMMIT")
                    return current
                committed = current.model_copy(update={
                    "status": "committed", "fencing_token": fencing_token,
                    "output_payload": output_payload, "output_hash": content_hash(output_payload),
                    "error": None, "committed_at": datetime.now(timezone.utc),
                })
                conn.execute(
                    "UPDATE node_execution_receipts SET status = ?, payload_json = ? WHERE receipt_id = ?",
                    (committed.status, _json(committed), receipt_id),
                )
                conn.execute("COMMIT")
                return committed
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def fail_node_execution_owned(
        self,
        receipt_id: str,
        harness_run_id: str,
        lease_owner: str,
        fencing_token: int,
        error: dict[str, Any],
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._get_run_for_update(conn, harness_run_id)
                self._require_active_lease(run, lease_owner, fencing_token, datetime.now(timezone.utc))
                row = conn.execute(
                    "SELECT payload_json FROM node_execution_receipts WHERE receipt_id = ?", (receipt_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"node receipt not found: {receipt_id}")
                failed = NodeExecutionReceipt.model_validate(_payload(row)).model_copy(update={
                    "status": "failed", "fencing_token": fencing_token, "error": error,
                })
                conn.execute(
                    "UPDATE node_execution_receipts SET status = ?, payload_json = ? WHERE receipt_id = ?",
                    (failed.status, _json(failed), receipt_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _require_active_lease(
        run: HarnessRun,
        lease_owner: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        if (
            run.status != "running"
            or run.lease_owner != lease_owner
            or run.fencing_token != fencing_token
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise HarnessLeaseLost("harness lease is no longer active")

    @staticmethod
    def _get_run_for_update(conn, run_id: str) -> HarnessRun:
        row = conn.execute("SELECT payload_json FROM harness_runs WHERE harness_run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"harness run not found: {run_id}")
        return HarnessRun.model_validate(_payload(row))

    def save_unit_drafts(
        self,
        work_order_id: str,
        harness_run_id: str,
        drafts: list[DocumentUnitDraft],
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if lease_owner is not None or fencing_token is not None:
                    if not lease_owner or fencing_token is None:
                        raise ValueError("both lease_owner and fencing_token are required for fenced draft writes")
                    run = self._get_run_for_update(conn, harness_run_id)
                    self._require_active_lease(run, lease_owner, fencing_token, datetime.now(timezone.utc))
                for draft in drafts:
                    conn.execute(
                        """INSERT INTO document_unit_drafts (work_order_id, harness_run_id, unit_id, payload_json)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(harness_run_id, unit_id) DO UPDATE SET payload_json = excluded.payload_json""",
                        (work_order_id, harness_run_id, draft.unit_id, _json(draft)),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_unit_drafts(self, harness_run_id: str) -> list[DocumentUnitDraft]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM document_unit_drafts WHERE harness_run_id = ? ORDER BY unit_id",
                (harness_run_id,),
            ).fetchall()
        return [DocumentUnitDraft.model_validate(_payload(row)) for row in rows]

    def save_validation_report(self, report: ValidationReport) -> ValidationReport:
        with closing(self._connect()) as conn:
            self._put(conn, "validation_reports", {
                "validation_report_id": report.validation_report_id, "work_order_id": report.work_order_id,
            }, report)
        return report

    def get_validation_report(self, report_id: str) -> ValidationReport | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM validation_reports WHERE validation_report_id = ?", (report_id,)).fetchone()
        return ValidationReport.model_validate(_payload(row)) if row else None

    def save_artifact(self, artifact: DocumentArtifact, content: bytes, suffix: str) -> DocumentArtifact:
        fingerprint = artifact.idempotency_fingerprint or content_hash({
            "work_order_id": artifact.work_order_id,
            "run_id": artifact.run_id,
            "stage": artifact.stage,
            "content_hash": artifact.content_hash,
            "parent_artifact_id": artifact.parent_artifact_id,
            "validation_report_id": artifact.validation_report_id,
            "approval_subject_hash": artifact.approval_subject_hash,
        })
        artifact = artifact.model_copy(update={"idempotency_fingerprint": fingerprint})
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._artifact_by_fingerprint(conn, fingerprint)
                if existing is not None:
                    conn.execute("COMMIT")
                    return existing
                target = self._storage_path("artifacts", artifact.work_order_id, f"{fingerprint}.{suffix}")
                self._atomic_write(target, content)
                artifact = artifact.model_copy(update={"storage_ref": target})
                self._put(conn, "document_artifacts", {
                    "artifact_id": artifact.artifact_id, "tenant_id": artifact.tenant_id,
                    "work_order_id": artifact.work_order_id, "stage": artifact.stage,
                    "content_hash": artifact.content_hash, "created_at": artifact.created_at.isoformat(),
                }, artifact)
                self._put_outbox_event(conn, self._artifact_outbox_event(artifact))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return artifact

    def get_artifact(self, artifact_id: str) -> DocumentArtifact | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload_json FROM document_artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        return DocumentArtifact.model_validate(_payload(row)) if row else None

    def read_artifact_content(self, artifact_id: str) -> bytes:
        artifact = self.get_artifact(artifact_id)
        if artifact is None or not artifact.storage_ref:
            raise KeyError(f"artifact not found: {artifact_id}")
        return Path(artifact.storage_ref).read_bytes()

    def list_artifacts(self, work_order_id: str) -> list[DocumentArtifact]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM document_artifacts WHERE work_order_id = ? ORDER BY rowid", (work_order_id,)
            ).fetchall()
        return [DocumentArtifact.model_validate(_payload(row)) for row in rows]

    def save_human_event(self, event: DocumentHumanEvent) -> DocumentHumanEvent:
        fingerprint = event.idempotency_fingerprint or content_hash({
            "work_order_id": event.work_order_id,
            "run_id": event.run_id,
            "artifact_id": event.artifact_id,
            "unit_id": event.unit_id,
            "event_type": event.event_type,
            "subject_artifact_content_hash": event.subject_artifact_content_hash,
            "approval_subject_hash": event.approval_subject_hash,
            "value": event.value,
            "actor_id": event.actor_id,
            "actor_role": event.actor_role,
            "comment": event.comment,
        })
        event = event.model_copy(update={"idempotency_fingerprint": fingerprint})
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT payload_json FROM document_human_events
                       WHERE json_extract(payload_json, '$.idempotency_fingerprint') = ?""",
                    (fingerprint,),
                ).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return DocumentHumanEvent.model_validate(_payload(row))
                self._put(conn, "document_human_events", {
                    "event_id": event.event_id, "work_order_id": event.work_order_id,
                    "artifact_id": event.artifact_id, "event_type": event.event_type,
                }, event)
                self._put_outbox_event(conn, self._human_event_outbox_event(event))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return event

    @staticmethod
    def _artifact_by_fingerprint(conn, fingerprint: str) -> DocumentArtifact | None:
        row = conn.execute(
            """SELECT payload_json FROM document_artifacts
               WHERE json_extract(payload_json, '$.idempotency_fingerprint') = ?""",
            (fingerprint,),
        ).fetchone()
        return DocumentArtifact.model_validate(_payload(row)) if row else None

    def list_pending_outbox_events(self, limit: int = 100) -> list[DocumentOutboxEvent]:
        if limit < 1:
            raise ValueError("outbox event limit must be positive")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT payload_json FROM document_outbox_events
                   WHERE status IN ('pending', 'failed') ORDER BY created_at, event_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [DocumentOutboxEvent.model_validate(_payload(row)) for row in rows]

    def mark_outbox_event_delivered(self, event_id: str) -> DocumentOutboxEvent:
        return self._update_outbox_event(event_id, status="delivered", last_error=None, delivered_at=datetime.now(timezone.utc))

    def mark_outbox_event_failed(self, event_id: str, error_message: str) -> DocumentOutboxEvent:
        return self._update_outbox_event(event_id, status="failed", last_error=error_message[:500])

    def _update_outbox_event(self, event_id: str, **updates: Any) -> DocumentOutboxEvent:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload_json FROM document_outbox_events WHERE event_id = ?", (event_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"outbox event not found: {event_id}")
                current = DocumentOutboxEvent.model_validate(_payload(row))
                revised = current.model_copy(update={
                    **updates,
                    "delivery_attempts": current.delivery_attempts + 1,
                })
                conn.execute(
                    "UPDATE document_outbox_events SET status = ?, payload_json = ? WHERE event_id = ?",
                    (revised.status, _json(revised), event_id),
                )
                conn.execute("COMMIT")
                return revised
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _put_outbox_event(conn, event: DocumentOutboxEvent) -> None:
        DocumentAuthoringStore._put(conn, "document_outbox_events", {
            "event_id": event.event_id,
            "event_key": event.event_key,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "event_type": event.event_type,
            "status": event.status,
            "created_at": event.created_at.isoformat(),
        }, event)

    @staticmethod
    def _work_order_outbox_event(order: DocumentWorkOrder, action: str) -> DocumentOutboxEvent:
        return DocumentOutboxEvent(
            event_id=f"outbox-{uuid.uuid4().hex}",
            event_key=f"work-order:{order.work_order_id}:{order.lock_version}",
            aggregate_type="work_order", aggregate_id=order.work_order_id,
            event_type=f"document_work_order.{action}",
            payload={
                "work_order_id": order.work_order_id,
                "status": order.status,
                "lock_version": order.lock_version,
                "input_fingerprint": order.input_fingerprint,
                "source_set_snapshot_id": order.source_set_snapshot_id,
            },
        )

    @staticmethod
    def _artifact_outbox_event(artifact: DocumentArtifact) -> DocumentOutboxEvent:
        return DocumentOutboxEvent(
            event_id=f"outbox-{uuid.uuid4().hex}",
            event_key=f"artifact:{artifact.idempotency_fingerprint}",
            aggregate_type="artifact", aggregate_id=artifact.artifact_id,
            event_type=f"document_artifact.{artifact.stage}_created",
            payload={
                "artifact_id": artifact.artifact_id,
                "work_order_id": artifact.work_order_id,
                "run_id": artifact.run_id,
                "stage": artifact.stage,
                "content_hash": artifact.content_hash,
                "validation_report_id": artifact.validation_report_id,
            },
        )

    @staticmethod
    def _human_event_outbox_event(event: DocumentHumanEvent) -> DocumentOutboxEvent:
        return DocumentOutboxEvent(
            event_id=f"outbox-{uuid.uuid4().hex}",
            event_key=f"human-event:{event.idempotency_fingerprint}",
            aggregate_type="human_event", aggregate_id=event.event_id,
            event_type=f"document_human_event.{event.event_type}_recorded",
            payload={
                "event_id": event.event_id,
                "artifact_id": event.artifact_id,
                "work_order_id": event.work_order_id,
                "event_type": event.event_type,
                "actor_id": event.actor_id,
                "approval_subject_hash": event.approval_subject_hash,
            },
        )

    def list_human_events(self, artifact_id: str) -> list[DocumentHumanEvent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM document_human_events WHERE artifact_id = ? ORDER BY rowid", (artifact_id,)
            ).fetchall()
        return [DocumentHumanEvent.model_validate(_payload(row)) for row in rows]

    def _storage_path(self, *parts: str) -> str:
        path = os.path.abspath(os.path.join(self.artifact_root, *parts))
        root = os.path.abspath(self.artifact_root)
        if os.path.commonpath([root, path]) != root:
            raise ValueError("document artifact path escapes configured storage root")
        return path

    @staticmethod
    def _atomic_write(path: str, content: bytes) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tmp_", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
