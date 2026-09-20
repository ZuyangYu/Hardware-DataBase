"""Shared test isolation.

Admission control is a process-wide singleton keyed by user/tenant id.  Tests
reuse small user ids across suites, so quotas would accumulate across unrelated
tests and produce order-dependent 429s.  Admission stays disabled by default in
tests; the admission suites enable it explicitly (and reset the controller).
"""

from __future__ import annotations

import pytest

import src.settings
from src.core.concurrency.admission import reset_admission_controller


@pytest.fixture(autouse=True)
def _disable_admission_by_default():
    original = src.settings.ADMISSION_CONTROL_ENABLED
    src.settings.ADMISSION_CONTROL_ENABLED = False
    reset_admission_controller()
    try:
        yield
    finally:
        src.settings.ADMISSION_CONTROL_ENABLED = original
        reset_admission_controller()
