"""Server-resolved file targets and per-processor retrieval work items.

The model may mention a project, file, sheet or pin, but only this module turns
those hints into record identities and frozen source contracts. Nothing here
trusts model-supplied IDs.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from src.pipelines.document_rag.schemas import (
    DocumentRetrievalContract,
    DocumentSourceStamp,
    normalize_parse_status,
    TASK_STATUS_COMPLETED,
)


_FILE_SUFFIXES = (".docx", ".doc", ".xlsx", ".xlsm", ".xls", ".pdf", ".pptx", ".txt", ".md")
_GENERIC_COMPONENTS = frozenset({
    "xlsx", "xlsm", "xls", "docx", "doc", "pdf", "pptx", "txt", "md", "eps",
})
_PROJECT_RE = re.compile(r"\b(?:AP|AA)\d{6}\b", re.IGNORECASE)
_GENERIC_STANDALONE = frozenset({"icd", "adas", "hsi"})
_CELL_RE = re.compile(r"(?:(?<=!)([A-Z]{1,3}\d{1,7}(?::[A-Z]{1,3}\d{1,7})?)|([A-Z]{1,3}\d{1,7}:[A-Z]{1,3}\d{1,7})(?!!?))")
_PIN_RE = re.compile(r"(?:第\s*(\d{1,3})\s*(?:脚|引脚)|(?:pin|管脚)\s*[:：#]?\s*(\d{1,3}))", re.IGNORECASE)
_ROW_RE = re.compile(r"第\s*(\d{1,5})\s*行")
_SHEET_RE = re.compile(r"(?:工作表|sheet)\s*[:：]?\s*[「“\"']?([^，。；;、\s」”\"']+)", re.IGNORECASE)
_COMMON_SHEETS = ("管脚分配", "Pin Definition")
_PIN_FIELDS = {"管脚分配": "管脚", "Pin Definition": "Pin Number"}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def _stem(name: str) -> str:
    lowered = _norm(name)
    for suffix in _FILE_SUFFIXES:
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def _components(name: str) -> list[str]:
    stem = _stem(name)
    return [part for part in re.split(r"[\s_\-.]+", stem) if part]


def _alias_phrases(name: str) -> list[tuple[str, int]]:
    """Return (normalized phrase, specificity) from a document name.

    Specificity is 1 for a full name, 2 for the stem, 3 for two adjacent
    meaningful components ("ADAS HSI") and 4 for a single component.
    """
    phrases: list[tuple[str, int]] = [(_norm(name), 1), (_stem(name), 2)]
    components = _components(name)
    meaningful = [part for part in components if not part.isdigit()]
    for index in range(len(meaningful) - 1):
        first, second = meaningful[index], meaningful[index + 1]
        if len(first) >= 3 and len(second) >= 3:
            phrases.append((f"{first} {second}", 3))
            phrases.append((f"{first}_{second}", 3))
    for part in meaningful:
        if len(part) >= 3 and part not in _GENERIC_COMPONENTS:
            phrases.append((part, 4))
    unique: dict[str, int] = {}
    for phrase, specificity in phrases:
        if phrase and phrase not in unique:
            unique[phrase] = specificity
    return list(unique.items())


@dataclass(frozen=True)
class TargetLocator:
    target_id: str
    sheet_name: str | None = None
    row_index: int | None = None
    cell_ref: str | None = None
    pin_value: str | None = None
    pin_field: str | None = None
    match_basis: str = "question"
    source: str = "explicit"


@dataclass(frozen=True)
class RetrievalTarget:
    target_id: str
    kb_id: int
    kb_name: str
    record_id: int
    processor_kind: str
    document_name: str
    stamps: DocumentSourceStamp
    locators: tuple[TargetLocator, ...]
    match_basis: str
    source: str
    query_id: str
    question: str


@dataclass
class TurnRetrievalTargets:
    turn_id: str
    scope_fingerprint: str
    targets: tuple[RetrievalTarget, ...] = ()
    resolution: str = "unrestricted_within_kb"
    diagnostics: list[dict] = field(default_factory=list)
    target_fingerprint: str = ""

    @property
    def targeted_kb_ids(self) -> tuple[int, ...]:
        return tuple(sorted({target.kb_id for target in self.targets}))

    def required_for(self, target_id: str) -> tuple[RetrievalTarget, ...]:
        return tuple(target for target in self.targets if target.target_id == target_id)


@dataclass(frozen=True)
class RetrievalWorkItem:
    work_id: str
    target_ids: tuple[str, ...]
    query_id: str
    kb_id: int
    retriever_kind: str
    normalized_query: str
    contract: DocumentRetrievalContract
    locators_by_record: Mapping[int, tuple[TargetLocator, ...]] = field(default_factory=dict)


def _record_matches_basis(record, basis: str) -> bool:
    for phrase, _specificity in _alias_phrases(getattr(record, "document_name", "")):
        if phrase == basis or phrase.replace("_", " ") == basis:
            return True
    return False


def _record_stamp(record) -> DocumentSourceStamp:
    return DocumentSourceStamp(
        kb_id=int(getattr(record, "kb_id", 0) or 0),
        local_record_id=int(record.id),
        source_content_hash=str(getattr(record, "content_hash", "") or ""),
        remote_document_id=str(getattr(record, "document_id", "") or ""),
        index_revision=int(getattr(record, "index_revision", 0) or 0),
        source_version_id=getattr(record, "source_version_id", None),
        revision=getattr(record, "revision", None),
        processing_artifact_id=getattr(record, "processing_artifact_id", None),
        processor_version=getattr(record, "processor_version", None),
    )


def _clause_bounds(question: str, start: int) -> tuple[int, int]:
    left = max(question.rfind(mark, 0, start) for mark in "，,。；;\n、") + 1
    right_candidates = [question.find(mark, start) for mark in "，,。；;\n、"]
    right_candidates = [value for value in right_candidates if value >= 0]
    right = min(right_candidates) if right_candidates else len(question)
    return left, right


def _project_sources(clause: str, sources: Sequence) -> set[int]:
    normalized = _norm(clause)
    found = set()
    for source in sources:
        kb_name = _norm(source.kb_name)
        if kb_name and re.search(rf"(?<![A-Za-z0-9]){re.escape(kb_name)}(?![A-Za-z0-9])", normalized):
            found.add(source.kb_id)
    for match in _PROJECT_RE.findall(unicodedata.normalize("NFKC", clause)):
        for source in sources:
            if _norm(source.kb_name) == _norm(match):
                found.add(source.kb_id)
    return found


def _locators(question: str, target_id: str, *, sheet_hint: str | None = None) -> tuple[TargetLocator, ...]:
    sheet = sheet_hint
    if not sheet:
        sheet_match = _SHEET_RE.search(question)
        if sheet_match:
            value = sheet_match.group(1).strip()
            if value:
                sheet = value
    if not sheet:
        for candidate in _COMMON_SHEETS:
            if _norm(candidate) in _norm(question):
                sheet = candidate
                break
    pin_field = _PIN_FIELDS.get(sheet or "")
    cell_refs = []
    for match in _CELL_RE.finditer(question):
        value = (match.group(1) or match.group(2) or "").upper()
        if value and not _PROJECT_RE.fullmatch(value):
            cell_refs.append(value)
    row_indices = [int(value) for value in _ROW_RE.findall(question)]
    pins = [first or second for first, second in _PIN_RE.findall(question)]
    locators: list[TargetLocator] = []
    if pins:
        for pin in pins:
            locators.append(TargetLocator(target_id=target_id, sheet_name=sheet, pin_value=pin,
                                          pin_field=pin_field, match_basis="pin"))
    elif cell_refs:
        for cell in cell_refs:
            locators.append(TargetLocator(target_id=target_id, sheet_name=sheet, cell_ref=cell,
                                          match_basis="cell"))
    elif row_indices:
        for row in row_indices:
            locators.append(TargetLocator(target_id=target_id, sheet_name=sheet, row_index=row,
                                          match_basis="row"))
    if not locators:
        locators.append(TargetLocator(target_id=target_id, sheet_name=sheet, match_basis="file"))
    return tuple(locators)


def _sentence_around(question: str, start: int) -> str:
    left = max(question.rfind(mark, 0, start) for mark in "。；;\n") + 1
    rights = [question.find(mark, start) for mark in "。；;\n"]
    rights = [value for value in rights if value >= 0]
    right = min(rights) + 1 if rights else len(question)
    return question[left:right]


def _navigation_only(clause: str, basis: str) -> bool:
    cleaned = _clean_question(clause, basis)
    if len(cleaned) > 40:
        return False
    if not re.search(r"请|依据|基于|知识库|文件|文档", clause):
        return False
    return not re.search(r"电压|电流|范围|管脚|引脚|电阻|电容|网络|定义|功能|要求|条件|参数", cleaned)


def _clean_question(clause: str, basis: str) -> str:
    """Keep navigation names out of the keyword query."""
    value = unicodedata.normalize("NFKC", clause)
    if basis:
        value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(basis)}(?![A-Za-z0-9])", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[^\s，,。；;、]+?\.(?:docx|doc|xlsx|xlsm|xls|pdf|pptx|txt|md)\b", " ", value, flags=re.IGNORECASE)
    value = _PROJECT_RE.sub(" ", value)
    value = re.sub(r"^请(?:联合|分别|同时)?(?:检索|查找|读取|列出|查询)[^:：，。；]*[:：]?\s*", " ", value)
    value = re.sub(r"在\s*中规定的", " ", value)
    value = re.sub(r"在\s*中\s*", " ", value)
    value = value.replace("规定的", " ")
    value = re.sub(r"^\s*[、，,。；;:：]+\s*", "", value)
    return re.sub(r"\s+", " ", value).strip(" ，,。；;、") or clause.strip()


def _target_fingerprint(targets: Sequence[RetrievalTarget]) -> str:
    payload = [
        {
            "target_id": target.target_id,
            "kb_id": target.kb_id,
            "record_id": target.record_id,
            "stamps": target.stamps.__dict__,
            "locators": [locator.__dict__ for locator in target.locators],
        }
        for target in sorted(targets, key=lambda item: item.target_id)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_EXPLICIT_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_FILE_MENTION_RE = re.compile(
    r"\.(?:docx?|xlsx?|xlsm|pdf|pptx?|txt|md)\b", re.IGNORECASE,
)


def _likely_explicit_target(question: str) -> bool:
    """Cheap prefilter before listing every document in every selected KB.

    Only questions that look like they name a project/file trigger the full
    catalog scan.  Generic questions fall back to ordinary retrieval, which
    can still discover files by content.
    """

    text = str(question or "")
    if _PROJECT_RE.search(text) or _FILE_MENTION_RE.search(text):
        return True
    for token in _EXPLICIT_TOKEN_RE.findall(text):
        if len(token) >= 4 and (
            any(ch.isdigit() for ch in token)
            or "_" in token
            or token.isupper()
        ):
            return True
    return False


def resolve_turn_targets(
    question: str,
    *,
    sources: Sequence,
    document_store,
    turn_id: str = "",
    scope_fingerprint: str = "",
    history_hint: Sequence[dict] | None = None,
) -> TurnRetrievalTargets:
    """Resolve explicit project/file mentions to authorized current records."""
    if not history_hint and not _likely_explicit_target(question):
        return TurnRetrievalTargets(
            turn_id=turn_id,
            scope_fingerprint=scope_fingerprint,
            resolution="unrestricted_within_kb",
            target_fingerprint=_target_fingerprint(()),
        )
    records_by_kb: dict[int, list] = {}
    for source in sources:
        try:
            records = document_store.list_documents(source.kb_name, source.department_id)
        except Exception:
            records = []
        records_by_kb[source.kb_id] = [
            record for record in records
            if getattr(record, "kb_id", 0) in (0, None, source.kb_id)
            and normalize_parse_status(getattr(record, "status", ""), getattr(record, "processor_kind", "")) == TASK_STATUS_COMPLETED
        ]

    mentions: list[dict] = []
    for source in sources:
        for record in records_by_kb.get(source.kb_id, []):
            for phrase, specificity in _alias_phrases(getattr(record, "document_name", "")):
                for phrase_variant in {phrase, phrase.replace("_", " ")}:
                    if not phrase_variant:
                        continue
                    pattern = rf"(?<![A-Za-z0-9]){re.escape(phrase_variant)}(?![A-Za-z0-9])"
                    for match in re.finditer(pattern, question, re.IGNORECASE):
                        mentions.append({
                            "start": match.start(), "end": match.end(), "record": record,
                            "source": source, "specificity": specificity, "basis": phrase_variant,
                        })
    # Keep the most specific non-overlapping span; all records matching that
    # exact span stay as disambiguation candidates. Dropping same-span records
    # here would silently choose one KB before project binding runs.
    span_specificity: dict[tuple[int, int], int] = {}
    for mention in mentions:
        span = (mention["start"], mention["end"])
        span_specificity[span] = min(span_specificity.get(span, 99), mention["specificity"])
    selected_spans: list[tuple[int, int]] = []
    for span in sorted(span_specificity, key=lambda item: (span_specificity[item], -(item[1] - item[0]), item[0])):
        if any(not (span[1] <= other[0] or span[0] >= other[1]) for other in selected_spans):
            continue
        selected_spans.append(span)
    selected = [mention for mention in mentions
                if (mention["start"], mention["end"]) in selected_spans
                and mention["specificity"] == span_specificity[(mention["start"], mention["end"])]]

    diagnostics: list[dict] = []
    groups: dict[int, dict] = {}
    ambiguous = False
    not_found = False
    for mention in selected:
        left, right = _clause_bounds(question, mention["start"])
        clause = question[left:right]
        if _navigation_only(clause, mention["basis"]):
            clause = _sentence_around(question, mention["start"])
        projects = _project_sources(clause, sources)
        if mention["basis"] in _GENERIC_STANDALONE and not projects:
            # "ICD 表格" / "ADAS 的范围" are generic nouns, not a named file.
            # Only treat them as a target when the same clause names a project.
            continue
        candidates = [
            (source, record) for source in sources
            for record in records_by_kb.get(source.kb_id, [])
            if _record_matches_basis(record, mention["basis"])
        ]
        exact = [
            (source, record) for source, record in candidates
            if mention["basis"] in {
                _norm(getattr(record, "document_name", "")),
                _norm(getattr(record, "original_file_name", "")),
                _stem(getattr(record, "document_name", "")),
                _stem(getattr(record, "original_file_name", "")),
            }
        ]
        if exact:
            candidates = exact
        else:
            originals = [(source, record) for source, record in candidates
                         if int(getattr(record, "embed_depth", 0) or 0) == 0]
            if originals:
                candidates = originals
        if projects:
            filtered = [(source, record) for source, record in candidates if source.kb_id in projects]
            # In a shared list ("... AA250004 ... HWREQM ...") the nearest
            # previous project token is not the owner of every later item.
            # Only constrain when the project actually contains the candidate.
            if filtered:
                candidates = filtered
            elif len(projects) > 1 and len(candidates) > 1:
                ambiguous = True
                diagnostics.append({"start": mention["start"], "basis": mention["basis"],
                                    "resolution": "target_ambiguous", "reason": "project_binding_ambiguous"})
                continue
        unique = {(source.kb_id, record.id): (source, record) for source, record in candidates}
        if not unique:
            not_found = True
            diagnostics.append({"start": mention["start"], "basis": mention["basis"],
                                "resolution": "target_not_found", "reason": "record_not_available"})
            continue
        if len(unique) > 1:
            ambiguous = True
            diagnostics.append({"start": mention["start"], "basis": mention["basis"],
                                "resolution": "target_ambiguous", "reason": "document_name_ambiguous",
                                "candidates": sorted(f"kb:{kb_id}/record:{record_id}" for kb_id, record_id in unique)})
            continue
        source, record = next(iter(unique.values()))
        group = groups.setdefault(record.id, {
            "source": source, "record": record, "start": mention["start"],
            "basis": mention["basis"], "clause": clause,
        })
        group["start"] = min(group["start"], mention["start"])
        group["end"] = max(group.get("end", mention["end"]), mention["end"])

    # A filename with an extension that did not resolve is an explicit miss,
    # never permission to search every file.
    if not selected:
        for match in re.finditer(r"[^\s，,。；;、]+?\.(?:docx|doc|xlsx|xlsm|xls|pdf|pptx|txt|md)\b", question, re.IGNORECASE):
            value = match.group(0)
            candidates = [
                (source, record) for source in sources
                for record in records_by_kb.get(source.kb_id, [])
                if _norm(value) in {_norm(getattr(record, "document_name", "")), _norm(getattr(record, "original_file_name", ""))}
            ]
            if not candidates:
                not_found = True
                diagnostics.append({"start": match.start(), "basis": value,
                                    "resolution": "target_not_found", "reason": "explicit_file_not_found"})

    targets: list[RetrievalTarget] = []
    for index, group in enumerate(sorted(groups.values(), key=lambda item: item["start"]), 1):
        source = group["source"]
        record = group["record"]
        target_id = f"t{index}"
        processor_kind = str(getattr(record, "processor_kind", "") or "")
        locator_sheet = None
        for candidate in _COMMON_SHEETS:
            if _norm(candidate) in _norm(group["clause"]):
                locator_sheet = candidate
                break
        locators = (
            _locators(group["clause"], target_id, sheet_hint=locator_sheet)
            if processor_kind == "spreadsheet_table"
            else (TargetLocator(target_id=target_id, match_basis="file"),)
        )
        targets.append(RetrievalTarget(
            target_id=target_id, kb_id=source.kb_id, kb_name=source.kb_name,
            record_id=record.id, processor_kind=processor_kind,
            document_name=str(getattr(record, "document_name", "") or ""),
            stamps=_record_stamp(record),
            locators=locators,
            match_basis=group["basis"], source="explicit", query_id=f"q{index}",
            question=_clean_question(group["clause"], group["basis"]),
        ))

    if targets and _wants_embedded_children(question):
        expanded = list(targets)
        next_index = len(expanded) + 1
        for parent in list(targets):
            if parent.processor_kind != "ragflow":
                continue
            for child in _embedded_children(parent, records_by_kb):
                target_id = f"t{next_index}"
                next_index += 1
                expanded.append(RetrievalTarget(
                    target_id=target_id, kb_id=parent.kb_id, kb_name=parent.kb_name,
                    record_id=child.id, processor_kind=str(getattr(child, "processor_kind", "") or ""),
                    document_name=str(getattr(child, "document_name", "") or ""),
                    stamps=_record_stamp(child),
                    locators=(TargetLocator(target_id=target_id, match_basis="embedded_child"),),
                    match_basis="embedded_child", source="explicit",
                    query_id=parent.query_id, question=parent.question,
                ))
        targets = expanded

    if not targets and not selected and history_hint:
        hint_targets = _targets_from_history(history_hint, sources, records_by_kb)
        if hint_targets:
            targets = hint_targets
            diagnostics.append({"resolution": "resolved", "reason": "history_hint", "targets": len(targets)})

    if ambiguous:
        resolution = "target_ambiguous"
    elif not_found and not targets:
        resolution = "target_not_found"
    elif targets:
        resolution = "resolved"
    else:
        resolution = "unrestricted_within_kb"
    return TurnRetrievalTargets(
        turn_id=turn_id, scope_fingerprint=scope_fingerprint, targets=tuple(targets),
        resolution=resolution, diagnostics=diagnostics,
        target_fingerprint=_target_fingerprint(targets),
    )


def looks_like_follow_up(question: str) -> bool:
    """Conservatively detect a turn that inherits the previous target."""
    value = str(question or "").strip()
    if not value or len(value) > 120:
        return False
    return bool(re.search(
        r"^(?:那|那么|这个|该|其|它|上述|刚才|前面|继续|再看|还有|另外)|(?:呢|吗)[？?]?$",
        value,
    ))


def history_target_hint(summary: dict | None) -> list[dict]:
    """Extract a non-authoritative target hint from a completed turn summary."""
    if not isinstance(summary, dict):
        return []
    targets = ((summary.get("retrieval_targets") or {}).get("targets") or [])
    hint = []
    for target in targets:
        try:
            kb_id, record_id = int(target.get("kb_id")), int(target.get("record_id"))
        except (TypeError, ValueError):
            continue
        if kb_id > 0 and record_id > 0:
            hint.append({"kb_id": kb_id, "record_id": record_id})
    return hint


def _embedded_children(parent: RetrievalTarget, records_by_kb: Mapping[int, list]) -> list:
    """Return children whose verified parent name references this document."""
    children = []
    for record in records_by_kb.get(parent.kb_id, []):
        if int(getattr(record, "embed_depth", 0) or 0) <= 0:
            continue
        raw = getattr(record, "parent_document", "") or ""
        try:
            parents = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        except (TypeError, ValueError):
            parents = [part.strip() for part in str(raw).split(",") if part.strip()]
        if not isinstance(parents, list):
            parents = [str(parents)]
        if parent.document_name in {str(name) for name in parents}:
            children.append(record)
    return children


def _wants_embedded_children(question: str) -> bool:
    if re.search(r"不(?:要|包含|使用|读取).{0,8}(?:内嵌|嵌入)|仅(?:使用|依据).{0,8}原生表格", question):
        return False
    return bool(re.search(
        r"(?:包含|使用|一并|连同).{0,12}(?:内嵌|嵌入)|(?:内嵌|嵌入).{0,12}(?:表格|Excel|xlsx)",
        question, re.IGNORECASE,
    ))


def _targets_from_history(history_hint: Sequence[dict], sources: Sequence, records_by_kb: Mapping[int, list]) -> tuple[RetrievalTarget, ...]:
    by_key = {(source.kb_id, record.id): (source, record) for source in sources for record in records_by_kb.get(source.kb_id, [])}
    targets = []
    for index, hint in enumerate(history_hint, 1):
        key = (int(hint.get("kb_id") or 0), int(hint.get("record_id") or 0))
        if key not in by_key:
            continue
        source, record = by_key[key]
        target_id = f"t{index}"
        targets.append(RetrievalTarget(
            target_id=target_id, kb_id=source.kb_id, kb_name=source.kb_name,
            record_id=record.id, processor_kind=str(getattr(record, "processor_kind", "") or ""),
            document_name=str(getattr(record, "document_name", "") or ""),
            stamps=_record_stamp(record),
            locators=(TargetLocator(target_id=target_id, match_basis="history_hint", source="history_hint"),),
            match_basis="history_hint", source="history_hint", query_id="q1", question="",
        ))
    return tuple(targets)


def sanitize_query_for_targets(query: str, targets: Sequence[RetrievalTarget]) -> str:
    """Remove file-navigation names from a model query before retrieval."""
    value = unicodedata.normalize("NFKC", str(query or ""))
    for target in targets:
        for phrase, specificity in _alias_phrases(getattr(target, "document_name", "") or ""):
            if specificity > 3:
                continue
            for variant in {phrase, phrase.replace("_", " ")}:
                if variant:
                    value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])",
                                   " ", value, flags=re.IGNORECASE)
    value = _PROJECT_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip(" ，,。；;、|") or str(query or "").strip()


def retriever_kind_for(target: RetrievalTarget) -> str:
    if target.processor_kind == "spreadsheet_table":
        if any(locator.cell_ref for locator in target.locators):
            return "spreadsheet_cell"
        return "spreadsheet_semantic"
    if target.processor_kind == "circuit_design":
        return "circuit"
    if target.processor_kind == "external_conversation":
        return "conversation"
    if target.processor_kind == "wiki":
        return "wiki"
    return "document"


def compile_work_items(targets: Sequence[RetrievalTarget]) -> tuple[RetrievalWorkItem, ...]:
    groups: dict[tuple[int, str, str, str], dict] = {}
    for target in targets:
        kind = retriever_kind_for(target)
        key = (target.kb_id, kind, target.query_id, _norm(target.question))
        group = groups.setdefault(key, {"targets": [], "locators": {}})
        group["targets"].append(target)
        group["locators"].setdefault(target.record_id, []).extend(target.locators)

    work_items: list[RetrievalWorkItem] = []
    for index, ((kb_id, kind, query_id, question), group) in enumerate(sorted(groups.items(), key=lambda item: item[0]), 1):
        grouped_targets: list[RetrievalTarget] = group["targets"]
        processor_kinds = {target.processor_kind for target in grouped_targets}
        contract = DocumentRetrievalContract(
            mode="frozen",
            allowed_record_ids=tuple(sorted({target.record_id for target in grouped_targets})),
            expected_source_stamps={target.record_id: target.stamps for target in grouped_targets},
            processor_kind=next(iter(processor_kinds)) if len(processor_kinds) == 1 else None,
        )
        work_items.append(RetrievalWorkItem(
            work_id=f"w{index}", target_ids=tuple(target.target_id for target in grouped_targets),
            query_id=query_id, kb_id=kb_id, retriever_kind=kind,
            normalized_query=question or _norm(grouped_targets[0].document_name),
            contract=contract,
            locators_by_record={record_id: tuple(locators) for record_id, locators in group["locators"].items()},
        ))
    return tuple(work_items)
