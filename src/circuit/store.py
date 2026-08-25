"""Circuit design persistence.

Implements the storage layout prescribed by plan §3.4:

    storage/circuits/
    ├── {kb_name}/
    │   └── {design_id}/
    │       ├── circuit_state.json         # full CircuitDesign serialization
    │       ├── connectivity_graph.gpickle # NetworkX graph (written by GraphStore)
    │       ├── module_screenshots/        # PNGs keyed by module_id (Phase 4)
    │       └── pdf_cache/                 # cached schematic PDFs / page renders
    └── index.json                         # global circuit registry

Backward compatibility: previous revisions wrote ``circuit.json``. The loader
silently falls back to that filename so existing knowledge bases keep working,
and the next save migrates them to ``circuit_state.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config.settings
from src.circuit.models import CircuitDesign
from src.ingestion.kb_paths import validate_kb_name


_DESIGN_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")

STATE_FILE = "circuit_state.json"
LEGACY_STATE_FILE = "circuit.json"
MODULE_SCREENSHOT_DIR = "module_screenshots"
PDF_CACHE_DIR = "pdf_cache"
INDEX_FILE = "index.json"


def make_design_id(filename: str) -> str:
    stem = Path(filename).stem.strip() or "circuit"
    return _DESIGN_ID_RE.sub("_", stem)[:128]


def make_content_addressed_design_id(filename: str, content_hash: str | None) -> str:
    """Derive a collision-resistant design id from filename + content hash.

    ``make_design_id`` alone maps distinct files to the same id (spaces and
    CJK characters normalise to ``_``, long stems truncate at 128 chars), so a
    re-upload with different content would silently overwrite the earlier
    design. Appending 8 hex chars of the content hash makes the id unique per
    file content while keeping the human-readable stem prefix.
    """
    base = make_design_id(filename)
    if not content_hash:
        return base
    digest = re.sub(r"[^a-f0-9]", "", str(content_hash).lower())[:8]
    if not digest:
        return base
    return f"{base[:119]}-{digest}"


def design_id_matches_file(design_id: str, filename: str) -> bool:
    """Whether ``design_id`` could have been derived from ``filename``.

    Accepts both legacy plain-stem ids and content-addressed
    ``<stem>-<8hex>`` ids, so alias resolution / record cleanup keeps working
    for designs stored before the hash suffix was introduced.
    """
    if not design_id or not filename:
        return False
    base = make_design_id(filename)
    if design_id == base:
        return True
    if design_id.startswith(f"{base}-"):
        return re.fullmatch(r"[0-9a-f]{8}", design_id[len(base) + 1:]) is not None
    return False


def derive_circuit_aliases(design_id: str, file_names: list[str]) -> list[str]:
    """Derive human-facing aliases for a circuit.

    Used by the circuit index and ``CircuitScopeResolver`` so a user can refer
    to a design by its source-file stem or a spaced form of the design id
    (``main_board`` → ``main board``) even when no explicit alias was recorded
    at upload time. Purely a recall aid — resolution still validates against
    the current ``kb_name``'s design list (plan §4.3 / §4.8).
    """
    aliases: set[str] = set()
    stem = (design_id or "").strip()
    if stem:
        aliases.add(stem)
        spaced = re.sub(r"[_-]+", " ", stem).strip()
        if spaced and spaced != stem:
            aliases.add(spaced)
    for name in file_names or []:
        base = Path(name).stem.strip()
        if not base:
            continue
        aliases.add(base)
        spaced = re.sub(r"[_-]+", " ", base).strip()
        if spaced and spaced != base:
            aliases.add(spaced)
    return sorted(aliases)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(design: CircuitDesign) -> str:
    """Stable hash of a design's structured content (plan §4.6 version stamp).

    Only the structured payload matters — ``updated_at`` is excluded so
    re-saving an unchanged design keeps the same version and session
    ``last_entities`` don't get spuriously invalidated.
    """
    payload = design.to_dict()
    payload.pop("parse_warnings", None)  # warnings are non-structural
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: str, payload: str) -> None:
    """Write ``payload`` to ``path`` atomically (tmp file + os.replace + fsync)."""
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class CircuitStore:
    """File-system backed store for ``CircuitDesign`` objects.

    Layout helpers (``design_dir``, ``module_screenshot_dir``, ``pdf_cache_dir``)
    centralise path resolution so other components never compute paths by hand.
    """

    # Serialises global index.json read-modify-write cycles. Store instances
    # are frequently created per call site, so this must be class-level:
    # concurrent uploads each reading the index then writing it back would
    # otherwise lose the earlier design's registry entry.
    _INDEX_LOCK = threading.RLock()

    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(config.settings.STORAGE_DIR, "circuits")

    # ── path helpers ──────────────────────────────────────────────────────

    def design_dir(self, kb_name: str, design_id: str, create: bool = False) -> str:
        kb_name = validate_kb_name(kb_name)
        design_id = make_design_id(design_id)
        path = os.path.abspath(os.path.join(self.root, kb_name, design_id))
        root_abs = os.path.abspath(self.root)
        if os.path.commonpath([root_abs, path]) != root_abs:
            raise ValueError("Resolved circuit path escapes storage root.")
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    def module_screenshot_dir(self, kb_name: str, design_id: str, create: bool = False) -> str:
        path = os.path.join(self.design_dir(kb_name, design_id, create=create), MODULE_SCREENSHOT_DIR)
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    def pdf_cache_dir(self, kb_name: str, design_id: str, create: bool = False) -> str:
        path = os.path.join(self.design_dir(kb_name, design_id, create=create), PDF_CACHE_DIR)
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    def state_path(self, kb_name: str, design_id: str) -> str:
        return os.path.join(self.design_dir(kb_name, design_id), STATE_FILE)

    def index_path(self) -> str:
        return os.path.join(self.root, INDEX_FILE)

    # ── design CRUD ───────────────────────────────────────────────────────

    def save(self, design: CircuitDesign) -> str:
        path = self.design_dir(design.kb_name, design.design_id, create=True)
        target = os.path.join(path, STATE_FILE)
        payload = json.dumps(design.to_dict(), ensure_ascii=False, indent=2)
        _atomic_write(target, payload)

        # Migrate away from the legacy filename in one step.
        legacy = os.path.join(path, LEGACY_STATE_FILE)
        if os.path.exists(legacy) and os.path.abspath(legacy) != os.path.abspath(target):
            try:
                os.unlink(legacy)
            except OSError:
                pass

        self._update_index(design)

        return target

    def load(self, kb_name: str, design_id: str) -> CircuitDesign | None:
        design_dir = self.design_dir(kb_name, design_id)
        for candidate in (STATE_FILE, LEGACY_STATE_FILE):
            target = os.path.join(design_dir, candidate)
            if os.path.exists(target):
                with open(target, "r", encoding="utf-8") as f:
                    return CircuitDesign.from_dict(json.load(f))
        return None

    def list_designs(self, kb_name: str) -> list[CircuitDesign]:
        kb_dir = os.path.join(self.root, validate_kb_name(kb_name))
        if not os.path.isdir(kb_dir):
            return []
        designs: list[CircuitDesign] = []
        for name in sorted(os.listdir(kb_dir)):
            loaded = self.load(kb_name, name)
            if loaded:
                designs.append(loaded)
        return designs

    # ── global index ──────────────────────────────────────────────────────

    def _index_entry(self, design: CircuitDesign) -> dict[str, Any]:
        file_names = [f.file_name for f in design.files]
        return {
            "kb_name": design.kb_name,
            "design_id": design.design_id,
            "name": design.design_id,
            "aliases": derive_circuit_aliases(design.design_id, file_names),
            "content_hash": _content_hash(design),
            "status": str(design.status),
            "updated_at": _utcnow_iso(),
            "file_count": len(design.files),
            "instance_count": len(design.instances),
            "net_count": len(design.nets),
            "module_count": len(design.modules),
            "schematic_page_count": len(design.schematic_pages),
            "files": [
                {"name": f.file_name, "type": f.file_type, "source_group": f.source_group}
                for f in design.files
            ],
        }

    def circuit_version(self, kb_name: str, design_id: str) -> str | None:
        """Current content version stamp for a circuit (plan §4.6).

        Returns the index ``content_hash`` (falls back to ``updated_at``),
        or ``None`` when the circuit isn't registered. Used to detect that a
        session's ``last_entities`` were captured against an older parse.
        """
        for entry in self._read_index().get("designs", []):
            if entry.get("kb_name") == kb_name and entry.get("design_id") == design_id:
                return entry.get("content_hash") or entry.get("updated_at")
        return None

    def _read_index(self) -> dict[str, Any]:
        target = self.index_path()
        if not os.path.exists(target):
            return {"version": 1, "designs": []}
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"version": 1, "designs": []}
            data.setdefault("version", 1)
            data.setdefault("designs", [])
            if not isinstance(data["designs"], list):
                data["designs"] = []
            return data
        except (OSError, ValueError):
            return {"version": 1, "designs": []}

    def _update_index(self, design: CircuitDesign) -> None:
        with self._INDEX_LOCK:
            os.makedirs(self.root, exist_ok=True)
            index = self._read_index()
            entry = self._index_entry(design)
            designs: list[dict[str, Any]] = [
                item for item in index.get("designs", [])
                if not (item.get("kb_name") == design.kb_name and item.get("design_id") == design.design_id)
            ]
            designs.append(entry)
            designs.sort(key=lambda item: (item.get("kb_name", ""), item.get("design_id", "")))
            index["designs"] = designs
            index["updated_at"] = _utcnow_iso()
            _atomic_write(self.index_path(), json.dumps(index, ensure_ascii=False, indent=2))

    def read_index(self) -> dict[str, Any]:
        """Public accessor for the global registry (UI / observability)."""
        return self._read_index()

    def remove_from_index(self, kb_name: str, design_id: str) -> None:
        with self._INDEX_LOCK:
            index = self._read_index()
            designs = [
                item for item in index.get("designs", [])
                if not (item.get("kb_name") == kb_name and item.get("design_id") == design_id)
            ]
            index["designs"] = designs
            index["updated_at"] = _utcnow_iso()
            _atomic_write(self.index_path(), json.dumps(index, ensure_ascii=False, indent=2))

    # ── deletion helpers ──────────────────────────────────────────────────

    def delete_design(self, kb_name: str, design_id: str) -> bool:
        """Remove a single design's directory and drop it from the global index.

        Returns ``True`` if at least one of the directory or the index entry was
        actually removed; ``False`` if nothing to do (caller can warn).
        """
        kb_name = validate_kb_name(kb_name)
        target_dir = self.design_dir(kb_name, design_id)
        removed = False
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir, ignore_errors=False)
            removed = True
        # Always reconcile the global index, even when the dir was already gone.
        index_before = self._read_index().get("designs", [])
        self.remove_from_index(kb_name, design_id)
        index_after = self._read_index().get("designs", [])
        if len(index_after) < len(index_before):
            removed = True
        return removed

    def delete_kb(self, kb_name: str) -> bool:
        """Remove every design directory for a knowledge base and drop its
        entries from the global index. Returns ``True`` when anything changed.
        """
        kb_name = validate_kb_name(kb_name)
        kb_dir = os.path.join(self.root, kb_name)
        removed = False
        if os.path.isdir(kb_dir):
            shutil.rmtree(kb_dir, ignore_errors=False)
            removed = True
        index = self._read_index()
        before = index.get("designs", [])
        kept = [item for item in before if item.get("kb_name") != kb_name]
        if len(kept) != len(before):
            with self._INDEX_LOCK:
                index = self._read_index()
                before = index.get("designs", [])
                kept = [item for item in before if item.get("kb_name") != kb_name]
                if len(kept) != len(before):
                    index["designs"] = kept
                    index["updated_at"] = _utcnow_iso()
                    _atomic_write(self.index_path(), json.dumps(index, ensure_ascii=False, indent=2))
            removed = True
        return removed

    # ── module / pdf artifact helpers ─────────────────────────────────────

    def module_screenshot_path(
        self, kb_name: str, design_id: str, module_id: str, ext: str = "png"
    ) -> str:
        safe_module = _DESIGN_ID_RE.sub("_", module_id)[:128] or "module"
        directory = self.module_screenshot_dir(kb_name, design_id, create=True)
        return os.path.join(directory, f"{safe_module}.{ext}")

    def list_module_screenshots(self, kb_name: str, design_id: str) -> list[str]:
        directory = self.module_screenshot_dir(kb_name, design_id)
        if not os.path.isdir(directory):
            return []
        return sorted(
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if not name.startswith(".")
        )

    def list_pdf_cache(self, kb_name: str, design_id: str) -> list[str]:
        directory = self.pdf_cache_dir(kb_name, design_id)
        if not os.path.isdir(directory):
            return []
        return sorted(
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if not name.startswith(".")
        )
