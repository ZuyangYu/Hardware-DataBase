from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .schemas import EvaluationSample


class DatasetValidationError(ValueError):
    """Raised when an evaluation JSONL file is invalid."""


def _parse_dataset(path: Path) -> tuple[list[EvaluationSample], list[str]]:
    samples: list[EvaluationSample] = []
    errors: list[str] = []
    seen: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        return [], [f"cannot read dataset {path}: {exc}"]

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
            if not isinstance(payload, dict):
                raise ValueError("sample must be a JSON object")
            sample = EvaluationSample.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            errors.append(f"line {line_number}: {exc}")
            continue
        if sample.id in seen:
            errors.append(
                f"duplicate sample id '{sample.id}' at line {line_number} "
                f"(first seen at line {seen[sample.id]})"
            )
            continue
        seen[sample.id] = line_number
        samples.append(sample)
    if not samples and not errors:
        errors.append("dataset contains no samples")
    return samples, errors


def validate_dataset(path: str | Path) -> list[str]:
    """Return every validation error without raising."""
    _, errors = _parse_dataset(Path(path))
    return errors


def load_dataset(path: str | Path) -> list[EvaluationSample]:
    """Load a validated evaluation dataset or raise one aggregated error."""
    samples, errors = _parse_dataset(Path(path))
    if errors:
        raise DatasetValidationError("; ".join(errors))
    return samples
