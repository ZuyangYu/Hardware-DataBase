"""The acceptance sweep covers the limits promised by the plan."""

from scripts.acceptance.run_parameter_sweep import EXPECTED_RESULTS, EXPERIMENTS


def test_parameter_sweep_includes_plan_upper_bounds():
    names = {name for name, _overrides in EXPERIMENTS}

    assert {"calls_48", "calls_96"} <= names
    assert {"deadline_60", "deadline_90"} <= names
    assert {"bytes_16000", "bytes_24000"} <= names


def test_parameter_sweep_records_bytes_4000_as_expected_negative_control():
    names = {name for name, _overrides in EXPERIMENTS}
    assert EXPECTED_RESULTS["bytes_4000"] is False
    assert all(EXPECTED_RESULTS.get(name, True) is True for name in names - {"bytes_4000"})
