from __future__ import annotations

import pytest

from src.services.pipeline_directories import (
    DirectoryValidationError,
    normalize_directory_name,
    parse_relative_path,
)


@pytest.mark.parametrize(
    "value",
    ["", "   ", ".", "..", "a/b", r"a\\b", "CON", "CON.txt", "name.", "\x00name"],
)
def test_directory_names_reject_unsafe_segments(value):
    with pytest.raises(DirectoryValidationError):
        normalize_directory_name(value)


@pytest.mark.parametrize(
    "value",
    ["/absolute.pdf", r"folder\\file.pdf", "../escape.pdf", "folder/../file.pdf", "C:/file.pdf", "folder//file.pdf"],
)
def test_relative_paths_reject_traversal_and_non_posix_forms(value):
    with pytest.raises(DirectoryValidationError):
        parse_relative_path(value)


def test_relative_path_returns_valid_directory_segments_and_basename():
    assert parse_relative_path("docs/power/spec.pdf") == (["docs", "power"], "spec.pdf")
    assert parse_relative_path("spec.pdf") == ([], "spec.pdf")
