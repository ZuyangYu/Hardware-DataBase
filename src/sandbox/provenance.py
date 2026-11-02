"""Source identity, signature verification and SBOM evidence for Skill packages.

The control plane deliberately separates *evidence* from *trust*.  A source
archive always gets a deterministic file manifest and an HDB-generated SBOM,
but neither is presented as a third-party attestation.  A signature is only
``verified`` when it validates against a public key registered by an
administrator; a key or signature supplied by the uploader is never trusted
by itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class SkillProvenanceError(ValueError):
    """A source declaration or attestation failed validation."""


SOURCE_KINDS = frozenset({"direct_upload", "git", "registry", "model_generated", "internal"})
SIGNATURE_STATUSES = frozenset({"not_present", "verified"})
SBOM_STATUSES = frozenset({"generated_not_attested", "provided_validated_not_attested"})
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_SBOM_BYTES = 2 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_digest(value: Any) -> str:
    digest = str(value or "").strip().lower().removeprefix("sha256:")
    if not _DIGEST_RE.fullmatch(digest):
        raise SkillProvenanceError("package digest must be a SHA-256 digest")
    return digest


def canonical_manifest(source_manifest: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(source_manifest, (list, tuple)):
        raise SkillProvenanceError("source manifest must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in source_manifest:
        if not isinstance(raw, Mapping):
            raise SkillProvenanceError("source manifest entry must be an object")
        path = str(raw.get("path") or "")
        digest = str(raw.get("sha256") or "").lower().removeprefix("sha256:")
        try:
            size = int(raw.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise SkillProvenanceError("source manifest size is invalid") from exc
        if (
            not path or path in seen or _CONTROL_RE.search(path) or path.startswith("/")
            or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise SkillProvenanceError("source manifest path is invalid")
        if not _DIGEST_RE.fullmatch(digest) or size < 0:
            raise SkillProvenanceError("source manifest digest or size is invalid")
        seen.add(path)
        result.append({"path": path, "sha256": digest, "size": size})
    return tuple(sorted(result, key=lambda item: item["path"]))


def manifest_digest(source_manifest: Any) -> str:
    entries = canonical_manifest(source_manifest)
    return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def _bounded_text(value: Any, field_name: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit or _CONTROL_RE.search(text):
        raise SkillProvenanceError(f"{field_name} is invalid")
    return text


def validate_source_declaration(
    *, source_kind: str, source_ref: str = "", source_commit: str = ""
) -> dict[str, str]:
    kind = _bounded_text(source_kind or "direct_upload", "source_kind", 32).lower()
    if kind not in SOURCE_KINDS:
        raise SkillProvenanceError("source_kind is unsupported")
    ref = _bounded_text(source_ref, "source_ref", 512)
    commit = _bounded_text(source_commit, "source_commit", 128)
    if "\n" in ref or "\r" in ref or "\n" in commit or "\r" in commit:
        raise SkillProvenanceError("source provenance contains a line break")
    if any(char.isspace() for char in ref):
        raise SkillProvenanceError("source_ref cannot contain whitespace")
    if "://" in ref:
        parsed = urlsplit(ref)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SkillProvenanceError("source_ref cannot contain credentials, query or fragment data")
    if kind == "git" and (not ref or not _COMMIT_RE.fullmatch(commit)):
        raise SkillProvenanceError("git source requires a repository reference and commit SHA")
    if kind == "registry" and not ref:
        raise SkillProvenanceError("registry source requires a source reference")
    return {"source_kind": kind, "source_ref": ref, "source_commit": commit}


def signed_source_payload(*, package_digest: str, source_ref: str = "", source_commit: str = "") -> bytes:
    digest = canonical_digest(package_digest)
    declaration = validate_source_declaration(
        source_kind="direct_upload", source_ref=source_ref, source_commit=source_commit
    )
    return (
        "HDB-SKILL-SOURCE-V1\n"
        f"package_sha256:{digest}\n"
        f"source_ref:{declaration['source_ref']}\n"
        f"source_commit:{declaration['source_commit']}\n"
    ).encode("utf-8")


def _decode_blob(value: str, field_name: str, *, max_bytes: int) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise SkillProvenanceError(f"{field_name} is missing")
    try:
        if re.fullmatch(r"[0-9a-fA-F]+", text) and len(text) % 2 == 0:
            decoded = bytes.fromhex(text)
        else:
            decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, binascii.Error) as exc:
        raise SkillProvenanceError(f"{field_name} is not valid base64 or hex") from exc
    if not decoded or len(decoded) > max_bytes:
        raise SkillProvenanceError(f"{field_name} has an invalid size")
    return decoded


def public_key_from_text(public_key: str) -> tuple[bytes, str]:
    raw = _decode_blob(public_key, "public_key", max_bytes=128)
    if len(raw) != 32:
        raise SkillProvenanceError("Ed25519 public key must contain 32 bytes")
    try:
        key = Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise SkillProvenanceError("Ed25519 public key is invalid") from exc
    normalized = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return normalized, hashlib.sha256(normalized).hexdigest()


def verify_signature(
    *,
    package_digest: str,
    source_ref: str,
    source_commit: str,
    signing_key_id: str,
    signature: str,
    trusted_public_key: str | None,
) -> dict[str, str]:
    key_id = _bounded_text(signing_key_id, "signing_key_id", 128)
    encoded_signature = _bounded_text(signature, "signature", 512)
    if not key_id and not encoded_signature:
        return {
            "status": "not_present",
            "algorithm": "ed25519",
            "key_id": "",
            "signature_digest": "",
            "key_fingerprint": "",
        }
    if not key_id or not encoded_signature:
        raise SkillProvenanceError("signing_key_id and signature must be provided together")
    if not _KEY_ID_RE.fullmatch(key_id):
        raise SkillProvenanceError("signing_key_id is invalid")
    if not trusted_public_key:
        raise SkillProvenanceError("signing key is not registered as trusted")
    public_bytes, fingerprint = public_key_from_text(trusted_public_key)
    signature_bytes = _decode_blob(encoded_signature, "signature", max_bytes=128)
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature_bytes,
            signed_source_payload(
                package_digest=package_digest, source_ref=source_ref, source_commit=source_commit
            ),
        )
    except InvalidSignature as exc:
        raise SkillProvenanceError("source signature is invalid") from exc
    return {
        "status": "verified",
        "algorithm": "ed25519",
        "key_id": key_id,
        "signature_digest": hashlib.sha256(signature_bytes).hexdigest(),
        "key_fingerprint": fingerprint,
    }


def _normalize_sbom_entries(value: Any, expected: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    try:
        entries = canonical_manifest(value)
    except SkillProvenanceError:
        raise
    if entries != expected:
        raise SkillProvenanceError("provided SBOM does not match the uploaded source manifest")
    return entries


def _normalize_dependencies(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or len(value) > 128:
        raise SkillProvenanceError("SBOM dependencies are invalid")
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SkillProvenanceError("SBOM dependency entry must be an object")
        name = str(raw.get("name") or "").strip()
        version = str(raw.get("version") or "").strip()
        hashes = raw.get("hashes") or []
        if not name or len(name) > 200 or not version or len(version) > 100:
            raise SkillProvenanceError("SBOM dependency identity is invalid")
        if not isinstance(hashes, (list, tuple)) or not hashes:
            raise SkillProvenanceError("SBOM dependency hashes are required")
        normalized_hashes = [str(item).lower().removeprefix("sha256:") for item in hashes]
        if any(not _DIGEST_RE.fullmatch(item) for item in normalized_hashes):
            raise SkillProvenanceError("SBOM dependency hash is invalid")
        result.append({"name": name, "version": version, "hashes": normalized_hashes})
    return result


def build_sbom(
    *,
    package_digest: str,
    source_manifest: Any,
    dependencies: Any = (),
    provided: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str | int]]:
    digest = canonical_digest(package_digest)
    entries = canonical_manifest(source_manifest)
    if provided:
        if len(canonical_json(provided).encode("utf-8")) > _MAX_SBOM_BYTES:
            raise SkillProvenanceError("provided SBOM is too large")
        if str(provided.get("format") or "") != "hdb-sbom-v1":
            raise SkillProvenanceError("provided SBOM format is unsupported")
        if canonical_digest(provided.get("package_digest")) != digest:
            raise SkillProvenanceError("provided SBOM package digest does not match the source")
        provided_entries = _normalize_sbom_entries(provided.get("source_manifest"), entries)
        dependencies_list = _normalize_dependencies(provided.get("dependencies") or [])
        expected_dependencies = _normalize_dependencies(dependencies or [])
        if dependencies_list != expected_dependencies:
            raise SkillProvenanceError("provided SBOM dependencies do not match the locked source dependencies")
        sbom = {
            "format": "hdb-sbom-v1",
            "package_digest": digest,
            "source_manifest": [dict(item) for item in provided_entries],
            "dependencies": dependencies_list,
            "attestation_status": "provided_validated_not_attested",
        }
        status = "provided_validated_not_attested"
    else:
        dependencies_list = _normalize_dependencies(dependencies or ())
        sbom = {
            "format": "hdb-sbom-v1",
            "package_digest": digest,
            "source_manifest": [dict(item) for item in entries],
            "dependencies": dependencies_list,
            # This is an HDB-derived inventory, not a vendor attestation.
            "attestation_status": "generated_not_attested",
        }
        status = "generated_not_attested"
    sbom_digest = hashlib.sha256(canonical_json(sbom).encode("utf-8")).hexdigest()
    summary: dict[str, str | int] = {
        "format": "hdb-sbom-v1",
        "status": status,
        "digest": sbom_digest,
        "component_count": len(entries),
        "dependency_count": len(sbom["dependencies"]),
    }
    return sbom, summary


@dataclass(frozen=True)
class SkillProvenance:
    skill_ref: str
    package_digest: str
    version: str
    record: Mapping[str, Any]
    sbom: Mapping[str, Any]
    created_at: str
    updated_at: str

    def to_dict(self, *, include_sbom: bool = False) -> dict[str, Any]:
        result = dict(self.record)
        result["skill_ref"] = self.skill_ref
        result["package_digest"] = self.package_digest
        result["version"] = self.version
        result["created_at"] = self.created_at
        result["updated_at"] = self.updated_at
        if include_sbom:
            result["sbom"] = dict(self.sbom)
        return result


class SkillProvenanceStore:
    """SQLite store for source attestations, trusted keys and review history."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sandbox_skill_signing_keys (
                    key_id TEXT PRIMARY KEY,
                    algorithm TEXT NOT NULL DEFAULT 'ed25519',
                    public_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    revoked_by TEXT NOT NULL DEFAULT '',
                    revoked_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sandbox_skill_provenance (
                    skill_ref TEXT NOT NULL,
                    package_digest TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    record_json TEXT NOT NULL DEFAULT '{}',
                    sbom_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(skill_ref, package_digest)
                );
                CREATE TABLE IF NOT EXISTS sandbox_skill_review_events (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_ref TEXT NOT NULL,
                    package_digest TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL DEFAULT '',
                    actor_name TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_skill_provenance_ref
                    ON sandbox_skill_provenance(skill_ref, updated_at);
                CREATE INDEX IF NOT EXISTS idx_skill_review_ref
                    ON sandbox_skill_review_events(skill_ref, package_digest, created_at);
                """
            )

    def register_key(self, *, key_id: str, public_key: str, actor: str = "") -> dict[str, Any]:
        if not _KEY_ID_RE.fullmatch(str(key_id or "")):
            raise SkillProvenanceError("signing_key_id is invalid")
        raw, fingerprint = public_key_from_text(public_key)
        existing = self.get_key(str(key_id))
        if existing and existing.get("state") == "active" and existing.get("fingerprint") != fingerprint:
            raise SkillProvenanceError("active signing key cannot be replaced; revoke it first")
        normalized = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO sandbox_skill_signing_keys
                   (key_id, algorithm, public_key, fingerprint, state, created_by, created_at)
                   VALUES (?, 'ed25519', ?, ?, 'active', ?, ?)
                   ON CONFLICT(key_id) DO UPDATE SET
                     algorithm = excluded.algorithm, public_key = excluded.public_key,
                     fingerprint = excluded.fingerprint, state = 'active',
                     created_by = excluded.created_by, created_at = excluded.created_at,
                     revoked_by = '', revoked_at = ''""",
                (str(key_id), normalized, fingerprint, str(actor or "")[:128], now),
            )
        return self.get_key(str(key_id)) or {}

    def get_key(self, key_id: str, *, active_only: bool = False) -> dict[str, Any] | None:
        query = "SELECT * FROM sandbox_skill_signing_keys WHERE key_id = ?"
        params: tuple[Any, ...] = (str(key_id),)
        if active_only:
            query += " AND state = 'active'"
        with closing(self._connect()) as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        return {
            "key_id": str(row["key_id"]),
            "algorithm": str(row["algorithm"]),
            "public_key": str(row["public_key"]),
            "fingerprint": str(row["fingerprint"]),
            "state": str(row["state"]),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "revoked_by": str(row["revoked_by"]),
            "revoked_at": str(row["revoked_at"]),
        }

    def list_keys(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        clause = "" if include_revoked else " WHERE state = 'active'"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM sandbox_skill_signing_keys{clause} ORDER BY created_at DESC"
            ).fetchall()
        return [self.get_key(str(row["key_id"])) or {} for row in rows]

    def revoke_key(self, key_id: str, *, actor: str = "") -> dict[str, Any]:
        now = _now()
        with closing(self._connect()) as conn:
            result = conn.execute(
                "UPDATE sandbox_skill_signing_keys SET state = 'revoked', revoked_by = ?, revoked_at = ? "
                "WHERE key_id = ? AND state = 'active'",
                (str(actor or "")[:128], now, str(key_id)),
            )
        if result.rowcount != 1:
            raise SkillProvenanceError("signing key is not active")
        return self.get_key(str(key_id)) or {}

    def put(
        self,
        *,
        skill_ref: str,
        package_digest: str,
        version: str,
        record: Mapping[str, Any],
        sbom: Mapping[str, Any],
    ) -> SkillProvenance:
        digest = canonical_digest(package_digest)
        now = _now()
        record_json = canonical_json(dict(record))
        sbom_json = canonical_json(dict(sbom))
        if len(sbom_json.encode("utf-8")) > _MAX_SBOM_BYTES:
            raise SkillProvenanceError("SBOM is too large to persist")
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO sandbox_skill_provenance
                   (skill_ref, package_digest, version, record_json, sbom_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(skill_ref, package_digest) DO UPDATE SET
                     version = excluded.version, record_json = excluded.record_json,
                     sbom_json = excluded.sbom_json, updated_at = excluded.updated_at""",
                (str(skill_ref), digest, str(version or "")[:64], record_json, sbom_json, now, now),
            )
        return self.get(skill_ref, digest)  # type: ignore[return-value]

    def get(self, skill_ref: str, package_digest: str) -> SkillProvenance | None:
        digest = canonical_digest(package_digest)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sandbox_skill_provenance WHERE skill_ref = ? AND package_digest = ?",
                (str(skill_ref), digest),
            ).fetchone()
        if row is None:
            return None
        try:
            record = dict(json.loads(str(row["record_json"] or "{}")) or {})
            sbom = dict(json.loads(str(row["sbom_json"] or "{}")) or {})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SkillProvenanceError("stored Skill provenance is invalid") from exc
        return SkillProvenance(
            skill_ref=str(row["skill_ref"]), package_digest=str(row["package_digest"]),
            version=str(row["version"]), record=record, sbom=sbom,
            created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        )

    def record_review(
        self,
        *,
        skill_ref: str,
        package_digest: str,
        action: str,
        decision: str,
        actor_id: str | int = "",
        actor_name: str = "",
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        digest = canonical_digest(package_digest)
        action_value = _bounded_text(action, "review action", 80)
        decision_value = _bounded_text(decision, "review decision", 40)
        reason_value = _bounded_text(reason, "review reason", 500)
        metadata_json = canonical_json(dict(metadata or {}))
        if len(metadata_json.encode("utf-8")) > 16 * 1024:
            raise SkillProvenanceError("review metadata is too large")
        created_at = _now()
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """INSERT INTO sandbox_skill_review_events
                   (skill_ref, package_digest, action, decision, actor_id, actor_name,
                    reason, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(skill_ref), digest, action_value, decision_value, str(actor_id or "")[:128],
                 str(actor_name or "")[:128], reason_value, metadata_json, created_at),
            )
            review_id = int(cursor.lastrowid)
        return {
            "review_id": review_id, "skill_ref": str(skill_ref), "package_digest": digest,
            "action": action_value, "decision": decision_value,
            "actor_id": str(actor_id or ""), "actor_name": str(actor_name or ""),
            "reason": reason_value, "metadata": dict(metadata or {}), "created_at": created_at,
        }

    def reviews(self, skill_ref: str, package_digest: str = "") -> list[dict[str, Any]]:
        params: list[Any] = [str(skill_ref)]
        query = "SELECT * FROM sandbox_skill_review_events WHERE skill_ref = ?"
        if package_digest:
            query += " AND package_digest = ?"
            params.append(canonical_digest(package_digest))
        query += " ORDER BY review_id ASC"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = dict(json.loads(str(row["metadata_json"] or "{}")) or {})
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            result.append({
                "review_id": int(row["review_id"]), "skill_ref": str(row["skill_ref"]),
                "package_digest": str(row["package_digest"]), "action": str(row["action"]),
                "decision": str(row["decision"]), "actor_id": str(row["actor_id"]),
                "actor_name": str(row["actor_name"]), "reason": str(row["reason"]),
                "metadata": metadata, "created_at": str(row["created_at"]),
            })
        return result


__all__ = [
    "SBOM_STATUSES",
    "SIGNATURE_STATUSES",
    "SOURCE_KINDS",
    "SkillProvenance",
    "SkillProvenanceError",
    "SkillProvenanceStore",
    "build_sbom",
    "canonical_json",
    "manifest_digest",
    "signed_source_payload",
    "validate_source_declaration",
    "verify_signature",
]
