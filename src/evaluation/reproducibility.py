"""Content identities independent of JSONL order and file formatting."""
from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .schemas import EvaluationSample


class EvaluationReproducibilityError(ValueError):
    """A frozen input or scorer changed since this run/checkpoint was created."""


def public_evaluator_config(config: dict) -> dict:
    return {
        key: value for key, value in config.items()
        if key not in {"scoring_requested", "scoring_resume", "collection_qc_verdict"}
    }


def execution_content_hash(samples: list[EvaluationSample]) -> str:
    records = [sample.model_dump(mode="json") for sample in sorted(samples, key=lambda item: item.id)]
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def scoring_rules_hash() -> str:
    digest = hashlib.sha256(b"hdb-scoring-rules-v1\0")
    for package in ("ragas", "openai", "instructor"):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "not_installed"
        digest.update(f"{package}=={installed}\0".encode())
    for name in ("ragas_adapter.py", "hardware_metrics.py", "cohorts.py", "gates.py", "service.py"):
        digest.update(name.encode() + b"\0" + Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()
