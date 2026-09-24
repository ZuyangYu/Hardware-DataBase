"""Structural validation for EDIF text edits.

EDIF is plain text, so an exact replacement is technically possible, but the
candidate is only accepted when the project's own netlist parser still reads
the same number of instances and nets.  A cosmetic edit passes; a connectivity
change or a syntax break is refused.
"""

from __future__ import annotations

import os
import tempfile


class EdfRevisionError(ValueError):
    """Raised when an EDIF edit no longer parses to the same structure."""


def _parse_counts(data: bytes) -> tuple[int, int]:
    from src.circuit.parsers.edf_parser import EdfParser

    with tempfile.NamedTemporaryFile(suffix=".edf", delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        instances, nets, _libraries = EdfParser(path).parse()
        return len(instances), len(nets)
    except EdfRevisionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parser failure rejects the edit
        raise EdfRevisionError(f"EDF 解析校验失败: {exc}") from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def validate_edf_replacement(parent: bytes, child: bytes) -> None:
    """Require the edited EDIF to parse with unchanged instance/net counts."""

    parent_counts = _parse_counts(parent)
    child_counts = _parse_counts(child)
    if child_counts != parent_counts:
        raise EdfRevisionError(
            "EDF 结构校验未通过：实例/网络数量发生变化 "
            f"({parent_counts} -> {child_counts})；该修改不会生成候选"
        )


__all__ = ["EdfRevisionError", "validate_edf_replacement"]
