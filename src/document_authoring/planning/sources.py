"""Safe adapters from legacy source snapshots to a planner-owned view."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Literal, Mapping

from pydantic import Field, model_validator

from src.document_authoring.models import KnowledgeBaseSourceSnapshot
from src.projects.models import SourceSetSnapshot

from .models import NonEmptyId, PlanningModel, planning_content_hash


SourceType = Literal[
    "baseline",
    "project_source",
    "shared_reference",
    "processing_artifact",
    "knowledge_base_source",
    "attachment",
    "user_assertion",
]


class FrozenSourceRef(PlanningModel):
    """A source identity safe to put in a plan or an audit event."""

    source_id: NonEmptyId
    source_type: SourceType
    scope: NonEmptyId
    content_hash: NonEmptyId
    ordinal: int = Field(ge=0, le=10_000_000)


class FrozenSourceScope(PlanningModel):
    """Immutable source identity/scope view with no raw source payload."""

    snapshot_id: NonEmptyId
    snapshot_hash: NonEmptyId
    refs: list[FrozenSourceRef] = Field(default_factory=list, max_length=100_000)
    version_policy: Literal["current_published", "latest", "explicit", "frozen"] = "frozen"
    hash_origin: Literal["derived", "upstream"] = "derived"

    @model_validator(mode="after")
    def validate_scope(self) -> "FrozenSourceScope":
        identities = [(item.source_type, item.source_id) for item in self.refs]
        if len(identities) != len(set(identities)):
            raise ValueError("frozen source references must be unique")
        ordinals = [item.ordinal for item in self.refs]
        if ordinals != list(range(len(ordinals))):
            raise ValueError("frozen source reference ordinals must be contiguous")
        if self.hash_origin == "derived":
            expected = self._derived_hash(self.refs, self.version_policy)
            if self.snapshot_hash != expected:
                raise ValueError("snapshot_hash does not match frozen source references")
        return self

    @staticmethod
    def _derived_hash(refs: list[FrozenSourceRef], version_policy: str) -> str:
        return planning_content_hash({
            "refs": [item.model_dump(mode="json") for item in refs],
            "version_policy": version_policy,
        })

    @classmethod
    def from_project_snapshot(cls, snapshot: SourceSetSnapshot) -> "FrozenSourceScope":
        scope = f"project:{snapshot.project_id}"
        values: list[tuple[str, str, str]] = [
            ("baseline", snapshot.baseline_id, snapshot.baseline_content_hash),
        ]
        values.extend(("project_source", source_id, snapshot.content_hash) for source_id in snapshot.source_version_ids)
        values.extend(("shared_reference", source_id, snapshot.content_hash) for source_id in snapshot.shared_reference_version_ids)
        values.extend(("processing_artifact", artifact_id, snapshot.content_hash) for artifact_id in snapshot.processing_artifact_ids)
        refs = [
            FrozenSourceRef(
                source_id=source_id,
                source_type=source_type,
                scope=scope,
                content_hash=content_hash,
                ordinal=index,
            )
            for index, (source_type, source_id, content_hash) in enumerate(values)
        ]
        return cls(
            snapshot_id=snapshot.source_set_snapshot_id,
            snapshot_hash=snapshot.content_hash,
            refs=refs,
            version_policy="frozen",
            hash_origin="upstream",
        )

    @classmethod
    def from_knowledge_base_snapshot(cls, snapshot: KnowledgeBaseSourceSnapshot) -> "FrozenSourceScope":
        scope = f"knowledge_base:{snapshot.knowledge_base_name}"
        refs = [
            FrozenSourceRef(
                source_id=source_name,
                source_type="knowledge_base_source",
                scope=scope,
                content_hash=snapshot.content_hash,
                ordinal=index,
            )
            for index, source_name in enumerate(snapshot.source_names)
        ]
        return cls(
            snapshot_id=snapshot.source_set_snapshot_id,
            snapshot_hash=snapshot.content_hash,
            refs=refs,
            version_policy="frozen",
            hash_origin="upstream",
        )

    @classmethod
    def from_attachment_refs(
        cls,
        refs: list[Any],
        *,
        version_policy: Literal["current_published", "latest", "explicit", "frozen"] = "frozen",
    ) -> "FrozenSourceScope":
        return cls.from_inputs(attachments=refs, version_policy=version_policy)

    @classmethod
    def from_user_assertion_hashes(
        cls,
        assertions: list[Any],
        *,
        scope: str = "user_assertion",
        version_policy: Literal["current_published", "latest", "explicit", "frozen"] = "frozen",
    ) -> "FrozenSourceScope":
        refs: list[FrozenSourceRef] = []
        for assertion in assertions:
            if isinstance(assertion, Mapping):
                _reject_raw_mapping(assertion, path="user_assertion")
                assertion_hash = assertion.get("content_hash") or assertion.get("sha256")
                assertion_id = assertion.get("assertion_id") or assertion_hash
            else:
                assertion_hash = str(assertion)
                assertion_id = assertion_hash
            if not str(assertion_hash or "").strip():
                raise ValueError("user assertions require a content hash")
            refs.append(FrozenSourceRef(
                source_id=str(assertion_id),
                source_type="user_assertion",
                scope=scope,
                content_hash=str(assertion_hash),
                ordinal=len(refs),
            ))
        return cls._from_refs(refs, version_policy=version_policy)

    @classmethod
    def from_inputs(
        cls,
        *,
        project_snapshot: SourceSetSnapshot | None = None,
        knowledge_base_snapshot: KnowledgeBaseSourceSnapshot | None = None,
        attachments: list[Any] | None = None,
        user_assertion_hashes: list[Any] | None = None,
        version_policy: Literal["current_published", "latest", "explicit", "frozen"] = "frozen",
    ) -> "FrozenSourceScope":
        refs: list[FrozenSourceRef] = []
        if project_snapshot is not None:
            project_scope = cls.from_project_snapshot(project_snapshot)
            refs.extend(project_scope.refs)
        if knowledge_base_snapshot is not None:
            kb_scope = cls.from_knowledge_base_snapshot(knowledge_base_snapshot)
            refs.extend(kb_scope.refs)
        for attachment in attachments or []:
            refs.append(cls._attachment_ref(attachment, ordinal=len(refs)))
        for assertion in user_assertion_hashes or []:
            if isinstance(assertion, Mapping):
                _reject_raw_mapping(assertion, path="user_assertion")
                assertion_hash = assertion.get("content_hash") or assertion.get("sha256")
                assertion_id = assertion.get("assertion_id") or assertion_hash
            else:
                assertion_hash = str(assertion)
                assertion_id = assertion_hash
            if not str(assertion_hash or "").strip():
                raise ValueError("user assertions require a content hash")
            refs.append(FrozenSourceRef(
                source_id=str(assertion_id),
                source_type="user_assertion",
                scope="user_assertion",
                content_hash=str(assertion_hash),
                ordinal=len(refs),
            ))
        return cls._from_refs(refs, version_policy=version_policy)

    @classmethod
    def _from_refs(
        cls,
        refs: list[FrozenSourceRef],
        *,
        version_policy: Literal["current_published", "latest", "explicit", "frozen"],
    ) -> "FrozenSourceScope":
        snapshot_hash = cls._derived_hash(refs, version_policy)
        return cls(
            snapshot_id=f"scope-{snapshot_hash.removeprefix('sha256:')[:24]}",
            snapshot_hash=snapshot_hash,
            refs=refs,
            version_policy=version_policy,
            hash_origin="derived",
        )

    @staticmethod
    def _attachment_ref(value: Any, *, ordinal: int) -> FrozenSourceRef:
        raw = asdict(value) if is_dataclass(value) else value
        if not isinstance(raw, Mapping):
            raise ValueError("attachment reference must be a mapping or AttachmentRef")
        _reject_raw_mapping(raw, path="attachment")
        attachment_id = raw.get("attachment_id") or raw.get("asset_id")
        content_hash = raw.get("sha256") or raw.get("content_hash")
        if not str(attachment_id or "").strip() or not str(content_hash or "").strip():
            raise ValueError("attachment reference requires attachment_id and sha256")
        return FrozenSourceRef(
            source_id=str(attachment_id),
            source_type="attachment",
            scope=f"attachment:{attachment_id}",
            content_hash=str(content_hash),
            ordinal=ordinal,
        )


def _reject_raw_mapping(value: Mapping[str, Any], *, path: str) -> None:
    forbidden = {
        "api_key", "content", "credential", "credentials", "evidence", "file",
        "password", "path", "prompt", "raw_content", "secret", "storage_ref",
        "template_bytes", "token",
    }
    for key, child in value.items():
        key_text = str(key)
        if key_text.casefold() in forbidden:
            raise ValueError(f"{path} contains forbidden key: {key_text}")
        if isinstance(child, Mapping):
            _reject_raw_mapping(child, path=f"{path}.{key_text}")
        elif isinstance(child, (list, tuple)):
            for index, item in enumerate(child):
                if isinstance(item, Mapping):
                    _reject_raw_mapping(item, path=f"{path}.{key_text}[{index}]")


__all__ = ["FrozenSourceRef", "FrozenSourceScope", "SourceType"]
